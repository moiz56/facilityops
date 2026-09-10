"""Parse and validate a supplied run record: stages 1 and 2 of the pipeline (5.2).

`load_record` turns one record JSON file into a `Record`. It reads section 2 as
the data contract and the supplied file as a claim about it, so where the two
disagree the contract wins (section 11) and the disagreement is recorded rather
than resolved.

Three rules govern everything here:

- Absent, null, the empty string and the empty array are the same thing, all
  resolving to None (2.9). Optional sequences resolve to an empty tuple.
- Nothing is coerced. A field of the wrong type is refused, set to None, and
  recorded as a `FieldAnomaly`; a confidence recorded as the string "0.94" must
  never reach the page as a measurement (2.9, section 11, TA-27).
- Only an unusable record raises. `RecordParseError` is for a file that cannot
  be read, is not JSON, is not a JSON object, or carries no run_id - without a
  run_id there is no name for the output files (4.1). Everything else is
  handled defensively and recorded, because section 11 requires a degraded
  record to produce a report rather than a crash.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar

from common.schema import (
    Accelerometer, Checkpoint, Environment, Event, FieldAnomaly, Finding,
    Particulate, Point2D, Pose, RawSensor, Record, SampleSensor, SensorAlert,
    SensorBlock, SensorSample, SensorWarning,
)

logger = logging.getLogger(__name__)

__all__ = ["RecordParseError", "load_record", "parse_offset_timestamp", "parse_local_timestamp"]

#: Anomaly owner for a field belonging to the run itself rather than an item.
RUN_LEVEL = "__run__"

#: The declared counts of 2.1. Claims about the arrays, reconciled in derive.py.
_COUNT_FIELDS = (
    "total_required_checkpoints", "total_completed_checkpoints", "passed_checkpoints",
    "failed_checkpoints", "missed_checkpoints", "warned_checkpoints", "finding_count",
)

T = TypeVar("T")


class RecordParseError(Exception):
    """A record that cannot be loaded at all.

    Raised only for structural failure: an unreadable file, invalid JSON, a
    root that is not an object, or a missing run_id. The message names the file
    and the specific problem (5.3, TA-26).
    """


# ---------------------------------------------------------------------------
# Timestamps
#
# The record carries two formats and section 2.8 requires them parsed
# separately. Both return None rather than raising, so an unreadable timestamp
# is data to be reported, not an exception.
# ---------------------------------------------------------------------------


def parse_offset_timestamp(text: str) -> datetime | None:
    """Parse an ISO 8601 timestamp with a colon-less offset, e.g. -0700 (2.1).

    An offset is required. `fromisoformat` accepts a space separator and a
    missing zone, which would let a sensor `received_at` pass as a run
    timestamp and yield a naive datetime; one naive value among aware ones
    raises when 3.5 sorts alerts by time. A result with no offset is refused
    here so the caller records it, rather than crashing a later stage.
    """
    parsed = None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None else None


def parse_local_timestamp(text: str) -> datetime | None:
    """Parse a sensor `received_at`: space-separated, no timezone (2.8).

    The result is naive. No zone is invented for it.
    """
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Collecting what the record got wrong
# ---------------------------------------------------------------------------


class _Context:
    """Collects field anomalies and undescribed keys while one record is parsed."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.anomalies: list[FieldAnomaly] = []
        self.unknown: list[str] = []

    def anomaly(self, item_id: str, field_name: str, problem: str) -> None:
        """Record a field that was present but unusable as recorded.

        Logged at debug only: the validate stage of the pipeline surfaces
        these, and logging them twice buries the rest of the output.
        """
        logger.debug("%s: %s.%s %s", self.path.name, item_id, field_name, problem)
        self.anomalies.append(
            FieldAnomaly(item_id=item_id, field_name=field_name, problem=problem)
        )

    def undescribed(self, where: str, keys: Iterable[str]) -> None:
        """Note keys section 2 does not describe.

        Not an anomaly: an extra key costs nothing and loses nothing. It is
        worth knowing about, so load_record reports the total, but it must not
        be louder than a value the engine had to refuse.
        """
        listed = ", ".join(sorted(keys))
        logger.debug("%s: %s carries keys not in section 2: %s", self.path.name, where, listed)
        self.unknown.append(f"{where}: {listed}")


# ---------------------------------------------------------------------------
# Reading one value
# ---------------------------------------------------------------------------


def _blank(value: Any) -> bool:
    """Whether a value counts as absent (2.9).

    Section 2.9 makes absent, null, the empty string and the empty array the
    same thing. An empty object is included too: a coordinates block with no
    keys states nothing.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict)):
        return not value
    return False


def _known_keys(
    data: Mapping[str, Any], known: Iterable[str], where: str, ctx: _Context,
) -> None:
    """Note any key of `data` that section 2 does not describe."""
    unknown = set(data) - set(known)
    if unknown:
        ctx.undescribed(where, unknown)


def _typed(
    data: Mapping[str, Any], key: str, kind: type | tuple[type, ...], described: str,
    ctx: _Context, item_id: str, *, required: bool = False,
) -> Any | None:
    """Return data[key] if it is of the expected type, else None with an anomaly.

    `required` mirrors section 2's optional column: pass it where the column
    reads "no", so that an absent field is recorded rather than passed over.

    Never coerces. `bool` is excluded from numeric kinds because it is a subclass
    of int and a flag recorded where a count belongs is a real error, not a 1.
    """
    if key not in data or _blank(data[key]):
        if required:
            ctx.anomaly(item_id, key, "required by section 2 but absent")
        return None
    value = data[key]
    if isinstance(value, bool) and kind is not bool:
        ctx.anomaly(item_id, key, f"recorded as the boolean {value!r}, expected {described}")
        return None
    if not isinstance(value, kind):
        ctx.anomaly(
            item_id, key,
            f"recorded as {type(value).__name__} {value!r}, expected {described}; "
            "left unset rather than converted",
        )
        return None
    return value.strip() if isinstance(value, str) else value


def _text(
    data: Mapping[str, Any], key: str, ctx: _Context, item_id: str, *, required: bool = False,
) -> str | None:
    """Read a string field."""
    return _typed(data, key, str, "a string", ctx, item_id, required=required)


def _whole(
    data: Mapping[str, Any], key: str, ctx: _Context, item_id: str, *, required: bool = False,
) -> int | None:
    """Read an integer field. A float is refused rather than truncated."""
    return _typed(data, key, int, "an integer", ctx, item_id, required=required)


def _number(
    data: Mapping[str, Any], key: str, ctx: _Context, item_id: str, *, required: bool = False,
) -> float | None:
    """Read a float field. A numeric string is refused rather than parsed (TA-27)."""
    value = _typed(data, key, (int, float), "a number", ctx, item_id, required=required)
    return float(value) if value is not None else None


def _confidence(
    data: Mapping[str, Any], key: str, ctx: _Context, item_id: str,
) -> float | None:
    """Read a confidence, applying section 2.9's reading of 0.0.

    A confidence of 0.0 means no confidence was produced, not zero confidence,
    so it resolves to None like an absent one and the report renders "not
    scored". The distinction between an absent key and a recorded 0.0 is not
    preserved, because 2.9 gives them the same meaning.
    """
    value = _number(data, key, ctx, item_id)
    if value == 0.0:
        logger.debug("%s: confidence recorded as 0.0, read as not scored (2.9)", item_id)
        return None
    return value


def _flag(
    data: Mapping[str, Any], key: str, ctx: _Context, item_id: str, *, required: bool = False,
) -> bool | None:
    """Read a boolean field. The string "false" is refused, not read as False."""
    return _typed(data, key, bool, "a boolean", ctx, item_id, required=required)


def _moment(
    data: Mapping[str, Any], key: str, parse: Callable[[str], datetime | None],
    ctx: _Context, item_id: str, *, required: bool = False,
) -> datetime | None:
    """Read a timestamp with the given parser (2.1, 2.8).

    Returns None when the text is absent or will not parse. The text as
    recorded is quoted in the anomaly, so an unreadable timestamp reaches the
    report as what the robot wrote rather than as a blank.
    """
    text = _text(data, key, ctx, item_id, required=required)
    if text is None:
        return None
    value = parse(text)
    if value is None:
        ctx.anomaly(item_id, key, f"timestamp {text!r} not in the expected format")
    return value


# ---------------------------------------------------------------------------
# Reading arrays and nested objects
# ---------------------------------------------------------------------------


def _objects(
    data: Mapping[str, Any], key: str, ctx: _Context, item_id: str,
) -> tuple[Mapping[str, Any], ...]:
    """Read an array of objects, skipping and recording entries that are not objects."""
    value = data.get(key)
    if _blank(value):
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        ctx.anomaly(item_id, key, f"recorded as {type(value).__name__}, expected an array")
        return ()
    kept = []
    for index, entry in enumerate(value):
        if isinstance(entry, Mapping):
            kept.append(entry)
        else:
            ctx.anomaly(item_id, f"{key}[{index}]",
                        f"recorded as {type(entry).__name__}, expected an object")
    return tuple(kept)


def _strings(
    data: Mapping[str, Any], key: str, ctx: _Context, item_id: str,
) -> tuple[str, ...]:
    """Read an array of path strings, skipping and recording entries that are not.

    Entries are stripped. A recorded path with surrounding whitespace would not
    resolve under the evidence root, and would then be reported as a missing
    image for the wrong reason.
    """
    value = data.get(key)
    if _blank(value):
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        ctx.anomaly(item_id, key, f"recorded as {type(value).__name__}, expected an array")
        return ()
    kept = []
    for index, entry in enumerate(value):
        if isinstance(entry, str) and entry.strip():
            kept.append(entry.strip())
        else:
            ctx.anomaly(item_id, f"{key}[{index}]",
                        f"recorded as {type(entry).__name__} {entry!r}, expected a path string")
    return tuple(kept)


def _nested(
    data: Mapping[str, Any], key: str, build: Callable[[Mapping[str, Any], _Context, str], T],
    ctx: _Context, item_id: str,
) -> T | None:
    """Read one nested object with `build`, or None when it is absent.

    The single place that handles "absent" and "present but not an object", so
    every nested block behaves the same way.
    """
    block = data.get(key)
    if _blank(block):
        return None
    if not isinstance(block, Mapping):
        ctx.anomaly(item_id, key, f"recorded as {type(block).__name__}, expected an object")
        return None
    return build(block, ctx, item_id)


# ---------------------------------------------------------------------------
# Building each shape of section 2
# ---------------------------------------------------------------------------


def _build_pose(block: Mapping[str, Any], ctx: _Context, item_id: str) -> Pose:
    """Build checkpoint or finding coordinates: x, y, z, yaw (2.2)."""
    _known_keys(block, ("x", "y", "z", "yaw"), f"{item_id}.coordinates", ctx)
    return Pose(
        x=_number(block, "x", ctx, item_id), y=_number(block, "y", ctx, item_id),
        z=_number(block, "z", ctx, item_id), yaw=_number(block, "yaw", ctx, item_id),
    )


def _build_point(block: Mapping[str, Any], ctx: _Context, item_id: str) -> Point2D:
    """Build alert or sample coordinates: x and y only (2.5).

    Kept separate from `_build_pose`. Section 2.5 warns against one parser
    assuming all four keys.
    """
    _known_keys(block, ("x", "y"), f"{item_id}.coordinates", ctx)
    return Point2D(x=_number(block, "x", ctx, item_id), y=_number(block, "y", ctx, item_id))


def _build_accelerometer(block: Mapping[str, Any], ctx: _Context, item_id: str) -> Accelerometer:
    """Build the accelerometer sub-block (2.8)."""
    _known_keys(block, ("accel_x", "accel_y", "accel_z", "vibration_peak", "vibration_rms_g"),
                f"{item_id}.accelerometer", ctx)
    return Accelerometer(
        accel_x=_number(block, "accel_x", ctx, item_id),
        accel_y=_number(block, "accel_y", ctx, item_id),
        accel_z=_number(block, "accel_z", ctx, item_id),
        vibration_peak=_number(block, "vibration_peak", ctx, item_id),
        vibration_rms_g=_number(block, "vibration_rms_g", ctx, item_id),
    )


def _build_environment(block: Mapping[str, Any], ctx: _Context, item_id: str) -> Environment:
    """Build the environment sub-block (2.8)."""
    _known_keys(block, ("temperature_c", "humidity_pct", "pressure_hpa"),
                f"{item_id}.environment", ctx)
    return Environment(
        temperature_c=_number(block, "temperature_c", ctx, item_id),
        humidity_pct=_number(block, "humidity_pct", ctx, item_id),
        pressure_hpa=_number(block, "pressure_hpa", ctx, item_id),
    )


def _build_particulate(block: Mapping[str, Any], ctx: _Context, item_id: str) -> Particulate:
    """Build the particulate sub-block (2.8).

    The values are read faithfully even when the governing flag is false.
    Whether they may be shown is decided in report/gaps.py, not here (5.4).
    """
    _known_keys(block, ("pm1_0", "pm2_5", "pm4_0", "pm10"), f"{item_id}.particulate", ctx)
    return Particulate(
        pm1_0=_number(block, "pm1_0", ctx, item_id),
        pm2_5=_number(block, "pm2_5", ctx, item_id),
        pm4_0=_number(block, "pm4_0", ctx, item_id),
        pm10=_number(block, "pm10", ctx, item_id),
    )


def _build_raw(block: Mapping[str, Any], ctx: _Context, item_id: str) -> RawSensor:
    """Build the raw device block, keeping its flat duplicates in `extra` (2.8).

    No undescribed-key note here: 2.8 says raw carries flat duplicates of every
    structured value, so unlisted keys are expected and `extra` is where they go.
    """
    described = ("device", "adxl345_ok", "bme680_ok", "sps30_ok", "air_status",
                 "vibration_status", "gas_kohms", "ts_ms", "received_at")
    return RawSensor(
        device=_text(block, "device", ctx, item_id),
        adxl345_ok=_flag(block, "adxl345_ok", ctx, item_id),
        bme680_ok=_flag(block, "bme680_ok", ctx, item_id),
        sps30_ok=_flag(block, "sps30_ok", ctx, item_id),
        air_status=_text(block, "air_status", ctx, item_id),
        vibration_status=_text(block, "vibration_status", ctx, item_id),
        gas_kohms=_number(block, "gas_kohms", ctx, item_id),
        ts_ms=_whole(block, "ts_ms", ctx, item_id),
        received_at=_moment(block, "received_at", parse_local_timestamp, ctx, item_id),
        extra={k: v for k, v in block.items() if k not in described},
    )


def _build_warning(block: Mapping[str, Any], ctx: _Context, item_id: str) -> SensorWarning:
    """Build one entry from a sensor block's warnings array (2.8)."""
    _known_keys(block, ("code", "label", "description", "severity"),
                f"{item_id}.sensor.warnings[]", ctx)
    return SensorWarning(
        code=_text(block, "code", ctx, item_id),
        label=_text(block, "label", ctx, item_id),
        description=_text(block, "description", ctx, item_id),
        severity=_text(block, "severity", ctx, item_id),
    )


def _build_sensor_block(block: Mapping[str, Any], ctx: _Context, item_id: str) -> SensorBlock:
    """Build a checkpoint's full sensor hub reading (2.8)."""
    _known_keys(block, ("ok", "status", "source", "sensor_hub_reachable", "age_seconds",
                        "timestamp", "received_at", "accelerometer", "environment",
                        "particulate", "warnings", "raw"), f"{item_id}.sensor", ctx)
    return SensorBlock(
        ok=_flag(block, "ok", ctx, item_id),
        status=_text(block, "status", ctx, item_id),
        source=_text(block, "source", ctx, item_id),
        sensor_hub_reachable=_flag(block, "sensor_hub_reachable", ctx, item_id),
        age_seconds=_number(block, "age_seconds", ctx, item_id),
        timestamp=_moment(block, "timestamp", parse_offset_timestamp, ctx, item_id),
        received_at=_moment(block, "received_at", parse_local_timestamp, ctx, item_id),
        accelerometer=_nested(block, "accelerometer", _build_accelerometer, ctx, item_id),
        environment=_nested(block, "environment", _build_environment, ctx, item_id),
        particulate=_nested(block, "particulate", _build_particulate, ctx, item_id),
        warnings=tuple(
            _build_warning(w, ctx, item_id) for w in _objects(block, "warnings", ctx, item_id)
        ),
        raw=_nested(block, "raw", _build_raw, ctx, item_id),
    )


def _build_sample_sensor(block: Mapping[str, Any], ctx: _Context, item_id: str) -> SampleSensor:
    """Build a sample's reduced sensor block: no raw, warnings, ok or age_seconds (2.6)."""
    _known_keys(block, ("accelerometer", "environment", "particulate"), f"{item_id}.sensor", ctx)
    return SampleSensor(
        accelerometer=_nested(block, "accelerometer", _build_accelerometer, ctx, item_id),
        environment=_nested(block, "environment", _build_environment, ctx, item_id),
        particulate=_nested(block, "particulate", _build_particulate, ctx, item_id),
    )


def _finding(data: Mapping[str, Any], ctx: _Context, where: str) -> Finding | None:
    """Read one finding, or one checkpoint detection, which share a shape (2.4).

    `where` locates the entry in the record, and is used for anomalies until a
    checkpoint_id is found: a finding does belong to a checkpoint, so once one
    is read it becomes the owner.
    """
    _known_keys(data, ("finding_id", "run_id", "checkpoint_id", "zone", "feature",
                       "description", "severity", "status", "timestamp", "evidence_image",
                       "coordinates", "recommended_action"), where, ctx)
    item_id = _text(data, "checkpoint_id", ctx, where) or where
    finding_id = _text(data, "finding_id", ctx, item_id, required=True)
    if finding_id is None:
        ctx.anomaly(where, "entry", "dropped: no finding_id to identify it by")
        return None
    return Finding(
        finding_id=finding_id,
        severity=_text(data, "severity", ctx, item_id, required=True) or "",
        status=_text(data, "status", ctx, item_id, required=True) or "",
        timestamp=_moment(data, "timestamp", parse_offset_timestamp, ctx, item_id, required=True),
        checkpoint_id=_text(data, "checkpoint_id", ctx, item_id, required=True),
        run_id=_text(data, "run_id", ctx, item_id),
        zone=_text(data, "zone", ctx, item_id),
        feature=_text(data, "feature", ctx, item_id),
        description=_text(data, "description", ctx, item_id),
        evidence_image=_text(data, "evidence_image", ctx, item_id),
        coordinates=_nested(data, "coordinates", _build_pose, ctx, item_id),
        recommended_action=_text(data, "recommended_action", ctx, item_id),
    )


def _checkpoint(data: Mapping[str, Any], ctx: _Context, index: int) -> Checkpoint | None:
    """Read one checkpoint (2.2).

    `status` and `result_status` are read independently; a checkpoint can be
    COMPLETED and FAIL, and neither is derived from the other.
    """
    where = f"checkpoints[{index}]"
    _known_keys(data, ("checkpoint_id", "checkpoint_name", "sequence_number", "zone", "status",
                       "result_status", "missed_reason", "observed", "expected_text", "notes",
                       "rule_type", "confidence", "timestamp", "coordinates", "evidence_images",
                       "annotated_images", "detections", "sensor"), where, ctx)
    item_id = _text(data, "checkpoint_id", ctx, where)
    if item_id is None:
        ctx.anomaly(where, "entry", "dropped: no checkpoint_id to identify it by")
        return None
    return Checkpoint(
        checkpoint_id=item_id,
        checkpoint_name=_text(data, "checkpoint_name", ctx, item_id, required=True) or "",
        zone=_text(data, "zone", ctx, item_id, required=True) or "",
        status=_text(data, "status", ctx, item_id, required=True) or "",
        result_status=_text(data, "result_status", ctx, item_id, required=True) or "",
        sequence_number=_whole(data, "sequence_number", ctx, item_id, required=True),
        timestamp=_moment(data, "timestamp", parse_offset_timestamp, ctx, item_id, required=True),
        missed_reason=_text(data, "missed_reason", ctx, item_id),
        observed=_text(data, "observed", ctx, item_id),
        expected_text=_text(data, "expected_text", ctx, item_id),
        notes=_text(data, "notes", ctx, item_id),
        rule_type=_text(data, "rule_type", ctx, item_id),
        confidence=_confidence(data, "confidence", ctx, item_id),
        coordinates=_nested(data, "coordinates", _build_pose, ctx, item_id),
        evidence_images=_strings(data, "evidence_images", ctx, item_id),
        annotated_images=_strings(data, "annotated_images", ctx, item_id),
        detections=tuple(
            f for f in (
                _finding(d, ctx, f"{item_id}.detections[{i}]")
                for i, d in enumerate(_objects(data, "detections", ctx, item_id))
            ) if f is not None
        ),
        sensor=_nested(data, "sensor", _build_sensor_block, ctx, item_id),
    )


def _alert(data: Mapping[str, Any], ctx: _Context, index: int) -> SensorAlert | None:
    """Read one threshold alert (2.5).

    Anomalies belong to the entry, never to `nearest_checkpoint_id`. That field
    is spatial proximity, not attribution: an alert near checkpoint 5 was not
    necessarily caused by anything at checkpoint 5.
    """
    where = f"sensor_alerts[{index}]"
    _known_keys(data, ("code", "label", "description", "severity", "timestamp", "coordinates",
                       "nearest_checkpoint_id", "nearest_checkpoint_name", "zone"), where, ctx)
    code = _text(data, "code", ctx, where, required=True)
    if code is None:
        ctx.anomaly(where, "entry", "dropped: no code to identify it by")
        return None
    return SensorAlert(
        code=code,
        severity=_text(data, "severity", ctx, where, required=True) or "",
        timestamp=_moment(data, "timestamp", parse_offset_timestamp, ctx, where, required=True),
        label=_text(data, "label", ctx, where),
        description=_text(data, "description", ctx, where),
        coordinates=_nested(data, "coordinates", _build_point, ctx, where),
        nearest_checkpoint_id=_text(data, "nearest_checkpoint_id", ctx, where),
        nearest_checkpoint_name=_text(data, "nearest_checkpoint_name", ctx, where),
        zone=_text(data, "zone", ctx, where),
    )


def _sample(data: Mapping[str, Any], ctx: _Context, index: int) -> SensorSample:
    """Read one continuous telemetry sample (2.6).

    Like an alert, a sample belongs to the run rather than to the checkpoint it
    happens to be nearest, so its anomalies are recorded against the entry.
    """
    where = f"sensor_samples[{index}]"
    _known_keys(data, ("timestamp", "status", "coordinates", "nearest_checkpoint_id",
                       "nearest_checkpoint_name", "zone", "sensor"), where, ctx)
    return SensorSample(
        timestamp=_moment(data, "timestamp", parse_offset_timestamp, ctx, where, required=True),
        status=_text(data, "status", ctx, where),
        coordinates=_nested(data, "coordinates", _build_point, ctx, where),
        nearest_checkpoint_id=_text(data, "nearest_checkpoint_id", ctx, where),
        nearest_checkpoint_name=_text(data, "nearest_checkpoint_name", ctx, where),
        zone=_text(data, "zone", ctx, where),
        sensor=_nested(data, "sensor", _build_sample_sensor, ctx, where),
    )


def _event(data: Mapping[str, Any], ctx: _Context, index: int) -> Event | None:
    """Read one event log entry (2.7).

    Run-level events omit checkpoint_id, checkpoint_name, result_status and
    evidence_count entirely; their absence is normal and not recorded. Where a
    checkpoint_id is present it is genuine attribution, so it owns the entry's
    anomalies.
    """
    where = f"event_log[{index}]"
    _known_keys(data, ("event_id", "event_type", "message", "status", "result_status",
                       "timestamp", "checkpoint_id", "checkpoint_name", "evidence_count"),
                where, ctx)
    item_id = _text(data, "checkpoint_id", ctx, where) or where
    event_id = _text(data, "event_id", ctx, item_id, required=True)
    event_type = _text(data, "event_type", ctx, item_id, required=True)
    if event_id is None or event_type is None:
        ctx.anomaly(where, "entry", "dropped: no event_id or event_type to identify it by")
        return None
    return Event(
        event_id=event_id,
        event_type=event_type,
        timestamp=_moment(data, "timestamp", parse_offset_timestamp, ctx, item_id, required=True),
        message=_text(data, "message", ctx, item_id),
        status=_text(data, "status", ctx, item_id),
        result_status=_text(data, "result_status", ctx, item_id),
        checkpoint_id=_text(data, "checkpoint_id", ctx, item_id),
        checkpoint_name=_text(data, "checkpoint_name", ctx, item_id),
        evidence_count=_whole(data, "evidence_count", ctx, item_id),
    )


def _counts(data: Mapping[str, Any], ctx: _Context) -> dict[str, int | None]:
    """Read the seven declared counts as claims, not facts (2.9).

    They are recorded as given. Cross-checking them against the arrays is
    derive.py's job, and printing both figures where they disagree is 3.3's.
    """
    return {key: _whole(data, key, ctx, RUN_LEVEL, required=True) for key in _COUNT_FIELDS}


def _duplicate_ids(checkpoints: Sequence[Checkpoint], ctx: _Context) -> None:
    """Record any checkpoint_id used more than once (TA-28).

    Reported, never deduplicated: two checkpoints sharing an id are two
    checkpoints, and collapsing them would hide one from the report entirely.
    """
    seen: dict[str, int] = {}
    for checkpoint in checkpoints:
        seen[checkpoint.checkpoint_id] = seen.get(checkpoint.checkpoint_id, 0) + 1
    for item_id, count in seen.items():
        if count > 1:
            ctx.anomaly(item_id, "checkpoint_id",
                        f"used by {count} checkpoints in one run; all are kept")


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def load_record(path: Path) -> Record:
    """Parse and validate a run record.

    Raises RecordParseError on structural failure, with a message naming the
    file and the specific problem. Any other disagreement with section 2 is
    recorded on `Record.anomalies` and the record still loads, so that a
    degraded input produces a report saying what is wrong with it.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise RecordParseError(f"{path}: cannot be read: {error.strerror or error}") from error
    except UnicodeDecodeError as error:
        raise RecordParseError(f"{path}: is not UTF-8 text: {error}") from error

    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise RecordParseError(
            f"{path}: is not valid JSON: {error.msg} at line {error.lineno} column {error.colno}"
        ) from error

    if not isinstance(data, Mapping):
        raise RecordParseError(
            f"{path}: root is {type(data).__name__}, expected a JSON object describing one run"
        )

    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise RecordParseError(
            f"{path}: run_id is {run_id!r}; without it the output files cannot be named"
        )

    ctx = _Context(path)
    _known_keys(data, ("run_id", "facility_id", "facility_name", "start_time", "end_time",
                       "duration", "run_status", "final_status", "progress_percentage", "locked",
                       "current_checkpoint_id", "checkpoints", "findings", "sensor_alerts",
                       "sensor_samples", "event_log", "live_detections", *_COUNT_FIELDS),
                RUN_LEVEL, ctx)

    checkpoints = tuple(
        c for c in (
            _checkpoint(entry, ctx, index)
            for index, entry in enumerate(_objects(data, "checkpoints", ctx, RUN_LEVEL))
        ) if c is not None
    )
    _duplicate_ids(checkpoints, ctx)
    if not checkpoints:
        logger.warning("%s: no checkpoints recorded", path.name)

    record = Record(
        run_id=run_id.strip(),
        facility_id=_text(data, "facility_id", ctx, RUN_LEVEL, required=True) or "",
        facility_name=_text(data, "facility_name", ctx, RUN_LEVEL, required=True) or "",
        run_status=_text(data, "run_status", ctx, RUN_LEVEL, required=True) or "",
        final_status=_text(data, "final_status", ctx, RUN_LEVEL, required=True) or "",
        start_time=_moment(data, "start_time", parse_offset_timestamp, ctx, RUN_LEVEL,
                           required=True),
        end_time=_moment(data, "end_time", parse_offset_timestamp, ctx, RUN_LEVEL),
        duration=_text(data, "duration", ctx, RUN_LEVEL),
        progress_percentage=_whole(data, "progress_percentage", ctx, RUN_LEVEL, required=True),
        locked=_flag(data, "locked", ctx, RUN_LEVEL),
        current_checkpoint_id=_text(data, "current_checkpoint_id", ctx, RUN_LEVEL),
        **_counts(data, ctx),
        checkpoints=checkpoints,
        findings=tuple(
            f for f in (
                _finding(entry, ctx, f"findings[{i}]")
                for i, entry in enumerate(_objects(data, "findings", ctx, RUN_LEVEL))
            ) if f is not None
        ),
        sensor_alerts=tuple(
            a for a in (
                _alert(entry, ctx, i)
                for i, entry in enumerate(_objects(data, "sensor_alerts", ctx, RUN_LEVEL))
            ) if a is not None
        ),
        sensor_samples=tuple(
            _sample(entry, ctx, i)
            for i, entry in enumerate(_objects(data, "sensor_samples", ctx, RUN_LEVEL))
        ),
        event_log=tuple(
            e for e in (
                _event(entry, ctx, i)
                for i, entry in enumerate(_objects(data, "event_log", ctx, RUN_LEVEL))
            ) if e is not None
        ),
        live_detections=tuple(
            f for f in (
                _finding(entry, ctx, f"live_detections[{i}]")
                for i, entry in enumerate(_objects(data, "live_detections", ctx, RUN_LEVEL))
            ) if f is not None
        ),
        anomalies=tuple(ctx.anomalies),
        source_path=path,
    )
    logger.info(
        "%s: loaded %d checkpoints, %d findings, %d alerts, %d samples, %d events, "
        "%d anomalies, %d structures with undescribed keys",
        path.name, len(record.checkpoints), len(record.findings), len(record.sensor_alerts),
        len(record.sensor_samples), len(record.event_log), len(record.anomalies),
        len(ctx.unknown),
    )
    return record
