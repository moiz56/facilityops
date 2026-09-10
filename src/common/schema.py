"""Dataclasses for section 2 of the brief: the run record data contract.

Shape only. No properties, no methods, no logging. Parsing and every reading of
the data live in loader.py, evidence path rewriting in paths.py, and the trust
rules in report/gaps.py, which section 5.4 requires to be the one place that
decides what a gap is.

Where each type comes from:

    2.1   Record                          the whole run
    2.2   Checkpoint, Pose                one stop on the route
    2.4   Finding                         something the run found
    2.5   SensorAlert, Point2D            a threshold breach during the run
    2.6   SensorSample, SampleSensor      continuous telemetry
    2.7   Event                           the run's own log
    2.8   SensorBlock, RawSensor,         one sensor hub reading
          SensorWarning, Accelerometer,
          Environment, Particulate

          
Two rules apply throughout:

- Every type is frozen and every sequence is a tuple. Section 5.2 requires that
  no pipeline stage mutates its input.
- Status, verdict and severity fields are plain strings, never Enums. Section
  11 requires an unlisted value to be rendered verbatim and recorded, never
  mapped onto a known one. GapType, in report/gaps.py, is the one closed set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    # coordinates
    "Pose", "Point2D",
    # the pieces of a sensor reading
    "Accelerometer", "Environment", "Particulate", "SensorWarning", "RawSensor",
    # the two sensor readings
    "SensorBlock", "SampleSensor",
    # what a run is made of
    "Checkpoint", "Finding", "SensorAlert", "SensorSample", "Event",
    # the run
    "Record",
    # the engine's own
    "FieldAnomaly",
]


# ---------------------------------------------------------------------------
# Coordinates
#
# Two shapes, deliberately two types. Section 2.5 warns that one parser must
# not assume all four keys, so loader.py reads them with separate functions.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Pose:
    """Where a checkpoint or a finding is: x, y, z and yaw in the map frame (2.2)."""

    x: float | None = None
    y: float | None = None
    z: float | None = None
    yaw: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Point2D:
    """Where a sensor alert or a telemetry sample is: x and y only (2.5).

    Alerts and samples record two keys where checkpoints record four. There is
    no z and no yaw, and none is invented.
    """

    x: float | None = None
    y: float | None = None


# ---------------------------------------------------------------------------
# The pieces of a sensor reading (2.8)
#
# Each measurement block is governed by a device health flag in RawSensor. When
# a flag is false the block's values are placeholders, not measurements - the
# rule that decides this is in report/gaps.py, not here.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Accelerometer:
    """Movement and vibration. Governed by adxl345_ok."""

    accel_x: float | None = None
    accel_y: float | None = None
    accel_z: float | None = None
    vibration_peak: float | None = None
    vibration_rms_g: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Environment:
    """Temperature, humidity and pressure. Governed by bme680_ok."""

    temperature_c: float | None = None
    humidity_pct: float | None = None
    pressure_hpa: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Particulate:
    """Airborne particulate counts. Governed by sps30_ok.

    In the reference run sps30_ok is false at every checkpoint and every value
    here reads 0.0. Those zeros are an offline sensor, not clean air.
    """

    pm1_0: float | None = None
    pm2_5: float | None = None
    pm4_0: float | None = None
    pm10: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SensorWarning:
    """A threshold warning the sensor hub raised at one checkpoint."""

    code: str | None = None
    label: str | None = None
    description: str | None = None
    severity: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RawSensor:
    """The device block: health flags, and duplicates of everything else.

    Only five fields here are usable: the three device health flags, plus
    air_status and vibration_status. Everything else duplicates the structured
    blocks in flat form, and those blocks are the source of truth (2.8). The
    duplicates are kept in `extra` so nothing in the record is lost, but no
    code reads a measurement from them.
    """

    device: str | None = None
    adxl345_ok: bool | None = None      # governs Accelerometer
    bme680_ok: bool | None = None       # governs Environment
    sps30_ok: bool | None = None        # governs Particulate
    air_status: str | None = None
    vibration_status: str | None = None
    gas_kohms: float | None = None
    ts_ms: int | None = None
    received_at: datetime | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The two sensor readings
#
# A checkpoint's reading and a sample's reading are different shapes, and are
# kept as different types. Section 2.6 warns against reusing one for both.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class SensorBlock:
    """What the sensor hub reported at one checkpoint (2.8).

    `ok`, `status` and `sensor_hub_reachable` say whether the reading can be
    trusted at all; `age_seconds` says how stale it was when recorded.
    """

    ok: bool | None = None
    status: str | None = None
    source: str | None = None
    sensor_hub_reachable: bool | None = None
    age_seconds: float | None = None
    timestamp: datetime | None = None
    received_at: datetime | None = None
    accelerometer: Accelerometer | None = None
    environment: Environment | None = None
    particulate: Particulate | None = None
    warnings: tuple[SensorWarning, ...] = ()
    raw: RawSensor | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SampleSensor:
    """What one telemetry sample carries (2.6): the three measurement blocks only.

    No raw, no warnings, no ok, no age_seconds. Because it has no device flags,
    the sub-device trust rule cannot be asked of a sample - which is the point
    of keeping it a separate type from SensorBlock.
    """

    accelerometer: Accelerometer | None = None
    environment: Environment | None = None
    particulate: Particulate | None = None


# ---------------------------------------------------------------------------
# What a run is made of
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Checkpoint:
    """One stop on the route: what the robot saw there and what it recorded (2.2).

    `status` says whether the robot got there (COMPLETED or MISSED).
    `result_status` is the verdict there (PASS, FAIL or WARN). They are
    independent: COMPLETED and FAIL is the most common failure in this data,
    and COMPLETED with no evidence at all is different again from MISSED.

    `confidence` is None when no confidence was produced. Section 2.9 gives 0.0
    that meaning, and loader.py applies it, so the report renders "not scored"
    for both.
    """

    checkpoint_id: str
    checkpoint_name: str
    zone: str
    status: str
    result_status: str
    sequence_number: int | None = None
    timestamp: datetime | None = None
    missed_reason: str | None = None        # populated when status is MISSED
    observed: str | None = None             # normal_scene, no_evidence, or a label
    expected_text: str | None = None
    notes: str | None = None                # free text, rendered verbatim (2.9)
    rule_type: str | None = None
    confidence: float | None = None
    coordinates: Pose | None = None
    evidence_images: tuple[str, ...] = ()   # paths as recorded; paths.py rewrites them
    annotated_images: tuple[str, ...] = ()
    detections: tuple[Finding, ...] = ()    # same shape as a run-level finding
    sensor: SensorBlock | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Finding:
    """Something the run found, tied to the checkpoint it came from (2.4).

    Also the shape of a checkpoint's `detections` and of `live_detections`.

    `severity` is info, warning or fail - lowercase, unlike the status enums
    elsewhere. `status` is logged, acknowledged or abstained; an abstained
    finding is one the detector declined to call, and is shown as requiring
    human review rather than as a confirmed finding, and never dropped.
    """

    finding_id: str
    severity: str
    status: str
    timestamp: datetime | None = None
    checkpoint_id: str | None = None
    run_id: str | None = None
    zone: str | None = None
    feature: str | None = None
    description: str | None = None
    evidence_image: str | None = None
    coordinates: Pose | None = None
    recommended_action: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SensorAlert:
    """A threshold breach logged during the run, independent of any checkpoint (2.5).

    `nearest_checkpoint_id` is spatial proximity, not attribution: an alert near
    checkpoint 5 was not necessarily caused by anything at checkpoint 5. It is
    rendered as "nearest checkpoint", never as "at checkpoint".
    """

    code: str
    severity: str                                   # warning or critical
    timestamp: datetime | None = None
    label: str | None = None
    description: str | None = None
    coordinates: Point2D | None = None
    nearest_checkpoint_id: str | None = None
    nearest_checkpoint_name: str | None = None
    zone: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SensorSample:
    """One continuous telemetry reading, roughly one every two seconds (2.6).

    Samples are never rendered individually. derive.py rolls them up per zone
    into the min, mean and max of the table in 3.4.
    """

    timestamp: datetime | None = None
    status: str | None = None
    coordinates: Point2D | None = None
    nearest_checkpoint_id: str | None = None
    nearest_checkpoint_name: str | None = None
    zone: str | None = None
    sensor: SampleSensor | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """One entry in the run's own log (2.7).

    Run-level events - run_started and run_completed - simply omit
    checkpoint_id, checkpoint_name, result_status and evidence_count. Those
    keys are absent rather than null.

    `evidence_count` is a declared count and must be cross-checked against the
    checkpoint's actual evidence_images; a mismatch is a gap (TA-25).
    """

    event_id: str
    event_type: str                          # run_started, checkpoint_completed, run_completed
    timestamp: datetime | None = None
    message: str | None = None
    status: str | None = None
    result_status: str | None = None
    checkpoint_id: str | None = None
    checkpoint_name: str | None = None
    evidence_count: int | None = None


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Record:
    """One inspection run: the root of the data contract (2.1).

    The seven count fields are declared, not derived. They are what the robot
    reported and may disagree with the arrays beside them - reconciling the two
    is derive.py's job, and printing both figures where they differ is 3.3's.

    `checkpoints` stays a tuple and is never keyed by checkpoint_id anywhere in
    the pipeline. Duplicate ids occur, and must be reported rather than
    silently collapsed into one (TA-28).
    """

    run_id: str                                     # names the output files (4.1)
    facility_id: str
    facility_name: str
    run_status: str                                 # RUNNING, COMPLETED, ABORTED
    final_status: str                               # PASS, FAIL, WARN
    start_time: datetime | None = None
    end_time: datetime | None = None                # absent if the run did not finish
    duration: str | None = None                     # HH:MM:SS, as recorded
    progress_percentage: int | None = None
    locked: bool | None = None
    current_checkpoint_id: str | None = None        # non-null only mid-run

    # Declared counts (2.1). Claims about the arrays below, not facts.
    total_required_checkpoints: int | None = None
    total_completed_checkpoints: int | None = None
    passed_checkpoints: int | None = None
    failed_checkpoints: int | None = None
    missed_checkpoints: int | None = None
    warned_checkpoints: int | None = None
    finding_count: int | None = None

    # The arrays themselves.
    checkpoints: tuple[Checkpoint, ...] = ()
    findings: tuple[Finding, ...] = ()
    sensor_alerts: tuple[SensorAlert, ...] = ()
    sensor_samples: tuple[SensorSample, ...] = ()
    event_log: tuple[Event, ...] = ()
    live_detections: tuple[Finding, ...] = ()

    # Added by the engine, not from section 2. See FieldAnomaly.
    anomalies: tuple[FieldAnomaly, ...] = ()
    source_path: Path | None = None


# ---------------------------------------------------------------------------
# The engine's own type
#
# Everything above mirrors section 2. This does not.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class FieldAnomaly:
    """A field that was there but could not be used as recorded.

    A confidence written as the string "0.94" is refused rather than converted,
    because a converted one would appear in the report as a measurement (TA-27).
    Refusing it silently would be worse, so the refusal travels here.

    Not a Gap: the ten gap types in 4.3 are a closed set and none of them means
    "wrong type". These are carried on Record.anomalies and rendered, but never
    appear in the manifest's gaps array.

    `problem` quotes the value as recorded, so the text of an unparseable
    timestamp or a refused number survives into the report.

    `item_id` is a checkpoint_id, or "__run__" for a run-level field, matching
    the convention 4.4 sets for gaps.
    """

    item_id: str
    field_name: str
    problem: str
