"""Read a run record JSON file into a Record.

Three rules:

1. Absent, null, "" and [] all mean the same thing: None, or () for a list.
2. Nothing is converted. A field of the wrong type is left unset and recorded as
   a FieldAnomaly, so a confidence written as the string "0.94" never reaches
   the report as a number.
3. Only a broken file raises. RecordParseError is for a file that cannot be
   read, is not JSON, is not a JSON object, or has no run_id (the output files
   are named after it). Everything else loads, carrying its problems in
   Record.anomalies, so a bad record still produces a report saying what is
   wrong with it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from common.schema import (
    Accelerometer, Checkpoint, Environment, Event, FieldAnomaly, Finding,
    Particulate, Point2D, Pose, RawSensor, Record, SampleSensor, SensorAlert,
    SensorBlock, SensorSample, SensorWarning,
)

logger = logging.getLogger(__name__)

#: Owner for an anomaly in a run-level field rather than a checkpoint's.
RUN_LEVEL = "__run__"

#: The declared counts. Claims about the arrays, cross-checked later in derive.py.
COUNT_FIELDS = (
    "total_required_checkpoints", "total_completed_checkpoints", "passed_checkpoints",
    "failed_checkpoints", "missed_checkpoints", "warned_checkpoints", "finding_count",
)

#: Collects anomalies while one record is read.
Anomalies = list[FieldAnomaly]
class RecordParseError(Exception):
    """The record cannot be loaded at all. The message names the file and the problem."""


# --- timestamps -------------------------------------------------------------
# The record uses two formats and they are parsed separately. Both return None
# instead of raising, so a bad timestamp is something to report, not a crash.


def parse_offset_timestamp(text: str) -> datetime | None:
    """Parse '2026-07-28T14:41:20-0700'. Returns None if it has no UTC offset."""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None
    # An offset is required. Without one we would get a naive datetime, and
    # mixing naive and aware values raises when alerts are sorted by time.
    return parsed if parsed.tzinfo is not None else None


def parse_local_timestamp(text: str) -> datetime | None:
    """Parse a sensor received_at, '2026-07-28 14:41:26'. No timezone, so naive."""
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


# --- reading one value ------------------------------------------------------


def _blank(value: Any) -> bool:
    """Whether a value counts as absent: None, "", [], {}."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict)):
        return not value
    return False


def _get(
    data: Mapping[str, Any],
    key: str,
    kind: type | tuple[type, ...],
    item: str,
    anomalies: Anomalies,
    *,
    required: bool = False,
) -> Any:
    """Read data[key] if it is of the expected type, else None with an anomaly.

    Pass required=True for a field the record must have, so an absent one is
    recorded rather than passed over silently.

    Never converts. bool is rejected where a number is expected, since True is
    an int in Python and a flag written where a count belongs is a real mistake.
    """
    if key not in data or _blank(data[key]):
        if required:
            anomalies.append(FieldAnomaly(
            item_id=item, field_name=key, problem="required, but absent",
        ))
        return None

    value = data[key]
    expected = kind if isinstance(kind, tuple) else (kind,)
    wanted = " or ".join(t.__name__ for t in expected)

    if isinstance(value, bool) and bool not in expected:
        anomalies.append(FieldAnomaly(
            item_id=item, field_name=key,
            problem=f"is the boolean {value!r}, expected {wanted}",
        ))
        return None
    if not isinstance(value, expected):
        anomalies.append(FieldAnomaly(
            item_id=item, field_name=key,
            problem=f"is {type(value).__name__} {value!r}, expected {wanted}; left unset",
        ))
        return None

    if isinstance(value, str):
        return value.strip()
    # A bool can only reach here when bool was asked for, and float never is.
    if float in expected:
        return float(value)
    return value


def _time(
    data: Mapping[str, Any],
    key: str,
    parse: Callable[[str], datetime | None],
    item: str,
    anomalies: Anomalies,
    *,
    required: bool = False,
) -> datetime | None:
    """Read a timestamp with the given parser. Unparseable text is recorded as written."""
    text = _get(data, key, str, item, anomalies, required=required)
    if text is None:
        return None
    value = parse(text)
    if value is None:
        anomalies.append(FieldAnomaly(
            item_id=item, field_name=key,
            problem=f"timestamp {text!r} is not in the expected format",
        ))
    return value


def _confidence(data: Mapping[str, Any], item: str, anomalies: Anomalies) -> float | None:
    """Read a confidence. 0.0 means no confidence was produced, so it reads as None."""
    value = _get(data, "confidence", (int, float), item, anomalies)
    return None if value == 0.0 else value


# --- reading nested objects and arrays --------------------------------------


def _object(
    data: Mapping[str, Any],
    key: str,
    build: Callable[[Mapping[str, Any], str, Anomalies], Any],
    item: str,
    anomalies: Anomalies,
) -> Any:
    """Read one nested object with build(), or None when it is absent."""
    block = data.get(key)
    if _blank(block):
        return None
    if not isinstance(block, Mapping):
        anomalies.append(FieldAnomaly(
            item_id=item, field_name=key,
            problem=f"is {type(block).__name__}, expected an object",
        ))
        return None
    return build(block, item, anomalies)


def _entries(
    data: Mapping[str, Any], key: str, item: str, anomalies: Anomalies
) -> tuple[Any, ...]:
    """Read data[key] as an array, or () with an anomaly if it is not one."""
    value = data.get(key)
    if _blank(value):
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        anomalies.append(FieldAnomaly(
            item_id=item, field_name=key,
            problem=f"is {type(value).__name__}, expected an array",
        ))
        return ()
    return tuple(value)


def _array(
    data: Mapping[str, Any], key: str, item: str, anomalies: Anomalies
) -> tuple[Mapping[str, Any], ...]:
    """Read an array of objects, skipping and recording any entry that is not one."""
    kept = []
    for index, entry in enumerate(_entries(data, key, item, anomalies)):
        if isinstance(entry, Mapping):
            kept.append(entry)
        else:
            anomalies.append(FieldAnomaly(
                item_id=item, field_name=f"{key}[{index}]",
                problem=f"is {type(entry).__name__}, expected an object",
            ))
    return tuple(kept)


def _paths(
    data: Mapping[str, Any], key: str, item: str, anomalies: Anomalies
) -> tuple[str, ...]:
    """Read an array of path strings. Entries are stripped; anything else is recorded."""
    kept = []
    for index, entry in enumerate(_entries(data, key, item, anomalies)):
        if isinstance(entry, str) and entry.strip():
            kept.append(entry.strip())
        else:
            anomalies.append(FieldAnomaly(
                item_id=item, field_name=f"{key}[{index}]",
                problem=f"is {type(entry).__name__} {entry!r}, expected a path string",
            ))
    return tuple(kept)


# --- the pieces of a record -------------------------------------------------


def _pose(block: Mapping[str, Any], item: str, anomalies: Anomalies) -> Pose:
    """Checkpoint or finding coordinates: x, y, z, yaw."""
    return Pose(**{
        key: _get(block, key, (int, float), item, anomalies)
        for key in ("x", "y", "z", "yaw")
    })


def _point(block: Mapping[str, Any], item: str, anomalies: Anomalies) -> Point2D:
    """Alert or sample coordinates: x and y only. Kept separate from _pose on purpose."""
    return Point2D(**{
        key: _get(block, key, (int, float), item, anomalies) for key in ("x", "y")
    })


def _accelerometer(block: Mapping[str, Any], item: str, anomalies: Anomalies) -> Accelerometer:
    """The accelerometer block. Governed by raw.adxl345_ok."""
    return Accelerometer(**{
        key: _get(block, key, (int, float), item, anomalies)
        for key in ("accel_x", "accel_y", "accel_z", "vibration_peak", "vibration_rms_g")
    })


def _environment(block: Mapping[str, Any], item: str, anomalies: Anomalies) -> Environment:
    """The environment block. Governed by raw.bme680_ok."""
    return Environment(**{
        key: _get(block, key, (int, float), item, anomalies)
        for key in ("temperature_c", "humidity_pct", "pressure_hpa")
    })


def _particulate(block: Mapping[str, Any], item: str, anomalies: Anomalies) -> Particulate:
    """The particulate block. Governed by raw.sps30_ok.

    The values are read as written even when that flag is false. Whether they
    may be shown is decided in report/gaps.py, not here.
    """
    return Particulate(**{
        key: _get(block, key, (int, float), item, anomalies)
        for key in ("pm1_0", "pm2_5", "pm4_0", "pm10")
    })


def _warning(block: Mapping[str, Any], item: str, anomalies: Anomalies) -> SensorWarning:
    """One entry from a sensor block's warnings array."""
    return SensorWarning(**{
        key: _get(block, key, str, item, anomalies)
        for key in ("code", "label", "description", "severity")
    })


#: raw keys that carry real information. The rest duplicate the structured
#: blocks in flat form and are kept in RawSensor.extra, never read as values.
_RAW_KEYS = ("device", "adxl345_ok", "bme680_ok", "sps30_ok", "air_status",
             "vibration_status", "gas_kohms", "ts_ms", "received_at")


def _raw(block: Mapping[str, Any], item: str, anomalies: Anomalies) -> RawSensor:
    """The raw device block: three health flags, two status strings, and duplicates."""
    return RawSensor(
        device=_get(block, "device", str, item, anomalies),
        adxl345_ok=_get(block, "adxl345_ok", bool, item, anomalies),
        bme680_ok=_get(block, "bme680_ok", bool, item, anomalies),
        sps30_ok=_get(block, "sps30_ok", bool, item, anomalies),
        air_status=_get(block, "air_status", str, item, anomalies),
        vibration_status=_get(block, "vibration_status", str, item, anomalies),
        gas_kohms=_get(block, "gas_kohms", (int, float), item, anomalies),
        ts_ms=_get(block, "ts_ms", int, item, anomalies),
        received_at=_time(block, "received_at", parse_local_timestamp, item, anomalies),
        extra={k: v for k, v in block.items() if k not in _RAW_KEYS},
    )


def _sensor(block: Mapping[str, Any], item: str, anomalies: Anomalies) -> SensorBlock:
    """A checkpoint's full sensor hub reading."""
    return SensorBlock(
        ok=_get(block, "ok", bool, item, anomalies),
        status=_get(block, "status", str, item, anomalies),
        source=_get(block, "source", str, item, anomalies),
        sensor_hub_reachable=_get(block, "sensor_hub_reachable", bool, item, anomalies),
        age_seconds=_get(block, "age_seconds", (int, float), item, anomalies),
        timestamp=_time(block, "timestamp", parse_offset_timestamp, item, anomalies),
        received_at=_time(block, "received_at", parse_local_timestamp, item, anomalies),
        accelerometer=_object(block, "accelerometer", _accelerometer, item, anomalies),
        environment=_object(block, "environment", _environment, item, anomalies),
        particulate=_object(block, "particulate", _particulate, item, anomalies),
        warnings=tuple(
            _warning(w, item, anomalies)
            for w in _array(block, "warnings", item, anomalies)
        ),
        raw=_object(block, "raw", _raw, item, anomalies),
    )


def _sample_sensor(block: Mapping[str, Any], item: str, anomalies: Anomalies) -> SampleSensor:
    """A telemetry sample's sensor block: the three measurement blocks only."""
    return SampleSensor(
        accelerometer=_object(block, "accelerometer", _accelerometer, item, anomalies),
        environment=_object(block, "environment", _environment, item, anomalies),
        particulate=_object(block, "particulate", _particulate, item, anomalies),
    )


def _finding(data: Mapping[str, Any], where: str, anomalies: Anomalies) -> Finding | None:
    """One finding, or one checkpoint detection. They share a shape.

    Dropped only if it has no finding_id, since there would be nothing to
    identify it by. `where` locates the entry until a checkpoint_id is found.
    """
    item = _get(data, "checkpoint_id", str, where, anomalies) or where
    finding_id = _get(data, "finding_id", str, item, anomalies, required=True)
    if finding_id is None:
        anomalies.append(FieldAnomaly(
            item_id=where, field_name="entry", problem="dropped: no finding_id",
        ))
        return None

    return Finding(
        finding_id=finding_id,
        severity=_get(data, "severity", str, item, anomalies, required=True) or "",
        status=_get(data, "status", str, item, anomalies, required=True) or "",
        timestamp=_time(data, "timestamp", parse_offset_timestamp, item, anomalies, required=True),
        checkpoint_id=_get(data, "checkpoint_id", str, item, anomalies, required=True),
        run_id=_get(data, "run_id", str, item, anomalies),
        zone=_get(data, "zone", str, item, anomalies),
        feature=_get(data, "feature", str, item, anomalies),
        description=_get(data, "description", str, item, anomalies),
        evidence_image=_get(data, "evidence_image", str, item, anomalies),
        coordinates=_object(data, "coordinates", _pose, item, anomalies),
        recommended_action=_get(data, "recommended_action", str, item, anomalies),
    )


def _checkpoint(data: Mapping[str, Any], index: int, anomalies: Anomalies) -> Checkpoint | None:
    """One stop on the route. Dropped only if it has no checkpoint_id.

    status (COMPLETED or MISSED) and result_status (PASS, FAIL, WARN) are read
    independently; neither is derived from the other.
    """
    where = f"checkpoints[{index}]"
    item = _get(data, "checkpoint_id", str, where, anomalies)
    if item is None:
        anomalies.append(FieldAnomaly(
            item_id=where, field_name="entry", problem="dropped: no checkpoint_id",
        ))
        return None

    return Checkpoint(
        checkpoint_id=item,
        checkpoint_name=_get(data, "checkpoint_name", str, item, anomalies, required=True) or "",
        zone=_get(data, "zone", str, item, anomalies, required=True) or "",
        status=_get(data, "status", str, item, anomalies, required=True) or "",
        result_status=_get(data, "result_status", str, item, anomalies, required=True) or "",
        sequence_number=_get(data, "sequence_number", int, item, anomalies, required=True),
        timestamp=_time(data, "timestamp", parse_offset_timestamp, item, anomalies, required=True),
        missed_reason=_get(data, "missed_reason", str, item, anomalies),
        observed=_get(data, "observed", str, item, anomalies),
        expected_text=_get(data, "expected_text", str, item, anomalies),
        notes=_get(data, "notes", str, item, anomalies),
        rule_type=_get(data, "rule_type", str, item, anomalies),
        confidence=_confidence(data, item, anomalies),
        coordinates=_object(data, "coordinates", _pose, item, anomalies),
        evidence_images=_paths(data, "evidence_images", item, anomalies),
        annotated_images=_paths(data, "annotated_images", item, anomalies),
        detections=tuple(
            f for f in (
                _finding(d, f"{item}.detections[{i}]", anomalies)
                for i, d in enumerate(_array(data, "detections", item, anomalies))
            ) if f is not None
        ),
        sensor=_object(data, "sensor", _sensor, item, anomalies),
    )


def _alert(data: Mapping[str, Any], index: int, anomalies: Anomalies) -> SensorAlert | None:
    """One threshold alert. Dropped only if it has no code.

    Anomalies belong to the entry, never to nearest_checkpoint_id: that field is
    proximity, not attribution.
    """
    where = f"sensor_alerts[{index}]"
    code = _get(data, "code", str, where, anomalies, required=True)
    if code is None:
        anomalies.append(FieldAnomaly(
            item_id=where, field_name="entry", problem="dropped: no code",
        ))
        return None

    return SensorAlert(
        code=code,
        severity=_get(data, "severity", str, where, anomalies, required=True) or "",
        timestamp=_time(data, "timestamp", parse_offset_timestamp, where, anomalies, required=True),
        label=_get(data, "label", str, where, anomalies),
        description=_get(data, "description", str, where, anomalies),
        coordinates=_object(data, "coordinates", _point, where, anomalies),
        nearest_checkpoint_id=_get(data, "nearest_checkpoint_id", str, where, anomalies),
        nearest_checkpoint_name=_get(data, "nearest_checkpoint_name", str, where, anomalies),
        zone=_get(data, "zone", str, where, anomalies),
    )


def _sample(data: Mapping[str, Any], index: int, anomalies: Anomalies) -> SensorSample:
    """One telemetry sample. Never dropped; samples are only ever rolled up per zone."""
    where = f"sensor_samples[{index}]"
    return SensorSample(
        timestamp=_time(data, "timestamp", parse_offset_timestamp, where, anomalies, required=True),
        status=_get(data, "status", str, where, anomalies),
        coordinates=_object(data, "coordinates", _point, where, anomalies),
        nearest_checkpoint_id=_get(data, "nearest_checkpoint_id", str, where, anomalies),
        nearest_checkpoint_name=_get(data, "nearest_checkpoint_name", str, where, anomalies),
        zone=_get(data, "zone", str, where, anomalies),
        sensor=_object(data, "sensor", _sample_sensor, where, anomalies),
    )


def _event(data: Mapping[str, Any], index: int, anomalies: Anomalies) -> Event | None:
    """One event log entry. Dropped only if it has no event_id or event_type.

    Run-level events omit checkpoint_id, checkpoint_name, result_status and
    evidence_count entirely, which is normal and not recorded.
    """
    where = f"event_log[{index}]"
    item = _get(data, "checkpoint_id", str, where, anomalies) or where
    event_id = _get(data, "event_id", str, item, anomalies, required=True)
    event_type = _get(data, "event_type", str, item, anomalies, required=True)
    if event_id is None or event_type is None:
        anomalies.append(FieldAnomaly(
            item_id=where, field_name="entry",
            problem="dropped: no event_id or event_type",
        ))
        return None

    return Event(
        event_id=event_id,
        event_type=event_type,
        timestamp=_time(data, "timestamp", parse_offset_timestamp, item, anomalies, required=True),
        message=_get(data, "message", str, item, anomalies),
        status=_get(data, "status", str, item, anomalies),
        result_status=_get(data, "result_status", str, item, anomalies),
        checkpoint_id=_get(data, "checkpoint_id", str, item, anomalies),
        checkpoint_name=_get(data, "checkpoint_name", str, item, anomalies),
        evidence_count=_get(data, "evidence_count", int, item, anomalies),
    )


def _record_duplicate_ids(checkpoints: Sequence[Checkpoint], anomalies: Anomalies) -> None:
    """Record any checkpoint_id used more than once. Reported, never deduplicated."""
    counts: dict[str, int] = {}
    for checkpoint in checkpoints:
        counts[checkpoint.checkpoint_id] = counts.get(checkpoint.checkpoint_id, 0) + 1
    for item, count in counts.items():
        if count > 1:
            anomalies.append(FieldAnomaly(
                item_id=item, field_name="checkpoint_id",
                problem=f"used by {count} checkpoints in one run; all are kept",
            ))


# --- the entry point --------------------------------------------------------


def _read_json(path: Path) -> Mapping[str, Any]:
    """Read the file as a JSON object, or raise RecordParseError naming the problem."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise RecordParseError(f"{path}: cannot be read: {error.strerror or error}") from None
    except UnicodeDecodeError as error:
        raise RecordParseError(f"{path}: is not UTF-8 text: {error}") from None

    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise RecordParseError(
            f"{path}: is not valid JSON: {error.msg} at line {error.lineno} column {error.colno}"
        ) from None

    if not isinstance(data, Mapping):
        raise RecordParseError(
            f"{path}: root is {type(data).__name__}, expected a JSON object describing one run"
        )
    return data


def load_record(path: Path) -> Record:
    """Read a run record file into a Record.

    Raises RecordParseError only for a file that cannot be used at all. Any
    other disagreement with the data contract is recorded in Record.anomalies
    and the record still loads.
    """
    data = _read_json(path)

    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise RecordParseError(
            f"{path}: run_id is {run_id!r}; without it the output files cannot be named"
        )

    anomalies: Anomalies = []

    checkpoints = tuple(
        c for c in (
            _checkpoint(entry, index, anomalies)
            for index, entry in enumerate(_array(data, "checkpoints", RUN_LEVEL, anomalies))
        ) if c is not None
    )
    _record_duplicate_ids(checkpoints, anomalies)
    if not checkpoints:
        logger.warning("%s: no checkpoints recorded", path.name)

    record = Record(
        run_id=run_id.strip(),
        facility_id=_get(data, "facility_id", str, RUN_LEVEL, anomalies, required=True) or "",
        facility_name=_get(data, "facility_name", str, RUN_LEVEL, anomalies, required=True) or "",
        run_status=_get(data, "run_status", str, RUN_LEVEL, anomalies, required=True) or "",
        final_status=_get(data, "final_status", str, RUN_LEVEL, anomalies, required=True) or "",
        start_time=_time(
            data, "start_time", parse_offset_timestamp, RUN_LEVEL, anomalies, required=True,
        ),
        end_time=_time(data, "end_time", parse_offset_timestamp, RUN_LEVEL, anomalies),
        duration=_get(data, "duration", str, RUN_LEVEL, anomalies),
        progress_percentage=_get(
            data, "progress_percentage", int, RUN_LEVEL, anomalies, required=True,
        ),
        locked=_get(data, "locked", bool, RUN_LEVEL, anomalies),
        current_checkpoint_id=_get(data, "current_checkpoint_id", str, RUN_LEVEL, anomalies),
        **{
            key: _get(data, key, int, RUN_LEVEL, anomalies, required=True)
            for key in COUNT_FIELDS
        },
        checkpoints=checkpoints,
        findings=tuple(
            f for f in (
                _finding(entry, f"findings[{i}]", anomalies)
                for i, entry in enumerate(_array(data, "findings", RUN_LEVEL, anomalies))
            ) if f is not None
        ),
        sensor_alerts=tuple(
            a for a in (
                _alert(entry, i, anomalies)
                for i, entry in enumerate(_array(data, "sensor_alerts", RUN_LEVEL, anomalies))
            ) if a is not None
        ),
        sensor_samples=tuple(
            _sample(entry, i, anomalies)
            for i, entry in enumerate(_array(data, "sensor_samples", RUN_LEVEL, anomalies))
        ),
        event_log=tuple(
            e for e in (
                _event(entry, i, anomalies)
                for i, entry in enumerate(_array(data, "event_log", RUN_LEVEL, anomalies))
            ) if e is not None
        ),
        live_detections=tuple(
            f for f in (
                _finding(entry, f"live_detections[{i}]", anomalies)
                for i, entry in enumerate(_array(data, "live_detections", RUN_LEVEL, anomalies))
            ) if f is not None
        ),
        anomalies=tuple(anomalies),
        source_path=path,
    )

    logger.info(
        "%s: loaded %d checkpoints, %d findings, %d alerts, %d samples, %d events, %d anomalies",
        path.name, len(record.checkpoints), len(record.findings), len(record.sensor_alerts),
        len(record.sensor_samples), len(record.event_log), len(record.anomalies),
    )
    return record
