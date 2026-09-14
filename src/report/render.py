"""Template -> HTML -> PDF.

5.5 fixes the stack: Jinja2 renders the templates to HTML and WeasyPrint turns
that into the PDF. Page-break control lives in styles.css and nowhere else.

Autoescaping is on for every template, which is what 5.5 asks for: notes,
description, missed_reason and recommended_action are free text, and a field
holding <script> or {{ config }} has to appear as written rather than execute or
interpolate.

Templates render what earlier stages worked out. Nothing here decides whether a
reading can be trusted or whether an image is missing; detect_gaps has already
said so, and 5.4 keeps that decision out of the templates.

This module is where formatting lives. An absent value becomes the words "Not
recorded" here, not in derive.py, so how the report words a missing figure is
settled in one place.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from weasyprint import CSS, HTML

from common import OUTPUT_DIR, PROJECT_ROOT
from common.paths import ResolvedImage, setting
from common.provenance import Provenance, stamp
from common.schema import (
    Checkpoint, Finding, Point2D, Pose, Record, SensorAlert, SensorBlock, SensorWarning,
)
from report.derive import DerivedValues, ZoneStat
from report.gaps import RUN_LEVEL, Gap, GapType, disagreeing_counts, offline_block_gaps
from report.images import GRID_COLUMNS, DirectionCell, prepare_image

logger = logging.getLogger("report.render")

__all__ = [
    "TEMPLATE_DIR", "STYLES_PATH", "SECTION_NAMES",
    "environment", "show", "moment", "sections",
    "CountRow", "count_rows", "CoverageItem", "coverage_items",
    "run_count_conflicts",
    "VERDICT_STYLES", "verdict_style", "STATUS_STYLES", "status_style",
    "SEVERITY_STYLES", "severity_style", "place",
    "CONFIRMED_STATUSES", "REVIEW_STATUS", "FindingGroup", "finding_groups",
    "ALERT_SEVERITIES", "AlertGroup", "alert_groups",
    "EvidenceCell", "image_absence", "image_source", "evidence_cells",
    "SENSOR_BLOCKS", "SensorBlockView", "SensorView", "reading",
    "sensor_block_view", "sensor_view",
    "CheckpointView", "checkpoint_views", "ZoneGroup", "zone_groups",
    "SummaryRow", "evidence_tally", "sensor_summary", "summary_rows",
    "ZONE_COLUMNS", "measure", "device_name", "zone_absence",
    "provenance_line", "logo_uri", "css_string", "runtime_css",
    "render_html", "render_pdf",
]

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STYLES_PATH = TEMPLATE_DIR / "styles.css"

#: The seven sections 5.7 can switch on and off, in the order 3.1 renders them.
SECTION_NAMES = (
    "cover", "coverage", "findings", "alerts", "zone_telemetry",
    "checkpoints", "summary",
)


# --- what a template is given ----------------------------------------------


def show(value: object, absent: str = "Not recorded") -> object:
    """A value, or the words that say it was never recorded.

    2.9 makes absent, null and "" the same thing, so all three read alike and a
    blank cell never stands in for a fact nobody wrote down.
    """
    if value is None or value == "":
        return absent
    return value


def moment(value: datetime | None, absent: str = "Not recorded") -> str:
    """One timestamp, with the offset it was recorded in.

    The offset is kept because a time without one says less than the record did.
    """
    if value is None:
        return absent
    return value.strftime("%Y-%m-%d %H:%M:%S %z")


def place(pose: Pose | Point2D | None) -> str | None:
    """Where something was recorded, as text, or None when nowhere was.

    Only the keys carrying a value are named. A two-key alert position and a
    four-key checkpoint pose therefore read the same way without either
    inventing the other's fields (2.5).
    """
    if pose is None:
        return None
    named = (
        ("x", getattr(pose, "x", None)),
        ("y", getattr(pose, "y", None)),
        ("z", getattr(pose, "z", None)),
        ("yaw", getattr(pose, "yaw", None)),
    )
    return ", ".join(f"{name} {value:g}" for name, value in named if value is not None) or None


def environment() -> Environment:
    """The Jinja environment every template renders in.

    Autoescaping is on, so free text renders as text (5.5, TA-29). Undefined
    names raise instead of rendering empty, so a mistyped field in a template is
    a failure rather than a quietly blank cell.
    """
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["show"] = show
    env.filters["moment"] = moment
    env.filters["place"] = place
    env.filters["measure"] = measure
    env.filters["zone_absence"] = zone_absence
    env.filters["severity_style"] = severity_style
    env.filters["verdict_style"] = verdict_style
    env.filters["status_style"] = status_style
    return env


# --- the counts, shared by the cover and the coverage page -------------------

#: The seven rows of 3.3, labelled as 3.3 labels them. The cover prints the same
#: figures as its headline counts, so both pages read from one list and cannot
#: show different numbers for the same row.
COUNT_LABELS = (
    ("required", "Required checkpoints"),
    ("completed", "Completed"),
    ("passed", "Passed"),
    ("failed", "Failed"),
    ("missed", "Missed"),
    ("warned", "Warned"),
    ("findings", "Findings"),
)


@dataclass(frozen=True)
class CountRow:
    """One row of 3.3: what the record declared, and what its arrays hold."""

    name: str             # the Counts field, so a section can ask for one row
    label: str
    computed: int | None
    declared: int | None
    disagrees: bool


def count_rows(derived: DerivedValues, disagreeing: set[str]) -> list[CountRow]:
    """The seven rows of the reconciliation table.

    Declared is copied from the record and never corrected; computed is what the
    arrays actually contain.

    `disagreeing` is gaps.disagreeing_counts' answer, not a comparison made
    here. The row that is marked on the page is therefore the same row that
    raised the COUNT_MISMATCH gap in the manifest, by construction rather than
    by two comparisons happening to match (5.4).
    """
    return [
        CountRow(
            name=name,
            label=label,
            computed=getattr(derived.computed, name),
            declared=getattr(derived.declared, name),
            disagrees=name in disagreeing,
        )
        for name, label in COUNT_LABELS
    ]


#: Verdict -> badge style. 2.2 lists PASS, FAIL and WARN. Anything else renders
#: as written in the neutral style, because 11 forbids mapping a value nobody
#: listed onto a known one.
VERDICT_STYLES = {"PASS": "pass", "FAIL": "fail", "WARN": "warn"}


def verdict_style(final_status: str) -> str:
    """The badge style for a verdict, or the neutral one for an unlisted value."""
    return VERDICT_STYLES.get(final_status, "other")


#: Status -> badge style. 2.2 lists COMPLETED and MISSED. This is the same kind
#: of choice as the verdict styles above: the word still prints as recorded and
#: only the colour is shared, so a status nobody listed is neutral rather than
#: dressed as one of these two.
STATUS_STYLES = {"COMPLETED": "pass", "MISSED": "fail"}


def status_style(status: str) -> str:
    """The badge style for whether the robot got there, or the neutral one."""
    return STATUS_STYLES.get(status, "other")


# --- what sits under the reconciliation table (3.3) --------------------------


@dataclass(frozen=True)
class CoverageItem:
    """One checkpoint listed beneath the reconciliation table."""

    checkpoint_id: str
    checkpoint_name: str
    detail: str
    observed: str | None


def coverage_items(record: Record, gaps: list[Gap], gap_type: GapType) -> list[CoverageItem]:
    """The checkpoints carrying one kind of gap, each with the detail it was given.

    Built from detect_gaps' output rather than by testing the checkpoints over
    again, so the list under the table and the manifest's gaps array cannot
    describe different checkpoints (4.4, 5.4).

    A gap whose checkpoint cannot be found is still listed, under its own id.
    Dropping it would hide something the manifest reports.
    """
    by_id = {checkpoint.checkpoint_id: checkpoint for checkpoint in record.checkpoints}

    items = []
    for gap in gaps:
        if gap.gap_type is not gap_type:
            continue
        checkpoint = by_id.get(gap.item_id)
        items.append(CoverageItem(
            checkpoint_id=gap.item_id,
            checkpoint_name=checkpoint.checkpoint_name if checkpoint else gap.item_id,
            detail=gap.detail,
            observed=checkpoint.observed if checkpoint else None,
        ))
    return items


def run_count_conflicts(gaps: list[Gap]) -> list[Gap]:
    """Every count conflict belonging to the run rather than to a checkpoint.

    The table marks which rows disagree; these say in words what each conflict
    was. That includes the one 2.9 singles out and TA-23 tests, which is not one
    of the seven rows at all: a run can declare no warned checkpoints, have none
    with result_status WARN, and still carry a sensor warning at almost every
    checkpoint.
    """
    return [
        gap for gap in gaps
        if gap.gap_type is GapType.COUNT_MISMATCH and gap.item_id == RUN_LEVEL
    ]


# --- the findings summary (3.1) ----------------------------------------------
#
# 3.1 asks for every run-level finding in a table, and 3.6 gives exactly one
# rule about which table: an abstained finding is shown as requiring review,
# not as a confirmed finding, and is never dropped.
#
# That rule names one status. 2.4 lists two others, `logged` and
# `acknowledged`, and those are what "confirmed" can mean here. A status that
# is none of the three is neither: 11 says an enum value section 2 does not
# list renders verbatim and is never mapped onto a known one, so it cannot be
# shown as confirmed and cannot be shown as abstained either. It gets its own
# block, which is the only place left that claims nothing about it.

#: The statuses 2.4 lists, other than the one 3.6 gives a rule for.
CONFIRMED_STATUSES = ("logged", "acknowledged")

#: The status 3.6 and TA-05 single out.
REVIEW_STATUS = "abstained"

#: Severity -> badge style. 2.4 lists info, warning and fail, lowercase and
#: unlike the status values elsewhere. 2.5 adds critical, which alerts use and
#: findings do not; it shares the fail colour because 3.5 wants criticals told
#: apart at a glance. Only the colour is shared - the word still prints as
#: recorded. A severity nobody listed renders as written in the neutral style,
#: for the same reason verdict_style does (11).
SEVERITY_STYLES = {
    "fail": "fail", "warning": "warn", "info": "info", "critical": "fail",
}


def severity_style(severity: str) -> str:
    """The badge style for a finding's or an alert's severity, or the neutral one."""
    return SEVERITY_STYLES.get(severity, "other")


@dataclass(frozen=True)
class FindingGroup:
    """One block of the findings summary: a heading, a sentence and its rows.

    `absent` is what the block says when it holds nothing. None means the block
    is left out entirely when empty.
    """

    heading: str
    lede: str
    findings: tuple[Finding, ...]
    absent: str | None = None


def finding_groups(findings: Sequence[Finding]) -> list[FindingGroup]:
    """Every finding, split into the blocks the findings summary renders.

    Each finding lands in exactly one block and none is dropped, so 3.1's "all
    run-level findings" holds however a record spells a status.

    A run that recorded none gets no blocks at all. Three headings each saying
    nothing was there says less than one sentence does, and the template prints
    that sentence instead.

    The two blocks the brief names always render, with a sentence when they are
    empty: "no finding was abstained" is a fact worth printing, and 0 asks for
    an absence to be stated rather than left blank. The third block exists only
    because a record used a status section 2 does not list, so it is left out
    when empty rather than heading an empty block for a category the brief does
    not have.
    """
    if not findings:
        return []

    listed = (*CONFIRMED_STATUSES, REVIEW_STATUS)
    return [
        FindingGroup(
            heading="Confirmed findings",
            lede="Findings the run recorded as logged or acknowledged.",
            findings=tuple(f for f in findings if f.status in CONFIRMED_STATUSES),
            absent="No finding was recorded as logged or acknowledged.",
        ),
        FindingGroup(
            heading="Requires review",
            lede=(
                "The detector was not confident enough to call these. They are "
                "not confirmed findings, and they have not been dropped: each "
                "needs a person to decide."
            ),
            findings=tuple(f for f in findings if f.status == REVIEW_STATUS),
            absent="No finding was abstained.",
        ),
        FindingGroup(
            heading="Findings with an unrecognised status",
            lede=(
                "These carry a status this report does not recognise. Each is "
                "printed exactly as recorded, and shown neither as confirmed "
                "nor as requiring review: the record does not say which it is."
            ),
            findings=tuple(f for f in findings if f.status not in listed),
        ),
    ]


# --- the sensor alerts summary (3.5) -----------------------------------------
#
# 3.1 asks for the alerts grouped by severity; 3.5 asks for them sorted by
# timestamp, with a count of each severity at the head. Grouping by severity
# and ordering each group by time satisfies both, so neither line has to be
# read as overriding the other (decision 90).


#: Severity order for the alert blocks. 2.5 names critical and warning, and the
#: worst reads first. The set is open, so anything else follows these in the
#: order alert_counts sorted it into, never folded into a severity it is not.
ALERT_SEVERITIES = ("critical", "warning")


@dataclass(frozen=True)
class AlertGroup:
    """One severity's block of the alerts summary, oldest alert first."""

    severity: str
    alerts: tuple[SensorAlert, ...]


def alert_groups(alerts: Sequence[SensorAlert], counted: dict[str, int]) -> list[AlertGroup]:
    """Every alert, split by severity and ordered by time within each block.

    `counted` is derive.py's tally, so the severities that get a block and the
    figures printed at the head of the section come from one place and cannot
    disagree.

    An alert with no timestamp sorts last rather than failing the comparison:
    2.5 marks the field required, and 11 asks for a record that does not match
    section 2 to be handled rather than to crash.
    """
    ordered = [s for s in ALERT_SEVERITIES if s in counted]
    ordered += [s for s in counted if s not in ALERT_SEVERITIES]
    return [
        AlertGroup(
            severity=severity,
            alerts=tuple(sorted(
                (a for a in alerts if a.severity == severity),
                key=lambda a: (a.timestamp is None, a.timestamp or datetime.min),
            )),
        )
        for severity in ordered
    ]


# --- the per-checkpoint section (3.2) ----------------------------------------
#
# Three pieces, kept apart because they answer different questions: what the
# evidence grid draws, what the sensor table says, and what the block as a
# whole is made of.
#
# Not one of them tests a device flag, a file or a count. detect_gaps has
# already decided all of that (5.4), and every absence printed here is read
# back off a gap it raised, which is what keeps 4.4's two directions true: the
# manifest and the page cannot disagree, because they are the same list.


# --- the evidence grid -------------------------------------------------------


@dataclass(frozen=True)
class EvidenceCell:
    """One cell of the rendered grid: a picture to draw, or words to print.

    Each modality has a source or an absence, never both and never neither.
    3.2 gives every direction a cell whether or not an image exists, so the
    grid keeps its shape and a missing direction does not shift the others.
    """

    direction: str
    rgb_src: str | None
    rgb_absent: str | None
    thermal_src: str | None
    thermal_absent: str | None


def image_absence(image: ResolvedImage | None, uncaptured: str, unusable: str) -> str | None:
    """What a cell says in place of a picture, or None when it has one to show.

    3.6 asks for three situations to read differently: nothing was captured for
    that direction, a path was recorded and does not resolve, and a file is
    there but empty or undecodable. The first is this cell's own wording; the
    other two carry the reason resolution already worked out, which is the same
    reason the MISSING_IMAGE gap carries.
    """
    if image is None:
        return uncaptured
    if not image.readable:
        return f"{unusable} - {image.reason}"
    return None


def image_source(image: ResolvedImage | None, image_dir: Path | None) -> str | None:
    """A URI for the file to embed, or None when there is nothing to embed.

    Sizing the file down happens in images.py, per 5.5.
    """
    path = prepare_image(image, image_dir) if image is not None else None
    return path.resolve().as_uri() if path is not None else None


def evidence_cells(
    grid: Sequence[DirectionCell], image_dir: Path | None
) -> tuple[EvidenceCell, ...]:
    """The eight cells of one checkpoint's grid, ready to draw."""
    return tuple(
        EvidenceCell(
            direction=cell.direction,
            rgb_src=image_source(cell.rgb, image_dir),
            rgb_absent=image_absence(cell.rgb, "Image not captured", "Image not available"),
            thermal_src=image_source(cell.thermal, image_dir),
            thermal_absent=image_absence(cell.thermal, "No thermal", "Thermal not available"),
        )
        for cell in grid
    )


# --- the sensor table --------------------------------------------------------

#: The three measurement blocks 3.2 draws, in the order it names them, with the
#: fields each carries and the unit each was recorded in. The keys are the block
#: names 5.7's subsystem_flags maps its device flags onto, so the table and the
#: trust rule cannot be talking about different blocks.
SENSOR_BLOCKS = (
    ("environment", "Environment", (
        ("temperature_c", "Temperature", "°C"),
        ("humidity_pct", "Humidity", "%"),
        ("pressure_hpa", "Pressure", "hPa"),
    )),
    ("accelerometer", "Accelerometer", (
        ("accel_x", "Acceleration X", "m/s²"),
        ("accel_y", "Acceleration Y", "m/s²"),
        ("accel_z", "Acceleration Z", "m/s²"),
        ("vibration_peak", "Vibration peak", "g"),
        ("vibration_rms_g", "Vibration RMS", "g"),
    )),
    ("particulate", "Particulate", (
        ("pm1_0", "PM1.0", "µg/m³"),
        ("pm2_5", "PM2.5", "µg/m³"),
        ("pm4_0", "PM4.0", "µg/m³"),
        ("pm10", "PM10", "µg/m³"),
    )),
)


@dataclass(frozen=True)
class SensorBlockView:
    """One measurement block: its readings, or the words that replace them.

    `absent` and `rows` are exclusive. When a device flag said the block is not
    a measurement, there are no rows at all - the numbers are not read, not
    formatted and not hidden behind a style. Nothing downstream can print them
    because nothing downstream is given them (TA-09, TA-10, TA-11).
    """

    name: str
    absent: str | None
    rows: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SensorView:
    """A checkpoint's whole sensor table.

    `unavailable` set means the reading is disqualified entirely and there are
    no blocks: 3.6 has the table read "Sensor unavailable" rather than listing
    three blocks that each say the same thing.

    `stale` set means the values still render, flagged with their age (2.8).
    """

    unavailable: str | None
    stale: str | None
    blocks: tuple[SensorBlockView, ...]
    warnings: tuple[SensorWarning, ...]


def reading(value: float | None, unit: str) -> str:
    """One measurement with its unit, or the words that say none was recorded."""
    if value is None:
        return "Not recorded"
    return f"{measure(value)} {unit}"


def sensor_block_view(
    sensor: SensorBlock, gaps: Sequence[Gap], key: str, name: str,
    fields: Sequence[tuple[str, str, str]],
) -> SensorBlockView:
    """One block of the sensor table, from the gaps already raised for it.

    Whether the device was working is not asked here. offline_block_gaps is the
    question put to detect_gaps' answer, and the flag is read back off the gap
    so the sentence names the same device the manifest does (TA-09).
    """
    offline = offline_block_gaps(gaps, key)
    if offline:
        flag = offline[0].detail.split("=", 1)[0]
        return SensorBlockView(
            name=name, absent=f"Not recorded - {device_name(flag)} offline", rows=(),
        )

    block = getattr(sensor, key, None)
    if block is None:
        return SensorBlockView(name=name, absent="Not recorded", rows=())

    return SensorBlockView(
        name=name,
        absent=None,
        rows=tuple(
            (label, reading(getattr(block, field, None), unit))
            for field, label, unit in fields
        ),
    )


def sensor_view(checkpoint: Checkpoint, gaps: Sequence[Gap]) -> SensorView:
    """One checkpoint's sensor table, built from its own gaps.

    The order matters and is 2.8's: a reading that is disqualified as a whole
    never reaches the per-block question, which is also why detect_gaps raises
    no SUBSYSTEM_OFFLINE alongside a SENSOR_UNAVAILABLE (decision 10).
    """
    mine = [gap for gap in gaps if gap.item_id == checkpoint.checkpoint_id]

    unavailable = next(
        (gap.detail for gap in mine if gap.gap_type is GapType.SENSOR_UNAVAILABLE), None,
    )
    if unavailable is not None:
        return SensorView(unavailable=unavailable, stale=None, blocks=(), warnings=())

    # detect_gaps always raises SENSOR_UNAVAILABLE for a checkpoint with no
    # sensor key at all (TA-14), so this is reached only by a caller that
    # passed a partial gap list. It says the same thing rather than failing on
    # the attribute, because a missing reading is exactly what it is.
    sensor = checkpoint.sensor
    if sensor is None:
        return SensorView(
            unavailable="no sensor reading was recorded", stale=None, blocks=(), warnings=(),
        )

    return SensorView(
        unavailable=None,
        stale=next(
            (gap.detail for gap in mine if gap.gap_type is GapType.STALE_READING), None,
        ),
        blocks=tuple(
            sensor_block_view(sensor, mine, key, name, fields)
            for key, name, fields in SENSOR_BLOCKS
        ),
        warnings=sensor.warnings,
    )


# --- the block as a whole ----------------------------------------------------


@dataclass(frozen=True)
class CheckpointView:
    """Everything 3.2 draws for one checkpoint.

    `missed_reason` and `no_evidence` are the details of the gaps that were
    raised for those two situations, not a second reading of the record. A
    checkpoint carrying either still renders in full: 3.6 is explicit that
    neither is a reason to skip the section.
    """

    checkpoint: Checkpoint
    cells: tuple[EvidenceCell, ...]
    sensor: SensorView
    findings: tuple[Finding, ...]
    missed_reason: str | None
    no_evidence: str | None


def checkpoint_views(
    record: Record,
    grids: dict[str, Sequence[DirectionCell]],
    gaps: Sequence[Gap],
    image_dir: Path | None,
) -> list[CheckpointView]:
    """One view per checkpoint, in the order the record listed them.

    3.2 renders a run-level finding inside the checkpoint it names as well as
    in the summary, so the findings here are a second appearance of the same
    entries and not a different set.
    """
    return [
        CheckpointView(
            checkpoint=checkpoint,
            cells=evidence_cells(grids.get(checkpoint.checkpoint_id, ()), image_dir),
            sensor=sensor_view(checkpoint, gaps),
            findings=tuple(
                finding for finding in record.findings
                if finding.checkpoint_id == checkpoint.checkpoint_id
            ),
            missed_reason=checkpoint.missed_reason if any(
                gap.item_id == checkpoint.checkpoint_id
                and gap.gap_type is GapType.MISSED_CHECKPOINT
                for gap in gaps
            ) else None,
            no_evidence=next(
                (
                    gap.detail for gap in gaps
                    if gap.item_id == checkpoint.checkpoint_id
                    and gap.gap_type is GapType.NO_EVIDENCE
                ),
                None,
            ),
        )
        for checkpoint in record.checkpoints
    ]


@dataclass(frozen=True)
class ZoneGroup:
    """One zone's checkpoints, under the header 3.1 asks for."""

    zone: str
    checkpoints: tuple[CheckpointView, ...]


def zone_groups(views: Sequence[CheckpointView]) -> list[ZoneGroup]:
    """The checkpoint views grouped by zone, zones in the order they first appear.

    3.1 groups the checkpoint sections by zone with a header per group, and 2.2
    calls `zone` the grouping key. Record order is kept inside a group rather
    than sorted: the record lists a route in the order it was driven, and
    sequence_number is optional, so sorting on it would reorder a run that did
    not record one.
    """
    grouped: dict[str, list[CheckpointView]] = {}
    for view in views:
        grouped.setdefault(view.checkpoint.zone, []).append(view)
    return [ZoneGroup(zone=zone, checkpoints=tuple(group)) for zone, group in grouped.items()]


# --- the run summary table (3.1) ---------------------------------------------
#
# One row per checkpoint across the whole run. 3.1 fixes the row and says
# nothing about the columns, so these are chosen: the two verdicts that cannot
# be collapsed, how much of the evidence arrived, what the sensor reported, and
# how many findings named the checkpoint. Every one is read back off the gaps
# or off a count already derived - nothing is worked out twice (decision 117).


@dataclass(frozen=True)
class SummaryRow:
    """One checkpoint's line in the run summary."""

    checkpoint: Checkpoint
    rgb: str
    thermal: str
    sensor: str
    findings: int


def evidence_tally(grid: Sequence[DirectionCell]) -> tuple[str, str]:
    """How many of the directions carry a usable RGB image, and a usable thermal.

    Counted out of the grid's own length rather than a fixed eight, so a config
    listing a different set of directions (5.7) still reads correctly.
    """
    total = len(grid)
    rgb = sum(1 for cell in grid if cell.rgb is not None and cell.rgb.readable)
    thermal = sum(1 for cell in grid if cell.thermal is not None and cell.thermal.readable)
    return f"{rgb} of {total}", f"{thermal} of {total}"


def sensor_summary(gaps: Sequence[Gap]) -> str:
    """What one checkpoint's sensor reported, in a few words.

    Reads the gaps already raised for that checkpoint. The table cannot say a
    reading was fine when detect_gaps found something wrong with it, because it
    is the same list the manifest carries (4.4).
    """
    kinds = {gap.gap_type: gap for gap in gaps}
    if GapType.SENSOR_UNAVAILABLE in kinds:
        return "Unavailable"

    offline = [
        device_name(gap.detail.split("=", 1)[0])
        for gap in gaps if gap.gap_type is GapType.SUBSYSTEM_OFFLINE
    ]
    stale = "stale" if GapType.STALE_READING in kinds else None

    parts = [", ".join(offline) + " offline"] if offline else []
    if stale:
        parts.append(stale)
    return "; ".join(parts) if parts else "Recorded"


def summary_rows(
    record: Record, grids: dict[str, Sequence[DirectionCell]], gaps: Sequence[Gap],
) -> list[SummaryRow]:
    """One row per checkpoint, in the order the record listed them.

    No total row. 3.1 asks for one row per checkpoint and nothing else, and the
    figures a total would add are the reconciliation page's job (3.3) - the same
    reasoning as decision 89 for the zone table.
    """
    rows = []
    for checkpoint in record.checkpoints:
        mine = [gap for gap in gaps if gap.item_id == checkpoint.checkpoint_id]
        rgb, thermal = evidence_tally(grids.get(checkpoint.checkpoint_id, ()))
        rows.append(SummaryRow(
            checkpoint=checkpoint,
            rgb=rgb,
            thermal=thermal,
            sensor=sensor_summary(mine),
            findings=sum(
                1 for finding in record.findings
                if finding.checkpoint_id == checkpoint.checkpoint_id
            ),
        ))
    return rows


# --- the zone telemetry summary (3.4) ----------------------------------------
#
# One row per zone: the sample count, the min, mean and max of three
# measurements, and how many alerts named the zone. derive.py has worked all of
# it out; nothing here counts anything.
#
# There is no particulate column, per decision 33. Where the device that would
# have filled one was off, that is a SUBSYSTEM_OFFLINE gap detect_gaps already
# raised, and the section says so - 4.4 requires every manifest gap to be
# visible in the PDF, and a rollup that drops a measurement without a word is
# the silent handling 3.6 rules out.

#: The three measurements 2.6 names and 3.4's table draws, with the unit each
#: is recorded in. A template loops over these rather than repeating nine
#: near-identical expressions.
ZONE_COLUMNS = (
    ("temperature_c", "Temp", "\u00b0C"),
    ("humidity_pct", "Humidity", "%"),
    ("vibration_rms_g", "Vibration RMS", "g"),
)


def measure(value: float | None, figures: int = 4) -> str | None:
    """One measurement, rounded for print and never in exponent form.

    Significant figures rather than a fixed number of decimal places: a zone's
    vibration sitting near 0.05 and its temperature near 30 need different
    decimals to read the same way, and the engine cannot know in advance what
    range a record will carry.

    derive.py leaves a mean unrounded on purpose, so the rounding happens here,
    once, where every other formatting decision lives. A value extreme enough
    that %g would reach for an exponent is written out in full instead: 5e-05
    is not a figure to put in front of a facility manager.
    """
    if value is None:
        return None
    text = f"{value:.{figures}g}"
    if "e" in text:
        text = f"{value:.10f}".rstrip("0").rstrip(".")
    return text


def device_name(flag: str) -> str:
    """The device a raw sensor flag is named after: adxl345_ok -> adxl345.

    TA-09 wants the block that was suppressed to name the device rather than
    say "a sensor", and the flag is where 5.7's config records that name.
    """
    return flag.removesuffix("_ok")


def zone_absence(stat: ZoneStat, samples: int) -> str:
    """Why one zone statistic has no numbers.

    Three different things leave the cell without a figure, and a reader has to
    be able to tell them apart - a blank that could mean any of the three is
    the confident wrong answer 0 rules out:

    - the device was off, so nothing it wrote is a measurement (TA-09 to TA-11)
    - the zone recorded no telemetry at all
    - telemetry was recorded, but none of it carried this value

    Which applies is read off what derive.py already worked out. Nothing is
    decided a second time here.
    """
    if stat.suppressed_by is not None:
        return f"Not recorded - {device_name(stat.suppressed_by)} offline"
    if samples == 0:
        return "No samples"
    return "Not recorded"


# --- config and provenance ---------------------------------------------------


def sections(config: dict) -> dict[str, bool]:
    """Which sections the config turns on.

    Every one of the seven is required. A flag nobody wrote is an error naming
    the key, never a silent false that drops a section out of the report without
    saying so (5.7).
    """
    return {name: setting(config, "sections", name) for name in SECTION_NAMES}


def provenance_line(provenance: Provenance, footer_text: str) -> str:
    """The footer sentence that appears on every page.

    5.6 requires the four provenance facts there, and 5.7's branding.footer_text
    is named for the footer, so both go in one line rather than competing for
    the same corner of the page.
    """
    return (
        f"{footer_text}"
        f"   |   Engine {provenance.engine_version}"
        f"   |   Template {provenance.template_version}"
        f"   |   Generated {provenance.generated_at}"
        f"   |   Run {provenance.source_run_id}"
    )


def logo_uri(config: dict) -> str | None:
    """An absolute file URI for the branding logo, or None when it is not there.

    5.7's rule about a missing key is about the key, not the file it points at.
    A logo that cannot be found is logged and left out; the report is still a
    report without a picture on the cover.
    """
    path = PROJECT_ROOT / setting(config, "branding", "logo_path")
    if not path.is_file():
        logger.warning("branding logo not found at %s; rendering without it", path)
        return None
    return path.resolve().as_uri()


def css_string(text: str) -> str:
    """One string, quoted for a CSS `content` property."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def runtime_css(config: dict, footer: str) -> str:
    """The stylesheet values that are not fixed by styles.css.

    Three things cannot live in a static file: the page size and brand colour
    are config (5.7), and the footer names the run it was generated for (5.6).
    Putting them here keeps those config keys real rather than decorative.

    Everything else about the page, page breaks included, stays in styles.css as
    5.5 requires.
    """
    return (
        f"@page {{ size: {setting(config, 'report', 'page_size')}; }}\n"
        f"@page {{ @bottom-left {{ content: {css_string(footer)}; }} }}\n"
        f":root {{ --primary-colour: {setting(config, 'branding', 'primary_colour')}; }}\n"
    )


# --- rendering ---------------------------------------------------------------


def render_html(
    record: Record,
    gaps: list[Gap],
    derived: DerivedValues,
    grids: dict[str, list[DirectionCell]],
    config: dict,
    image_dir: Path | None = None,
) -> str:
    """Render the whole report to HTML.

    Separate from render_pdf so the markup can be read and tested without
    producing a document.

    `image_dir` is where images.py writes the copies it has sized down (5.5).
    None means no resizing, which is what an HTML render wants: nothing is
    embedded, so there is no file size to keep within budget.
    """
    rows = count_rows(derived, disagreeing_counts(record))
    template = environment().get_template("full_report.html.j2")
    return template.render(
        record=record,
        gaps=gaps,
        derived=derived,
        grids=grids,
        sections=sections(config),
        logo_uri=logo_uri(config),
        count_rows=rows,
        counts_disagree=any(row.disagrees for row in rows),
        # Whether the run recorded no checkpoints is empty_record_gaps' answer,
        # not a second test of the same array inside a template (5.4).
        empty_record=any(gap.gap_type is GapType.EMPTY_RECORD for gap in gaps),
        count_conflicts=run_count_conflicts(gaps),
        missed=coverage_items(record, gaps, GapType.MISSED_CHECKPOINT),
        no_evidence=coverage_items(record, gaps, GapType.NO_EVIDENCE),
        verdict_style=verdict_style(record.final_status),
        finding_groups=finding_groups(record.findings),
        # 3.1 groups the alerts by severity and 3.5 orders them by time; one
        # function settles both, and reads its severities from the tally the
        # manifest prints so the two cannot disagree (4.2).
        alert_groups=alert_groups(record.sensor_alerts, derived.alerts_by_severity),
        # The Findings row of the reconciliation table, so the count at the head
        # of the findings section and the coverage page are one figure and not
        # two comparisons that happen to agree (5.4).
        findings_row=next(row for row in rows if row.name == "findings"),
        zone_columns=ZONE_COLUMNS,
        # Which measurement the zone table leaves out, and why, comes from the
        # gaps already detected rather than from a template testing a device
        # flag for itself (5.5).
        particulate_offline=offline_block_gaps(gaps, "particulate"),
        # Every absence a checkpoint section prints is read back off the gaps
        # detect_gaps raised, so 4.4's two directions hold by construction
        # rather than by the page and the manifest agreeing twice over (5.4).
        checkpoint_zones=zone_groups(checkpoint_views(record, grids, gaps, image_dir)),
        grid_columns=GRID_COLUMNS,
        summary_rows=summary_rows(record, grids, gaps),
    )


def render_pdf(
    record: Record,
    gaps: list[Gap],
    derived: DerivedValues,
    grids: dict[str, list[DirectionCell]],
    config: dict,
    output_dir: Path = OUTPUT_DIR,
    provenance: Provenance | None = None,
) -> Path:
    """Render one run to a PDF and return where it was written.

    `output_dir` and `provenance` are trailing arguments with defaults, so 5.3's
    call shape works unchanged while the pipeline passes the directory it was
    given and the stamp the manifest will carry. 5.6 wants the same four facts
    in the footer and the manifest, and one stamp shared between them is what
    makes the generation time the same in both.

    4.1 puts the PDF and the manifest side by side in output/, named from the
    run id exactly as recorded.
    """
    footer = provenance_line(
        provenance or stamp(record.run_id, config),
        setting(config, "branding", "footer_text"),
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"report_{record.run_id}.pdf"

    # The sized-down copies live only as long as it takes to embed them. They
    # are a rendering detail and not an output: 4.1 puts one PDF and one
    # manifest in output/ and nothing else.
    with tempfile.TemporaryDirectory(prefix="facilityops-images-") as image_dir:
        html = render_html(record, gaps, derived, grids, config, Path(image_dir))
        HTML(string=html, base_url=str(TEMPLATE_DIR)).write_pdf(
            path,
            stylesheets=[
                CSS(filename=str(STYLES_PATH)),
                CSS(string=runtime_css(config, footer)),
            ],
        )
    logger.info("rendered %d checkpoints to %s", len(record.checkpoints), path)
    return path
