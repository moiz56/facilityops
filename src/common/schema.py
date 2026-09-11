"""The shape of a run record.

Shape only: no properties, no methods, no logging. Reading the data is
loader.py's job, rewriting evidence paths is paths.py's, and deciding what
counts as a gap is report/gaps.py's.

Two rules throughout:

- Every type is frozen and every sequence is a tuple, so no pipeline stage can
  change what it was given.
- Status, verdict and severity are plain strings, never Enums. A value nobody
  listed has to render as written rather than being mapped onto a known one.
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


# --- coordinates ---
#
# Two shapes, so two types: nothing should assume all four keys are there.


@dataclass(frozen=True, slots=True, kw_only=True)
class Pose:
    """Where a checkpoint or a finding is: x, y, z and yaw in the map frame."""

    x: float | None = None
    y: float | None = None
    z: float | None = None
    yaw: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Point2D:
    """Where an alert or a sample is: x and y only.

    Alerts and samples record two keys where checkpoints record four. No z and
    no yaw, and neither is invented.
    """

    x: float | None = None
    y: float | None = None


# ---------------------------------------------------------------------------
# The pieces of a sensor reading
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

    In the reference run that flag is false at every checkpoint and every value
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
    """The device block: three health flags, two status strings, and duplicates.

    Everything else here repeats the structured blocks in flat form, and those
    blocks are what to read. The duplicates are kept in `extra` so nothing is
    lost, but no measurement is ever taken from them.
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
    """What the sensor hub reported at one checkpoint.

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
    """A telemetry sample's reading: the three measurement blocks only.

    No raw, no warnings, no ok, no age_seconds. With no device flags there is
    no way to ask whether a sample can be trusted, which is why this is a
    separate type from SensorBlock.
    """

    accelerometer: Accelerometer | None = None
    environment: Environment | None = None
    particulate: Particulate | None = None


# ---------------------------------------------------------------------------
# What a run is made of
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Checkpoint:
    """One stop on the route.

    `status` says whether the robot got there (COMPLETED or MISSED).
    `result_status` is the verdict (PASS, FAIL or WARN). They are independent:
    COMPLETED with FAIL is the most common failure in this data, and COMPLETED
    with no evidence at all is different again from MISSED.

    `confidence` is None when none was produced. A recorded 0.0 means the same
    thing and the loader reads it as None, so both render as "not scored".
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
    notes: str | None = None                # free text, rendered verbatim
    rule_type: str | None = None
    confidence: float | None = None
    coordinates: Pose | None = None
    evidence_images: tuple[str, ...] = ()   # as recorded; paths.py rewrites them
    annotated_images: tuple[str, ...] = ()
    detections: tuple[Finding, ...] = ()    # same shape as a run-level finding
    sensor: SensorBlock | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Finding:
    """Something the run found, tied to the checkpoint it came from.

    Also the shape of a checkpoint's `detections` and of `live_detections`.

    `severity` is info, warning or fail - lowercase, unlike the status values
    elsewhere. `status` is logged, acknowledged or abstained. An abstained
    finding is one the detector would not call: it is shown as needing human
    review rather than as a confirmed finding, and never dropped.
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
    """A threshold breach logged during the run, independent of any checkpoint.

    `nearest_checkpoint_id` is proximity, not attribution: an alert near
    checkpoint 5 was not necessarily caused by anything at checkpoint 5. It
    renders as "nearest checkpoint", never as "at checkpoint".
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
    """One telemetry reading, roughly one every two seconds.

    Samples are never rendered one by one. derive.py rolls them up per zone
    into a min, mean and max.
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
    """One entry in the run's own log.

    Run-level events - run_started and run_completed - omit checkpoint_id,
    checkpoint_name, result_status and evidence_count entirely. Those keys are
    absent rather than null.

    `evidence_count` is a claim, to be checked against the checkpoint's actual
    evidence_images. A mismatch is a gap.
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
    """One inspection run.

    The seven count fields are declared, not derived: they are what the robot
    reported, and may disagree with the arrays beside them. Reconciling the two
    is derive.py's job, and both figures get printed where they differ.

    `checkpoints` stays a tuple and is never keyed by checkpoint_id here.
    Duplicate ids do occur, and have to be reported rather than collapsed into
    one.
    """

    run_id: str                                     # names the output files
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

    # Declared counts: claims about the arrays below, not facts.
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

    # Added by the engine, not read from the file. See FieldAnomaly.
    anomalies: tuple[FieldAnomaly, ...] = ()
    source_path: Path | None = None


# ---------------------------------------------------------------------------
# The engine's own type
#
# Everything above mirrors the record. This does not.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class FieldAnomaly:
    """A field that was there but could not be used as recorded.

    A confidence written as the string "0.94" is refused rather than converted,
    since a converted one would appear in the report as a measurement. Refusing
    it silently would be worse, so the refusal travels here.

    Not a Gap. The ten gap types are a closed set and none of them means "wrong
    type", so these ride on Record.anomalies and are rendered, but never appear
    in the manifest's gaps array.

    `problem` quotes the value as recorded, so the text of an unparseable
    timestamp or a refused number survives into the report.

    `item_id` is a checkpoint_id, or "__run__" for a run-level field.
    """

    item_id: str
    field_name: str
    problem: str
