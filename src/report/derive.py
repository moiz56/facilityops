"""Every figure the report prints: the reconciliation counts, the per-zone
telemetry rollups, and the alert counts by severity.

Counting happens here and nowhere else. That is what keeps the coverage page,
the zone table, the manifest and the COUNT_MISMATCH gap from ever giving two
answers to the same question.

The seven counts are the two halves of the coverage page. Declared is what the
robot reported, copied verbatim and never corrected. Computed is what the
arrays actually hold. Where a pair disagrees the report prints both and flags
it, which is gaps.py's job, not this module's.

Nothing here formats. A mean is left unrounded, and a figure that could not be
worked out is left carrying the reason it could not, so a template reads fields
instead of deciding anything for itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from common.paths import CONFIG_PATH, load_config, setting
from common.schema import Checkpoint, Record, SensorSample

_config = load_config(CONFIG_PATH)

#: raw device flag -> the sensor block it governs. The block names are the
#: attribute names on both SensorBlock and SampleSensor, which is what lets a
#: checkpoint's flag decide whether a sample's block can be read.
SUBSYSTEM_FLAGS: dict[str, str] = setting(_config, "sensor", "subsystem_flags")


@dataclass(frozen=True)
class Counts:
    """The seven counts of the reconciliation table, named as the manifest names them.

    Declared counts can be None, because a record is allowed to leave one out.
    Computed counts never are.
    """

    required: int | None
    completed: int | None
    passed: int | None
    failed: int | None
    missed: int | None
    warned: int | None
    findings: int | None


def declared_counts(record: Record) -> Counts:
    """What the record claims. Copied as written, never corrected."""
    return Counts(
        required=record.total_required_checkpoints,
        completed=record.total_completed_checkpoints,
        passed=record.passed_checkpoints,
        failed=record.failed_checkpoints,
        missed=record.missed_checkpoints,
        warned=record.warned_checkpoints,
        findings=record.finding_count,
    )


def was_warned(checkpoint: Checkpoint) -> bool:
    """Whether anything about this checkpoint amounts to a warning.

    Two things do, and either is enough. The verdict `WARN` is one. A
    non-empty `sensor.warnings` array is the other: the hub raised a threshold
    warning there, whatever verdict the checkpoint was given afterwards.

    Counting only the verdict is what let the reference run pass reconciliation
    while contradicting itself. 2.9 names that case exactly - warned_checkpoints
    declared 0 against six of eight checkpoints carrying warnings - and says the
    contradiction must appear in the report. It could not, while the figure the
    declaration was compared against counted only verdicts and came back 0 too
    (decision 176).

    The warnings are read as recorded, without asking whether the reading they
    came with was trustworthy. A warning is not a measurement: it says the hub
    flagged something, and that it was flagged is true whether or not the
    numbers beside it can be used.
    """
    return checkpoint.result_status == "WARN" or bool(
        checkpoint.sensor is not None and checkpoint.sensor.warnings
    )


def computed_counts(record: Record) -> Counts:
    """What the arrays actually contain."""
    checkpoints = record.checkpoints
    return Counts(
        required=len(checkpoints),
        completed=sum(1 for c in checkpoints if c.status == "COMPLETED"),
        passed=sum(1 for c in checkpoints if c.result_status == "PASS"),
        failed=sum(1 for c in checkpoints if c.result_status == "FAIL"),
        missed=sum(1 for c in checkpoints if c.status == "MISSED"),
        warned=sum(1 for c in checkpoints if was_warned(c)),
        findings=len(record.findings),
    )


def alert_counts(record: Record) -> dict[str, int]:
    """How many sensor alerts of each severity the run logged.

    Severity is whatever the record says. An unlisted one is counted under its
    own name rather than folded into a known one.
    """
    counts: dict[str, int] = {}
    for alert in record.sensor_alerts:
        counts[alert.severity] = counts.get(alert.severity, 0) + 1
    return dict(sorted(counts.items()))


# --- the zone telemetry table ----------------------------------------------
#
# 3.4 is one row per zone: how many samples it recorded, the min, mean and max
# of its temperature, humidity and vibration RMS, and how many alerts named it.
# Individual samples are never rendered (2.6).
#
# Particulate has no column. 2.6 names the three measurements the report shows,
# 3.4's table lists those three, and a fourth nobody asked for would be an
# unrequested feature (10). The placeholder zeros an offline particulate sensor
# writes are therefore never summarised at all.


@dataclass(frozen=True)
class ZoneStat:
    """One measurement's min, mean and max across a zone's samples.

    Three different things leave a stat with no numbers, and a blank cell that
    cannot say which would be the confident wrong answer 0 warns about:

    - the zone recorded no samples, so `used` is 0 and nothing was suppressed
    - the device was offline, so `suppressed_by` names the flag that said so
    - samples were taken but none carried this value, so `used` is 0 again
      while the zone's sample count is not

    A template tells the three apart by reading `used`, `suppressed_by` and the
    row's sample count. It works nothing out for itself.
    """

    minimum: float | None = None
    mean: float | None = None
    maximum: float | None = None
    used: int = 0
    suppressed_by: str | None = None


def summarise(values: Sequence[float]) -> ZoneStat:
    """The min, mean and max of the values that were actually recorded.

    No values gives an empty stat rather than zeros: nothing was measured, and
    0.0 would read as a measurement.
    """
    if not values:
        return ZoneStat()
    return ZoneStat(
        minimum=min(values),
        mean=sum(values) / len(values),
        maximum=max(values),
        used=len(values),
    )


def sample_values(samples: Sequence[SensorSample], block: str, name: str) -> list[float]:
    """Every recorded value of one measurement, across the given samples.

    A sample with no reading, without that block, or without that value is left
    out. It is not counted as a zero, which would pull a mean towards a number
    nobody recorded.
    """
    values = []
    for sample in samples:
        reading = getattr(sample.sensor, block, None) if sample.sensor else None
        value = getattr(reading, name, None) if reading else None
        if value is not None:
            values.append(value)
    return values


def offline_blocks(checkpoints: Sequence[Checkpoint]) -> dict[str, str]:
    """Which sensor blocks these checkpoints report as offline, and the flag that said so.

    A flag disqualifies its block only when it actually says false. An absent
    flag claims nothing and suppresses nothing, which is how gaps.py reads the
    same flags.

    A sample carries no device flags of its own - 2.6 says its sensor block has
    no `raw` - so the only thing that can say a device was offline is the
    checkpoints standing in that zone. TA-11 is the case: a checkpoint whose
    adxl345_ok is false keeps its vibration figures out of the rollup.

    One checkpoint reporting a device offline suppresses the block for the whole
    zone. 2.2 makes `zone` a grouping key and never promises one checkpoint per
    zone, so a zone can hold several that disagree; a mean that mixes a real
    reading with a placeholder is the confident wrong answer 0 rules out.
    """
    offline = {}
    for checkpoint in checkpoints:
        raw = checkpoint.sensor.raw if checkpoint.sensor else None
        for flag, block in SUBSYSTEM_FLAGS.items():
            if getattr(raw, flag, None) is False:
                offline[block] = flag
    return offline


def zone_stat(
    samples: Sequence[SensorSample], offline: dict[str, str], block: str, name: str
) -> ZoneStat:
    """One measurement's stat for a zone, or the reason it has none.

    A suppressed measurement is never summarised. 3.4 is explicit that
    statistics are not computed over placeholder zeros, so the values are not
    read at all rather than read and then hidden.
    """
    flag = offline.get(block)
    if flag is not None:
        return ZoneStat(suppressed_by=flag)
    return summarise(sample_values(samples, block, name))


@dataclass(frozen=True)
class ZoneRow:
    """One row of the zone telemetry table, as 3.4 lays it out.

    `zone` is None for the row holding samples and alerts that recorded no zone
    at all. They are kept rather than dropped: 11 requires an optional field to
    be supported wherever it is absent, and a table that quietly loses a
    zone's worth of telemetry would not say it had.

    `samples` counts every sample in the zone, including any that carried no
    usable value, so it says how much telemetry the zone recorded rather than
    how much of it could be summarised.
    """

    zone: str | None
    samples: int
    temperature_c: ZoneStat
    humidity_pct: ZoneStat
    vibration_rms_g: ZoneStat
    alerts: int


def zone_order(record: Record) -> list[str | None]:
    """Every zone the table has a row for, in the order it renders them.

    Zones the route defined come first, in route order, then any zone only a
    sample or an alert mentions, then the unzoned row last if there is anything
    in it.
    """
    zones: list[str | None] = []
    unzoned = False

    for checkpoint in record.checkpoints:
        if checkpoint.zone not in zones:
            zones.append(checkpoint.zone)

    for item in (*record.sensor_samples, *record.sensor_alerts):
        if item.zone is None:
            unzoned = True
        elif item.zone not in zones:
            zones.append(item.zone)

    if unzoned:
        zones.append(None)
    return zones


def zone_row(record: Record, zone: str | None) -> ZoneRow:
    """The one row for a single zone.

    A zone that recorded no samples still gets a row, with a sample count of
    zero. The route defined it, and a table that left it out would not say so.
    """
    samples = [sample for sample in record.sensor_samples if sample.zone == zone]
    offline = offline_blocks([c for c in record.checkpoints if c.zone == zone])

    return ZoneRow(
        zone=zone,
        samples=len(samples),
        temperature_c=zone_stat(samples, offline, "environment", "temperature_c"),
        humidity_pct=zone_stat(samples, offline, "environment", "humidity_pct"),
        vibration_rms_g=zone_stat(samples, offline, "accelerometer", "vibration_rms_g"),
        alerts=sum(1 for alert in record.sensor_alerts if alert.zone == zone),
    )


def zone_rows(record: Record) -> tuple[ZoneRow, ...]:
    """Every row of the zone telemetry table."""
    return tuple(zone_row(record, zone) for zone in zone_order(record))


# --- everything above, for one run -----------------------------------------


@dataclass(frozen=True)
class DerivedValues:
    """Every figure one run's report prints.

    The coverage page, the zone table, the alerts section and the manifest all
    read from here, so none of them can print a different number for the same
    thing.
    """

    declared: Counts
    computed: Counts
    zones: tuple[ZoneRow, ...]
    alerts_by_severity: dict[str, int]


def derive_report_values(record: Record) -> DerivedValues:
    """Work out every figure the report prints, for one run."""
    return DerivedValues(
        declared=declared_counts(record),
        computed=computed_counts(record),
        zones=zone_rows(record),
        alerts_by_severity=alert_counts(record),
    )
