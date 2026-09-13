"""Gap detection: the things a report has to say it does not have.

A Gap is one thing missing, stale, or contradictory in a run. Every gap shown
is found here, and every gap in the manifest is found here, so the
two can never disagree about what was wrong with a run.

That is why the rules live in this module and not in the templates. A template
that decided for itself whether a sensor reading could be trusted would spread
that decision across a dozen files, and none of it could be tested on its own.

"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence

from pathlib import PurePosixPath

from common.paths import CONFIG_PATH, ResolvedImage, load_config, setting
from common.schema import Checkpoint, Finding, Record, SensorBlock
from report.derive import SUBSYSTEM_FLAGS, computed_counts, declared_counts
from report.images import DirectionCell, build_direction_grid

__all__ = [
    "RUN_LEVEL", "GapType", "Gap",
    "MAX_AGE_SECONDS", "SUBSYSTEM_FLAGS", "unavailable_reason",
    "missed_checkpoint_gaps", "no_evidence_gaps", "missing_image_gaps",
    "missing_thermal_gaps", "sensor_unavailable_gaps", "subsystem_offline_gaps",
    "stale_reading_gaps", "no_findings_gaps", "count_mismatch_gaps",
    "empty_record_gaps", "checkpoint_gaps", "run_gaps", "detect_gaps",
]

#: item_id for a gap that belongs to the run itself rather than to a checkpoint.
RUN_LEVEL = "__run__"


class GapType(StrEnum):
    """The ten kinds of gap. A closed set: no others, ever.

    A StrEnum, so a gap type writes itself into the manifest as its own name
    and there is no conversion step to forget.
    """

    #: A referenced image does not resolve, is empty, or will not decode.
    MISSING_IMAGE = "MISSING_IMAGE"

    #: A direction has an RGB image and no thermal counterpart.
    #: Provisional, pending question 7 in decisions.md: this fires whether or
    #: not that RGB itself resolves, so a zero-byte RGB reports both this and
    #: MISSING_IMAGE. Read the other way, one broken file would suppress a
    #: second, unrelated gap.
    MISSING_THERMAL = "MISSING_THERMAL"

    #: evidence_images is empty, or observed is "no_evidence".
    NO_EVIDENCE = "NO_EVIDENCE"

    #: status is "MISSED" - the robot never reached the checkpoint.
    MISSED_CHECKPOINT = "MISSED_CHECKPOINT"

    #: The sensor block is absent, ok is false, status is not "connected",
    #: or the hub was unreachable. The whole reading is disqualified.
    SENSOR_UNAVAILABLE = "SENSOR_UNAVAILABLE"

    #: A raw device flag is false, so that one block holds placeholders rather
    #: than measurements. Its numbers are never printed.
    SUBSYSTEM_OFFLINE = "SUBSYSTEM_OFFLINE"

    #: age_seconds is above the configured maximum. The values still render,
    #: flagged stale.
    STALE_READING = "STALE_READING"

    #: No finding references this checkpoint.
    NO_FINDINGS = "NO_FINDINGS"

    #: A declared count disagrees with the data it claims to describe.
    COUNT_MISMATCH = "COUNT_MISMATCH"

    #: The checkpoints array is empty.
    EMPTY_RECORD = "EMPTY_RECORD"


@dataclass(frozen=True)
class Gap:
    """One thing the report has to say it does not have.

    `detail` is the sentence the reader sees, in both the PDF and the
    manifest, so it names what is missing rather than restating the gap type:
    "directions N, NE, E, SE have RGB with no thermal counterpart", not
    "missing thermal".
    """

    item_id: str          # a checkpoint_id, or RUN_LEVEL for the run itself
    gap_type: GapType
    detail: str


# --- one function per gap type ---------------------------------------------
#
# Each function decides one gap type and nothing else, so any rule can be read
# or tested on its own. Every one takes a checkpoint, returns a list, and
# returns an empty list when it finds nothing.
#
#   missed_checkpoint_gaps    status is MISSED
#   no_evidence_gaps          no images listed, or observed says no_evidence
#   missing_image_gaps        a listed image is absent, empty, or undecodable
#   missing_thermal_gaps      a direction has RGB and no thermal beside it
#   sensor_unavailable_gaps   the whole reading cannot be trusted
#   subsystem_offline_gaps    one device flag is false
#   stale_reading_gaps        the reading was older than the maximum
#   no_findings_gaps          no finding references this checkpoint
#
# Two belong to the run rather than to a checkpoint:
#
#   count_mismatch_gaps       a declared count disagrees with the data
#   empty_record_gaps         the checkpoints array is empty
#
# checkpoint_gaps() runs the per-checkpoint rules, run_gaps() the run-level
# ones, and detect_gaps() runs everything for a whole record.

_config = load_config(CONFIG_PATH)

#: Above this many seconds a reading still renders, but flagged stale.
MAX_AGE_SECONDS: float = setting(_config, "sensor", "max_age_seconds")

# SUBSYSTEM_FLAGS is defined in derive.py and re-exported here. One definition
# of which flag governs which block keeps this module's gap and derive's
# suppressed zone statistic from ever disagreeing about the same device.


def unavailable_reason(sensor: SensorBlock | None) -> str | None:
    """Why the whole reading cannot be used, or None when it can.

    Not a gap rule itself. sensor_unavailable_gaps turns it into a gap, and the
    two per-block rules ask it whether there is any point looking inside.

    `status` has to say "connected" for the reading to count, so one that never
    says what its status was is not trusted either. `ok` and
    `sensor_hub_reachable` work the other way round: they are read as failures
    only when they actually say false, since an absent flag claims nothing.
    """
    if sensor is None:
        return "no sensor reading was recorded"
    if sensor.ok is False:
        return "the reading reported ok=false"
    if sensor.status != "connected":
        return f"sensor status is {sensor.status!r}, not 'connected'"
    if sensor.sensor_hub_reachable is False:
        return "the sensor hub was not reachable"
    return None


def missed_checkpoint_gaps(checkpoint: Checkpoint) -> list[Gap]:
    """MISSED_CHECKPOINT: the robot never reached this checkpoint.

    Only the exact value MISSED counts. Some records carry PENDING, which is
    rendered as written rather than read as a miss.
    """
    if checkpoint.status != "MISSED":
        return []
    return [Gap(
        checkpoint.checkpoint_id, GapType.MISSED_CHECKPOINT,
        checkpoint.missed_reason or "no reason recorded",
    )]


def no_evidence_gaps(checkpoint: Checkpoint) -> list[Gap]:
    """NO_EVIDENCE: nothing was captured, or the check itself says so.

    Two causes, and a checkpoint can have both. They are listed separately so
    the detail says which applied rather than only that one did.
    """
    causes = []
    if not checkpoint.evidence_images:
        causes.append("evidence_images empty")
    if checkpoint.observed == "no_evidence":
        causes.append("observed=no_evidence")
    if not causes:
        return []
    return [Gap(checkpoint.checkpoint_id, GapType.NO_EVIDENCE, "; ".join(causes))]


def missing_image_gaps(checkpoint: Checkpoint, images: list[ResolvedImage]) -> list[Gap]: # MISSING_IMAGE
    """MISSING_IMAGE: a listed image is absent, empty, or will not decode.
    One gap per broken image, carrying the reason resolution already gave, so
    the manifest tells the three apart and says the same thing the PDF cell
    does.
    """
    return [
        Gap(
            checkpoint.checkpoint_id, GapType.MISSING_IMAGE,
            f"{PurePosixPath(image.original_uri).name}: {image.reason}",
        )
        for image in images
        if not image.readable
    ]


def missing_thermal_gaps(checkpoint: Checkpoint, grid: list[DirectionCell]) -> list[Gap]:
    """MISSING_THERMAL: a direction has an RGB image and no thermal beside it.

    Takes the grid rather than the images, because the pipeline has already
    paired them. Building a second grid here would mean two answers to the same
    question, and the manifest and the page have to give the same one.

    One gap per checkpoint naming the directions, not one per direction: eight
    gaps all saying the same thing would bury everything else in the manifest.

    Provisional, pending question 7 in decisions.md: a direction counts as
    having an RGB image once the record referenced one, whether or not that
    file resolves. Read the other way, one broken file would suppress a second,
    unrelated gap.
    """
    unpaired = [cell.direction for cell in grid if cell.rgb is not None and cell.thermal is None]
    if not unpaired:
        return []
    one = len(unpaired) == 1
    return [Gap(
        checkpoint.checkpoint_id, GapType.MISSING_THERMAL,
        f"{'direction' if one else 'directions'} {', '.join(unpaired)} "
        f"{'has' if one else 'have'} RGB with no thermal counterpart",
    )]


def sensor_unavailable_gaps(checkpoint: Checkpoint) -> list[Gap]:
    """SENSOR_UNAVAILABLE: the whole reading cannot be trusted.

    The detail says which of the four conditions applied, so the manifest is
    more use than "the sensor was unavailable".
    """
    reason = unavailable_reason(checkpoint.sensor)
    if reason is None:
        return []
    return [Gap(checkpoint.checkpoint_id, GapType.SENSOR_UNAVAILABLE, reason)]


def subsystem_offline_gaps(checkpoint: Checkpoint) -> list[Gap]:
    """SUBSYSTEM_OFFLINE: a device flag is false, so that block is not a measurement.

    Reports nothing when the whole reading is already unavailable: the table
    then reads "Sensor unavailable" and a gap naming one block inside it would
    describe something the page never shows (decision 19).
    """
    sensor = checkpoint.sensor
    if unavailable_reason(sensor) is not None:
        return []
    return [
        Gap(
            checkpoint.checkpoint_id, GapType.SUBSYSTEM_OFFLINE,
            f"{flag}=false; {block} block not recorded",
        )
        for flag, block in SUBSYSTEM_FLAGS.items()
        if getattr(sensor.raw, flag, None) is False
    ]


def stale_reading_gaps(checkpoint: Checkpoint) -> list[Gap]:
    """STALE_READING: the reading was older than the maximum when recorded.

    The values still render, flagged with the age. Reports nothing when the
    whole reading is already unavailable, for the same reason as
    subsystem_offline_gaps.
    """
    sensor = checkpoint.sensor
    if unavailable_reason(sensor) is not None:
        return []
    age = sensor.age_seconds
    if age is None or age <= MAX_AGE_SECONDS:
        return []
    return [Gap(
        checkpoint.checkpoint_id, GapType.STALE_READING,
        f"reading was {age:g} s old, above the {MAX_AGE_SECONDS:g} s maximum",
    )]


def no_findings_gaps(checkpoint: Checkpoint, findings: Sequence[Finding]) -> list[Gap]:
    """NO_FINDINGS: no finding references this checkpoint.

    `findings` is the run's whole findings array, since a finding names the
    checkpoint it came from rather than being nested inside it.

    This is a gap in the ordinary sense of the word, not a fault: most
    checkpoints have nothing to report. The page says so in as many words, so
    a reader can tell "nothing was found" from "nobody looked".
    """
    if any(finding.checkpoint_id == checkpoint.checkpoint_id for finding in findings):
        return []
    return [Gap(
        checkpoint.checkpoint_id, GapType.NO_FINDINGS,
        "no findings recorded for this checkpoint",
    )]


def empty_record_gaps(record: Record) -> list[Gap]:
    """EMPTY_RECORD: the run recorded no checkpoints at all.

    The report still renders: a cover, a coverage page, and a plain statement
    that there were none. Nothing here treats it as a failure to report.
    """
    if record.checkpoints:
        return []
    return [Gap(RUN_LEVEL, GapType.EMPTY_RECORD, "no checkpoints recorded")]


def checkpoint_gaps(
    checkpoint: Checkpoint,
    images: list[ResolvedImage],
    grid: list[DirectionCell],
    findings: Sequence[Finding] = (),
) -> list[Gap]:
    """Every gap for one checkpoint, in a fixed order.

    `images` is that checkpoint's evidence paths, resolved against the evidence
    root; `grid` is those images paired into the eight compass cells. Both come
    from the resolve_images stage, so nothing is resolved or paired twice.
    """
    return [
        *missed_checkpoint_gaps(checkpoint),
        *no_evidence_gaps(checkpoint),
        *missing_image_gaps(checkpoint, images),
        *missing_thermal_gaps(checkpoint, grid),
        *sensor_unavailable_gaps(checkpoint),
        *subsystem_offline_gaps(checkpoint),
        *stale_reading_gaps(checkpoint),
        *no_findings_gaps(checkpoint, findings),
    ]


def run_gaps(record: Record) -> list[Gap]:
    """Every gap that belongs to the run rather than to one checkpoint."""
    return [*empty_record_gaps(record), *count_mismatch_gaps(record)]


def detect_gaps(
    record: Record,
    images: dict[str, list[ResolvedImage]],
    grids: dict[str, list[DirectionCell]] | None = None,
) -> list[Gap]:
    """Every gap in a run: checkpoints in route order, then the run itself.

    Returns them all. An empty list means the run was genuinely complete, never
    that nothing was checked.

    `grids` is optional so the two-argument call still works. Pass the grids the
    resolve_images stage already built and nothing is paired twice.
    """
    if grids is None:
        grids = {
            checkpoint_id: build_direction_grid(found)
            for checkpoint_id, found in images.items()
        }

    gaps = []
    for checkpoint in record.checkpoints:
        found = images.get(checkpoint.checkpoint_id, [])
        gaps += checkpoint_gaps(
            checkpoint,
            found,
            grids.get(checkpoint.checkpoint_id) or build_direction_grid(found),
            record.findings,
        )
    return gaps + run_gaps(record)


# --- the count rules -------------------------------------------------------
#
# Declared counts are claims, not facts, and every one is cross-checked against
# the data it describes. Where they disagree the report prints both figures and
# flags the conflict; it never silently prefers one.
#
# Three separate checks produce COUNT_MISMATCH:
#
#   the seven rows of the coverage page   declared vs the arrays      __run__
#   warned_checkpoints vs sensor warnings  the contradiction of 2.9    __run__
#   evidence_count vs evidence_images      per checkpoint (TA-25)      its id
#
# The second is not one of the seven rows, and that is the point of it. In the
# reference run warned_checkpoints is 0 and no checkpoint has result_status
# WARN, so that row agrees - while seven checkpoints carry a non-empty
# sensor.warnings array. Comparing only the rows would report nothing.

#: Each row of the coverage page: the Counts field, the record field it was
#: declared in, and what the computed figure actually counted.
COUNT_ROWS = (
    ("required", "total_required_checkpoints", "checkpoints in the array"),
    ("completed", "total_completed_checkpoints", "checkpoints with status COMPLETED"),
    ("passed", "passed_checkpoints", "checkpoints with result_status PASS"),
    ("failed", "failed_checkpoints", "checkpoints with result_status FAIL"),
    ("missed", "missed_checkpoints", "checkpoints with status MISSED"),
    ("warned", "warned_checkpoints", "checkpoints with result_status WARN"),
    ("findings", "finding_count", "findings in the array"),
)


def count_mismatch_gaps(record: Record) -> list[Gap]:
    """COUNT_MISMATCH: a declared count disagrees with the data it describes.

    A count the record never declared raises nothing here. There is no claim to
    disagree with, and the loader has already recorded the field as absent.
    """
    declared = declared_counts(record)
    computed = computed_counts(record)

    gaps = [
        Gap(
            RUN_LEVEL, GapType.COUNT_MISMATCH,
            f"{field} declared {getattr(declared, name)}; "
            f"{getattr(computed, name)} {counted}",
        )
        for name, field, counted in COUNT_ROWS
        if getattr(declared, name) is not None
        and getattr(declared, name) != getattr(computed, name)
    ]

    # The contradiction 2.9 singles out. Not one of the rows above: a run can
    # declare no warned checkpoints, have none with result_status WARN, and
    # still carry a sensor warning at almost every checkpoint.
    with_warnings = sum(1 for c in record.checkpoints if c.sensor and c.sensor.warnings)
    if declared.warned is not None and declared.warned != with_warnings:
        gaps.append(Gap(
            RUN_LEVEL, GapType.COUNT_MISMATCH,
            f"warned_checkpoints declared {declared.warned}; "
            f"{with_warnings} checkpoints carry sensor warnings",
        ))

    # Each event that declared an evidence count, against the images that
    # checkpoint actually lists. Owned by the checkpoint, not the run.
    listed = {c.checkpoint_id: len(c.evidence_images) for c in record.checkpoints}
    for event in record.event_log:
        if event.evidence_count is None or event.checkpoint_id not in listed:
            continue
        actual = listed[event.checkpoint_id]
        if event.evidence_count != actual:
            gaps.append(Gap(
                event.checkpoint_id, GapType.COUNT_MISMATCH,
                f"event_log declared evidence_count {event.evidence_count}; "
                f"evidence_images lists {actual}",
            ))

    return gaps
