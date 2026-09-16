"""Gap detection: the things a report has to say it does not have.

A Gap is one thing missing, stale, or contradictory in a run. Every gap shown
is found here, and every gap in the manifest is found here, so the
two can never disagree about what was wrong with a run.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Collection, Sequence

from pathlib import PurePosixPath

from common.paths import DEFAULT_CONFIG, ResolvedImage, setting
from common.schema import Checkpoint, Finding, Record, SensorBlock
from report.derive import SUBSYSTEM_FLAGS, computed_counts, declared_counts
from report.images import ViewCell, build_view_grid

#: item_id for a gap that belongs to the run itself rather than to a checkpoint.
RUN_LEVEL = "__run__"


class GapType(StrEnum):
    """The ten kinds of gap. A closed set: no others, ever.
    """

    #: A referenced image does not resolve, is empty, or will not decode.
    MISSING_IMAGE = "MISSING_IMAGE"

    #: A view has an RGB image and no thermal counterpart.
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

    #: A field arrived as the wrong type and was refused.
    INCORRECT_DATATYPE = "INCORRECT_DATATYPE"


@dataclass(frozen=True)
class Gap:
    """One thing the report has to say it does not have.

    `detail` is the sentence the reader sees, in both the PDF and the
    manifest, so it names what is missing rather than restating the gap type:
    """

    item_id: str          # a checkpoint_id, or RUN_LEVEL for the run itself
    gap_type: GapType
    detail: str

# above this many seconds a reading still renders, but flagged stale.
MAX_AGE_SECONDS: float = setting(DEFAULT_CONFIG, "sensor", "max_age_seconds")


# to give out the reason for why the sensor reading can not be used
def unavailable_reason(sensor: SensorBlock | None) -> str | None:
    """Why the whole reading cannot be used, or None when it can.
    Not a gap rule itself. sensor_unavailable_gaps turns it into a gap, and the
    two per-block rules ask it whether there is any point looking inside.
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
        causes.append("observed = no_evidence")
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


def missing_thermal_gaps(checkpoint: Checkpoint, grid: list[ViewCell]) -> list[Gap]:
    """MISSING_THERMAL: a view has an RGB image and no thermal beside it.

    Views are named where the filenames named them and unnamed where they did
    not, so the detail either lists the labels or says how many views were
    unpaired. A checkpoint that recorded one unlabelled picture and no thermal
    still reports the gap; it just has no label to quote.
    """
    unpaired = [cell.view for cell in grid if cell.rgb is not None and cell.thermal is None]
    if not unpaired:
        return []

    one = len(unpaired) == 1
    named = [view for view in unpaired if view]
    if named:
        what = f"{'view' if one else 'views'} {', '.join(named)}"
    else:
        what = "the recorded view" if one else f"{len(unpaired)} recorded views"

    return [Gap(
        checkpoint.checkpoint_id, GapType.MISSING_THERMAL,
        f"{what} {'has' if one else 'have'} RGB with no thermal counterpart",
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


def subsystem_offline_gaps(
    checkpoint: Checkpoint, run_level_blocks: Collection[str] = (),
) -> list[Gap]:
    """SUBSYSTEM_OFFLINE: a device flag is false, so that block is not a measurement.

    Reports nothing when the whole reading is already unavailable: the table
    then reads "Sensor unavailable" and a gap naming one block inside it would
    describe something the page never shows (decision 19).

    `run_level_blocks` are the blocks whose device was off at every checkpoint
    that could report on it. Those are one gap about the run, raised by
    run_subsystem_offline_gaps, so they are not repeated here - eight identical
    gaps saying the same thing is a scope the item_id already expresses
    (decision 171).
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
        if getattr(sensor.raw, flag, None) is False and block not in run_level_blocks
    ]


def offline_everywhere(record: Record) -> dict[str, tuple[str, int, int]]:
    """Blocks whose device was off wherever it could be read: block -> (flag, off, readable).

    A checkpoint reports on a flag only when its reading is usable at all: one
    whose whole sensor is unavailable says nothing about any device inside it,
    which is the same rule decision 19 applies to the gap. An absent flag claims
    nothing either (decision 21).

    A block qualifies when at least one checkpoint reported the flag false and
    none reported it true. `off` is how many said so and `readable` how many
    could have.
    """
    off: dict[str, int] = {}
    readable: dict[str, int] = {}
    working: set[str] = set()

    for checkpoint in record.checkpoints:
        if unavailable_reason(checkpoint.sensor) is not None:
            continue
        raw = checkpoint.sensor.raw
        for flag, block in SUBSYSTEM_FLAGS.items():
            value = getattr(raw, flag, None)
            if value is None:
                continue
            readable[block] = readable.get(block, 0) + 1
            if value is False:
                off[block] = off.get(block, 0) + 1
            else:
                working.add(block)

    return {
        block: (flag, off[block], readable[block])
        for flag, block in SUBSYSTEM_FLAGS.items()
        if off.get(block) and block not in working
    }


def run_subsystem_offline_gaps(record: Record) -> list[Gap]:
    """SUBSYSTEM_OFFLINE for a device that was off for the whole run.

    One gap, owned by the run rather than by a checkpoint, because that is what
    the fact is about: the device never worked, and eight checkpoints repeating
    it says nothing the first one did not. The detail names the device and the
    span it covers, so a reader of the manifest can tell this from a gap about
    one checkpoint without looking at anything else.

    The flag stays at the front of the detail, where offline_block_gaps reads it
    back, so a section that leaves a measurement out still names the same device
    the manifest does.
    """
    return [
        Gap(
            RUN_LEVEL, GapType.SUBSYSTEM_OFFLINE,
            f"{flag}=false at every checkpoint that reported it "
            f"({off} of {readable}); {block} block not recorded for the run",
        )
        for block, (flag, off, readable) in offline_everywhere(record).items()
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


def offline_block_gaps(gaps: Sequence[Gap], block: str) -> list[Gap]:
    """The SUBSYSTEM_OFFLINE gaps belonging to one sensor block.

    Not a rule. A section that leaves a measurement out because its device was
    off has to say so - 4.4 requires every manifest gap to appear in the PDF -
    and this is how it asks which gaps those were, rather than testing the
    flags a second time in a template (5.4, 5.5).

    The flag is read back off the front of the detail this module wrote, so
    there is no second copy of the flag-to-block mapping to fall out of step
    with SUBSYSTEM_FLAGS.
    """
    flags = {flag for flag, name in SUBSYSTEM_FLAGS.items() if name == block}
    return [
        gap for gap in gaps
        if gap.gap_type is GapType.SUBSYSTEM_OFFLINE
        and gap.detail.split("=", 1)[0] in flags
    ]


def empty_record_gaps(record: Record) -> list[Gap]:
    """EMPTY_RECORD: the run recorded no checkpoints at all.

    The report still renders: a cover, a coverage page, and a plain statement
    that there were none. Nothing here treats it as a failure to report.
    """
    if record.checkpoints:
        return []
    return [Gap(RUN_LEVEL, GapType.EMPTY_RECORD, "no checkpoints recorded")]


def datatype_gaps(record: Record, item_id: str | None = None) -> list[Gap]:
    """INCORRECT_DATATYPE: a field was the wrong type, and was refused rather than read.

    The loader never converts: a confidence written as the string "0.94" is left
    unset and recorded as an anomaly, so the page prints "Not scored" and no
    measurement is invented from it (TA-27). This turns that record into a gap,
    so the refusal appears in the report and the manifest instead of only in the
    log.

    Only anomalies of kind "datatype" become gaps. A required field that was
    absent, an array entry dropped for having no id, and a checkpoint_id used
    twice are all recorded by the loader too, and none of them is a datatype
    problem; 4.3 has no member for them, so they stay in the anomaly log.

    `item_id` narrows it to one checkpoint. None returns every one.
    """
    return [
        Gap(
            anomaly.item_id, GapType.INCORRECT_DATATYPE,
            f"{anomaly.field_name}: {anomaly.problem}",
        )
        for anomaly in record.anomalies
        if anomaly.kind == "datatype" and (item_id is None or anomaly.item_id == item_id)
    ]


def checkpoint_gaps(
    checkpoint: Checkpoint,
    images: list[ResolvedImage],
    grid: list[ViewCell],
    findings: Sequence[Finding] = (),
    run_level_blocks: Collection[str] = (),
) -> list[Gap]:
    """Every gap for one checkpoint, in a fixed order.

    `images` is that checkpoint's evidence paths, resolved against the evidence
    root; `grid` is those images paired into the eight compass cells. Both come
    from the resolve_images stage, so nothing is resolved or paired twice.

    `run_level_blocks` are the blocks already reported as off for the whole run.
    A trailing argument with a default, so a caller that has one checkpoint and
    no view of the run still gets that checkpoint's gaps.
    """
    return [
        *missed_checkpoint_gaps(checkpoint),
        *no_evidence_gaps(checkpoint),
        *missing_image_gaps(checkpoint, images),
        *missing_thermal_gaps(checkpoint, grid),
        *sensor_unavailable_gaps(checkpoint),
        *subsystem_offline_gaps(checkpoint, run_level_blocks),
        *stale_reading_gaps(checkpoint),
        *no_findings_gaps(checkpoint, findings),
    ]


def run_gaps(record: Record) -> list[Gap]:
    """Every gap that belongs to the run rather than to one checkpoint.

    The datatype gaps here are the ones no checkpoint owns: a run-level field,
    or an array entry the loader could only locate by position. A checkpoint's
    own are raised beside it, in detect_gaps.
    """
    known = {checkpoint.checkpoint_id for checkpoint in record.checkpoints}
    return [
        *empty_record_gaps(record),
        *run_subsystem_offline_gaps(record),
        *count_mismatch_gaps(record),
        *evidence_count_gaps(record),
        *[gap for gap in datatype_gaps(record) if gap.item_id not in known],
    ]


def detect_gaps(
    record: Record,
    images: dict[str, list[ResolvedImage]],
    grids: dict[str, list[ViewCell]] | None = None,
) -> list[Gap]:
    """Every gap in a run: checkpoints in route order, then the run itself.

    Returns them all. An empty list means the run was genuinely complete, never
    that nothing was checked.

    `grids` is optional so the two-argument call still works. Pass the grids the
    resolve_images stage already built and nothing is paired twice.
    """
    if grids is None:
        grids = {
            checkpoint_id: build_view_grid(found)
            for checkpoint_id, found in images.items()
        }

    # Devices that were off wherever they could be read are one gap about the
    # run, not one per checkpoint, so the per-checkpoint rule skips them.
    run_level_blocks = set(offline_everywhere(record))

    gaps = []
    for checkpoint in record.checkpoints:
        found = images.get(checkpoint.checkpoint_id, [])
        gaps += checkpoint_gaps(
            checkpoint,
            found,
            grids.get(checkpoint.checkpoint_id) or build_view_grid(found),
            record.findings,
            run_level_blocks,
        )
        # The fields of this checkpoint the loader refused. Raised here rather
        # than inside checkpoint_gaps, which is given one checkpoint and not the
        # record's anomaly list.
        gaps += datatype_gaps(record, checkpoint.checkpoint_id)
    return gaps + run_gaps(record)


# --- the count rules -------------------------------------------------------
#
# Declared counts are claims, not facts, and every one is cross-checked against
# the data it describes. Where they disagree the report prints both figures and
# flags the conflict; it never silently prefers one.

COUNT_ROWS = (
    ("required", "total_required_checkpoints", "checkpoints in the array"),
    ("completed", "total_completed_checkpoints", "checkpoints with status COMPLETED"),
    ("passed", "passed_checkpoints", "checkpoints with result_status PASS"),
    ("failed", "failed_checkpoints", "checkpoints with result_status FAIL"),
    ("missed", "missed_checkpoints", "checkpoints with status MISSED"),
    ("warned", "warned_checkpoints", "checkpoints with result_status WARN or a sensor warning"),
    ("findings", "finding_count", "findings in the array"),
)


def disagreeing_counts(record: Record) -> set[str]:
    """Which of the seven counts the record declared differ from its own arrays.

    Named by their Counts field. Both the COUNT_MISMATCH gap below and the mark
    on the coverage page's row read this, so the manifest and the page cannot
    disagree about which row is wrong - two copies of the same comparison would
    only agree until one of them changed (5.4).

    A count the record never declared is not in the set: there is no claim for
    the array to disagree with (decision 27).
    """
    declared = declared_counts(record)
    computed = computed_counts(record)
    return {
        name for name, _, _ in COUNT_ROWS
        if getattr(declared, name) is not None
        and getattr(declared, name) != getattr(computed, name)
    }


def count_mismatch_gaps(record: Record) -> list[Gap]:
    """COUNT_MISMATCH: a declared count disagrees with the data it describes.

    The seven rows of the coverage page, and nothing else. 
    """
    declared = declared_counts(record)
    computed = computed_counts(record)
    disagreeing = disagreeing_counts(record)

    gaps = [
        Gap(
            RUN_LEVEL, GapType.COUNT_MISMATCH,
            f"{field} declared {getattr(declared, name)}; "
            f"{getattr(computed, name)} {counted}",
        )
        for name, field, counted in COUNT_ROWS
        if name in disagreeing
    ]
    return gaps


def evidence_count_gaps(record: Record) -> list[Gap]:
    """COUNT_MISMATCH: event_log's evidence count is not the array's length.
    """
    listed = {c.checkpoint_id: len(c.evidence_images) for c in record.checkpoints}

    gaps = []
    for event in record.event_log:
        if event.evidence_count is None or event.checkpoint_id is None:
            continue
        if event.checkpoint_id not in listed:
            gaps.append(Gap(
                event.checkpoint_id, GapType.COUNT_MISMATCH,
                f"event_log declared evidence_count {event.evidence_count}; "
                f"the run recorded no checkpoint with this id",
            ))
            continue
        actual = listed[event.checkpoint_id]
        if event.evidence_count != actual:
            gaps.append(Gap(
                event.checkpoint_id, GapType.COUNT_MISMATCH,
                f"event_log declared evidence_count {event.evidence_count}; "
                f"evidence_images lists {actual}",
            ))

    return gaps
