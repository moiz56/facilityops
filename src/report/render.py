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
from functools import partial
from datetime import datetime
from pathlib import Path
from typing import Sequence

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from weasyprint import CSS, HTML

from common import OUTPUT_DIR, PROJECT_ROOT
from common.paths import ConfigError, ResolvedImage, decimals, setting, units
from common.provenance import Provenance, stamp
from common.schema import (
    Checkpoint, Finding, Point2D, Pose, Record, SensorAlert, SensorBlock, SensorWarning,
)
from report.derive import DerivedValues, ZoneStat
from report.gaps import RUN_LEVEL, Gap, GapType, disagreeing_counts, offline_block_gaps
from report.images import GRID_COLUMNS, ViewCell, prepare_image

logger = logging.getLogger("report.render")

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
#: The stylesheet used when `branding.stylesheet` says nothing.
DEFAULT_STYLESHEET = "styles.css"


def stylesheet_path(config: dict) -> Path:
    """The stylesheet `branding.stylesheet` names, or the default one.

    The key carries a file name and not a path, because the sheet is one of the
    ones shipped in templates/ rather than anywhere on disk: `styles.css` is the
    current sheet and `style_default.css` the one it was restyled from, kept so
    a run can be laid out the old way without editing either file.
    """
    block = config.get("branding")
    name = block.get("stylesheet", DEFAULT_STYLESHEET) if isinstance(block, dict) else DEFAULT_STYLESHEET
    if not isinstance(name, str) or Path(name).name != name:
        raise ConfigError(
            f"'branding.stylesheet' must name a file in templates/, not {name!r}"
        )
    path = TEMPLATE_DIR / name
    if not path.is_file():
        raise ConfigError(f"'branding.stylesheet' names {name!r}, which templates/ does not hold")
    return path

#: The seven sections 5.7 can switch on and off, in the order 3.1 renders them.
SECTION_NAMES = (
    "cover", "coverage", "findings", "alerts", "zone_telemetry",
    "checkpoints", "summary",
)

#: What the report actually renders, in order: the section switch it belongs to,
#: the anchor on its `<section>`, and how the contents page names it.
#:
#: Two entries are not sections of their own. The contents page is front matter
#: and answers to no switch, and the gap list rides on `coverage` because 5.7
#: names seven sections and an eighth key is a config the client's tests do not
#: know about (decision 153). Both still get a contents row, because a reader
#: looking for them needs a page number, and the manifest merges the gap list
#: into coverage's range so its ids stay the ones 5.7 defines.
RENDERED_BLOCKS = (
    ("cover", "cover", "Cover"),
    (None, "contents", "Contents"),
    ("coverage", "coverage", "Coverage and reconciliation"),
    ("coverage", "gaps", "All gaps"),
    ("findings", "findings", "Findings"),
    ("alerts", "alerts", "Sensor alerts"),
    ("zone_telemetry", "zone-telemetry", "Zone telemetry"),
    ("checkpoints", "checkpoints", "Checkpoints"),
    ("summary", "summary", "Run summary"),
)


@dataclass(frozen=True)
class ContentsRow:
    """One line of the contents page: what it is, and where it starts."""

    label: str
    page: int | None        # None on the measuring pass, before pages are known


@dataclass(frozen=True)
class PageMap:
    """Where everything landed in the PDF that was actually written.

    `sections` is the manifest's own shape (4.2): one entry per section the
    config turned on, with the real first and last page. `contents` is the same
    information as the contents page prints it.
    """

    count: int
    sections: tuple[dict, ...]
    contents: tuple[ContentsRow, ...]


def contents_rows(sections_on: dict[str, bool]) -> tuple[ContentsRow, ...]:
    """The contents page's lines, with no page numbers yet.

    Built before the document is laid out, so the page is the right height on
    the measuring pass and the numbers added afterwards do not move anything.
    """
    return tuple(
        ContentsRow(label=label, page=None)
        for switch, _, label in RENDERED_BLOCKS
        if switch is None or sections_on.get(switch)
    )


def page_map(pages: Sequence, sections_on: dict[str, bool]) -> PageMap:
    """Read back which page each section started on, from the laid-out document.

    `pages` is WeasyPrint's own list of pages, so the numbers describe the file
    that is about to be written rather than an estimate of it. 4.4 requires the
    manifest's section ranges to match the real PDF; this is the only place they
    come from.

    A section ends on the page before the next one starts, because every section
    begins on a fresh page (decision 50). The last one ends on the last page.
    """
    starts: dict[str, int] = {}
    for number, page in enumerate(pages, 1):
        for anchor in page.anchors:
            starts.setdefault(anchor, number)

    rendered = [
        (switch, anchor, label)
        for switch, anchor, label in RENDERED_BLOCKS
        if (switch is None or sections_on.get(switch)) and anchor in starts
    ]

    rows = tuple(
        ContentsRow(label=label, page=starts[anchor]) for _, anchor, label in rendered
    )

    # One manifest entry per section the config turned on. Blocks sharing a
    # switch - coverage and the gap list - share its range, so the ids stay the
    # ones 5.7 names and the range still covers every page they occupy.
    sections = []
    for index, (switch, anchor, _) in enumerate(rendered):
        if switch is None:
            continue
        following = [starts[a] for _, a, _ in rendered[index + 1:]]
        end = (following[0] - 1) if following else len(pages)
        if sections and sections[-1]["id"] == switch:
            sections[-1]["page_end"] = end
            continue
        sections.append({"id": switch, "page_start": starts[anchor], "page_end": end})

    return PageMap(count=len(pages), sections=tuple(sections), contents=rows)





# --- what a template is given ----------------------------------------------


def show(value: object, absent: str = "Not recorded") -> object:
    """A value, or the words that say it was never recorded.

    2.9 makes absent, null and "" the same thing, so all three read alike and a
    blank cell never stands in for a fact nobody wrote down.
    """
    if value is None or value == "":
        return absent
    return value


def percentage(value: int | float | None, absent: str = "Not recorded") -> str:
    """A declared percentage with its sign, or the words that say none was recorded."""
    if value is None:
        return absent
    return f"{value}%"


def lock_state(value: bool | None) -> str:
    """Whether the record was finalised, in the words the reader needs.

    2.1 calls `locked` "whether the record has been finalised". A report is read
    after the fact, so whether the file behind it was still being written to is
    material to what every figure in it means: true and false both print in
    full, and an absent flag says so rather than being read as either.
    """
    if value is None:
        return "Not recorded"
    if value:
        return "Yes - the record was finalised and is no longer being written to"
    return "No - the record was still open when this report was generated"


def moment(value: datetime | None, absent: str = "Not recorded") -> str:
    """One timestamp, with the offset it was recorded in.

    The offset is kept because a time without one says less than the record did.
    """
    if value is None:
        return absent
    return value.strftime("%Y-%m-%d %H:%M:%S %z")


def place(pose: Pose | Point2D | None, field_places: dict[str, int] | None = None) -> str | None:
    """Where something was recorded, as text, or None when nowhere was.

    Only the keys carrying a value are named. A two-key alert position and a
    four-key checkpoint pose therefore read the same way without either
    inventing the other's fields (2.5).

    Each key is formatted to its configured decimal places, through the same
    measure() every other figure goes through. %g used to give each value its
    own width, so one line read "x: -10.8734, y: -0.588941" - 4 places against
    6, from two numbers of the same kind measured the same way (decision 131).

    The colon after each key is what separates the name from its number. Set as
    "x 0.996, y -0.136" the pairs run together at a glance, and a negative sign
    reads as the only thing dividing them; "x: 0.996, y: -0.136" keeps the four
    pairs legible without a wider column.
    """
    if pose is None:
        return None
    field_places = field_places or {}
    named = (
        ("x", getattr(pose, "x", None)),
        ("y", getattr(pose, "y", None)),
        ("z", getattr(pose, "z", None)),
        ("yaw", getattr(pose, "yaw", None)),
    )
    return ", ".join(
        f"{name}: {measure(value, field_places.get(name))}"
        for name, value in named if value is not None
    ) or None


def environment(field_places: dict[str, int] | None = None) -> Environment:
    """The Jinja environment every template renders in.

    Autoescaping is on, so free text renders as text (5.5, TA-29). Undefined
    names raise instead of rendering empty, so a mistyped field in a template is
    a failure rather than a quietly blank cell.

    `place` is bound to the configured decimal places here rather than taking
    them as a filter argument: a coordinate's field name is inside the pose, not
    at the call site, so a template could not name it (decision 131). `measure`
    stays an ordinary filter, because there the caller does know its field.
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
    env.filters["percentage"] = percentage
    env.filters["lock_state"] = lock_state
    env.filters["place"] = partial(place, field_places=field_places or {})
    env.filters["measure"] = measure
    env.filters["zone_absence"] = zone_absence
    env.filters["severity_style"] = severity_style
    env.filters["verdict_style"] = verdict_style
    env.filters["status_style"] = status_style
    return env


# --- naming a gap on the page ------------------------------------------------


@dataclass(frozen=True)
class GapNote:
    """One gap as the page prints it: its type, and the sentence it carried.

    Every absence the report states because detect_gaps raised a gap carries
    the gap type beside it, so a reader can take the word off the page, find it
    in the manifest's `gaps` array, and be sure the two are the same finding
    (4.4). The type is the StrEnum's own value, never a string a template typed
    out, so a name cannot drift from the enum it claims to be.
    """

    gap_type: str
    detail: str


def gaps_for(gaps: Sequence[Gap], checkpoint_id: str) -> list[Gap]:
    """One checkpoint's gaps, plus the run-level ones that describe every checkpoint.

    A device off for the whole run raises one SUBSYSTEM_OFFLINE against __run__
    rather than one per checkpoint (decision 171), and the block it names is
    still not a measurement at any of them. A section reading only the gaps
    filed under its own id would print the placeholder zeros that 2.8 calls the
    single most important thing this milestone must not do.
    """
    return [
        gap for gap in gaps
        if gap.item_id == checkpoint_id
        or (gap.item_id == RUN_LEVEL and gap.gap_type is GapType.SUBSYSTEM_OFFLINE)
    ]


def refused_fields(gaps: Sequence[Gap], item_id: str) -> dict[str, GapNote]:
    """The fields one item had refused for being the wrong type, by field name.

    The name is read off the front of the detail the gap carries -
    "confidence: is str '0.94', expected int or float" - so there is no second
    copy of the field name to fall out of step with the one the loader wrote.

    A cell whose field is in here prints the gap type in place of a value.
    "Not scored" is what an absent confidence reads as, and a refused one is a
    different fact: the record did carry something, and it could not be used
    (decision 168).
    """
    return {
        gap.detail.split(":", 1)[0]: GapNote(str(gap.gap_type), gap.detail)
        for gap in gaps
        if gap.gap_type is GapType.INCORRECT_DATATYPE and gap.item_id == item_id
    }


def gap_note(
    gaps: Sequence[Gap], gap_type: GapType, item_id: str | None = None
) -> GapNote | None:
    """The first gap of one type, as a note, or None when none was raised.

    `item_id` narrows it to one checkpoint. None means the caller has already
    narrowed the list, or wants the run-level gap.
    """
    for gap in gaps:
        if gap.gap_type is gap_type and (item_id is None or gap.item_id == item_id):
            return GapNote(str(gap.gap_type), gap.detail)
    return None


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
    gap_type: str
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
            gap_type=str(gap.gap_type),
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


# --- every gap in one list (4.4) ---------------------------------------------


@dataclass(frozen=True)
class GapRow:
    """One line of the consolidated gap list: what the manifest holds, readable.

    `item_name` is the checkpoint's name where the id names one, printed under
    the id rather than instead of it: the id is what matches the manifest, the
    name is what a reader recognises.
    """

    gap_type: str
    item_id: str
    item_name: str | None
    detail: str


def gap_rows(record: Record, gaps: Sequence[Gap]) -> list[GapRow]:
    """Every gap, in the order detect_gaps returned them.

    That order is the manifest's order too - per checkpoint in route order,
    then the run-level ones - so the two lists can be read side by side without
    sorting either. 4.4 requires every manifest gap to appear in the PDF; this
    is where an auditor can check that by eye instead of by diff.

    Nothing is filtered, collapsed or ranked. A gap the reader has already seen
    inline appears here as well, because this table's job is to be the same
    list, not a shorter one.
    """
    by_id = {checkpoint.checkpoint_id: checkpoint for checkpoint in record.checkpoints}
    return [
        GapRow(
            gap_type=str(gap.gap_type),
            item_id=gap.item_id,
            item_name=(
                by_id[gap.item_id].checkpoint_name if gap.item_id in by_id else None
            ),
            detail=gap.detail,
        )
        for gap in gaps
    ]


# --- the evidence count check (2.7, TA-25) -----------------------------------


@dataclass(frozen=True)
class EvidenceRow:
    """One checkpoint's evidence count: declared, listed, and actually resolved.

    `declared` holds every distinct count the event log claimed for this
    checkpoint - a tuple rather than one number, because nothing stops two
    events declaring different counts for the same checkpoint, and printing
    whichever came last would hide that.

    Why a row does not reconcile is kept in two fields because two rules answer
    two questions: `count_reasons` is the COUNT_MISMATCH raised against the
    declared count, `image_reasons` one MISSING_IMAGE per listed path that gave
    no usable file. Both empty means the check ran and found nothing wrong,
    which is a different thing from the check not having run.
    """

    checkpoint_id: str
    checkpoint_name: str
    declared: tuple[int, ...]
    referenced: int               # evidence paths the checkpoint listed
    resolved: int                 # of those, the ones that gave a usable file
    count_reasons: tuple[str, ...]
    image_reasons: tuple[str, ...]

    @property
    def reasons(self) -> tuple[str, ...]:
        """Every sentence under the row: the claim first, then the files."""
        return self.count_reasons + self.image_reasons

    @property
    def mismatch(self) -> bool:
        """Whether anything about this checkpoint's evidence failed to reconcile."""
        return bool(self.reasons)

    @property
    def gap_types(self) -> tuple[str, ...]:
        """The gap types this row raised, named as the manifest files them.

        The row prints these rather than one label of its own: declared 12,
        listed 12 and one file unreadable is a MISSING_IMAGE and not a count
        conflict at all, and a row naming a gap the manifest does not carry
        would break 4.4 in the direction nobody checks.
        """
        return (
            ("COUNT_MISMATCH",) if self.count_reasons else ()
        ) + (
            ("MISSING_IMAGE",) if self.image_reasons else ()
        )


def evidence_rows(record: Record, gaps: list[Gap]) -> list[EvidenceRow]:
    """One row per checkpoint: the declared evidence count against what resolved.

    2.7 makes `evidence_count` a claim to be cross-checked and TA-25 asks for
    the discrepancy per checkpoint. Every checkpoint gets a row whether or not
    anything was wrong with it: a check that ran and passed silently reads, to
    an auditor, exactly like a check nobody wrote.

    Three figures, none of them corrected: what the event log declared, how many
    paths the checkpoint listed, and how many of those paths resolved to a
    usable file. A row that does not reconcile carries the sentences the gaps
    gave - the COUNT_MISMATCH raised against the declared count, and one
    MISSING_IMAGE per file that did not resolve, each naming the file and the
    reason resolution gave (decision 23).

    Those sentences are read back off detect_gaps' output rather than worked out
    again here, so a row is marked exactly when the manifest carries a gap for
    it (4.4, 5.4).
    """
    declared: dict[str, list[int]] = {}
    for event in record.event_log:
        # An event declaring a count while naming no checkpoint claims nothing
        # about any checkpoint's evidence. gaps.py passes over it for the same
        # reason.
        if event.evidence_count is None or event.checkpoint_id is None:
            continue
        counts = declared.setdefault(event.checkpoint_id, [])
        if event.evidence_count not in counts:
            counts.append(event.evidence_count)

    # The count sentence first, then one line per file that did not resolve:
    # the claim the row is about, and then what it turned out to be about. Two
    # gap types, because they answer two questions - the checkpoint's
    # COUNT_MISMATCH compares the declared count against the array,
    # MISSING_IMAGE says a listed path gave no usable file. A run-level
    # COUNT_MISMATCH is one of the seven coverage rows and belongs to the table
    # higher up the page, not here.
    counted: dict[str, list[str]] = {}
    unresolved: dict[str, list[str]] = {}
    for gap in gaps:
        if gap.gap_type is GapType.MISSING_IMAGE:
            unresolved.setdefault(gap.item_id, []).append(gap.detail)
        elif gap.gap_type is GapType.COUNT_MISMATCH and gap.item_id != RUN_LEVEL:
            counted.setdefault(gap.item_id, []).append(gap.detail)

    rows = [
        EvidenceRow(
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_name=checkpoint.checkpoint_name,
            declared=tuple(declared.get(checkpoint.checkpoint_id, ())),
            referenced=len(checkpoint.evidence_images),
            resolved=(
                len(checkpoint.evidence_images)
                - len(unresolved.get(checkpoint.checkpoint_id, ()))
            ),
            count_reasons=tuple(counted.get(checkpoint.checkpoint_id, ())),
            image_reasons=tuple(unresolved.get(checkpoint.checkpoint_id, ())),
        )
        for checkpoint in record.checkpoints
    ]

    # A count declared against a checkpoint the run never recorded. detect_gaps
    # raises that as a COUNT_MISMATCH under the id the event named, and the row
    # carries its sentence rather than writing a second copy of it.
    # Without a row the claim would be in the manifest and nowhere in the
    # document.
    known = {checkpoint.checkpoint_id for checkpoint in record.checkpoints}
    rows += [
        EvidenceRow(
            checkpoint_id=item_id,
            checkpoint_name=item_id,
            declared=tuple(declared.get(item_id, ())),
            referenced=0,
            resolved=0,
            count_reasons=tuple(said),
            image_reasons=(),
        )
        for item_id, said in counted.items()
        if item_id not in known
    ]
    return rows


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
    One cell is one view, RGB above its thermal counterpart, and a checkpoint
    has as many cells as it recorded views (decision 174).
    """

    view: str | None
    rgb_src: str | None
    rgb_absent: str | None
    rgb_gap: str | None
    thermal_src: str | None
    thermal_absent: str | None
    thermal_gap: str | None


def image_absence(image: ResolvedImage | None, uncaptured: str, unusable: str) -> str | None:
    """What a cell says in place of a picture, or None when it has one to show.

    3.6 asks for three situations to read differently: nothing was captured for
    that view in that modality, a path was recorded and does not resolve, and a
    file is there but empty or undecodable. The first is this cell's own wording; the
    other two carry the reason resolution already worked out, which is the same
    reason the MISSING_IMAGE gap carries.
    """
    if image is None:
        return uncaptured
    if not image.readable:
        return f"{unusable} - {image.reason}"
    return None


def image_absence_gap(image: ResolvedImage | None) -> str | None:
    """The gap type behind an empty RGB cell, or None when its absence raised none.

    A path that was recorded and did not resolve is a MISSING_IMAGE, one per
    file. A view nobody photographed raised nothing of its own: there is no path
    for a gap to be about, and an empty evidence array is the checkpoint's
    NO_EVIDENCE rather than this cell's.
    """
    if image is not None and not image.readable:
        return str(GapType.MISSING_IMAGE)
    return None


def image_source(image: ResolvedImage | None, image_dir: Path | None) -> str | None:
    """A URI for the file to embed, or None when there is nothing to embed.

    Sizing the file down happens in images.py, per 5.5.
    """
    path = prepare_image(image, image_dir) if image is not None else None
    return path.resolve().as_uri() if path is not None else None


@dataclass(frozen=True)
class EvidenceView:
    """One checkpoint's evidence: the cells to draw, or a line that says why not.

    `columns` is how wide the grid fans out for this checkpoint: one cell fills
    the row, two sit side by side, and a checkpoint with more than
    `grid_columns` views wraps, so seven views draw four and then three
    (decision 174). Every cell in one checkpoint stays the same size; only the
    number of them changes.

    Two shapes of grid say the same thing in every cell, and both collapse:

    `empty` - the checkpoint recorded no evidence path at all, so a grid of
    cells would each repeat the sentence already printed above them.

    `thermal_absent` - every view carried an RGB and none carried a thermal, so
    the thermal halves are one fact, not one per cell. The cells keep their RGB
    and the fact prints once beneath the grid.

    Neither collapses a grid that says different things in different cells. A
    path that was recorded and did not resolve carries its own reason, so a
    grid holding one of those keeps every cell (decision 150).
    """

    cells: tuple[EvidenceCell, ...]
    columns: int              # cells on a row, capped at the configured grid_columns
    rgb: str                  # "8 of 8", the views carrying a usable RGB
    thermal: str
    empty: bool
    thermal_absent: bool
    thermal_note: GapNote | None   # the MISSING_THERMAL behind a collapsed thermal


def evidence_view(
    checkpoint: Checkpoint, grid: Sequence[ViewCell], image_dir: Path | None,
    gaps: Sequence[Gap] = (),
) -> EvidenceView:
    """One checkpoint's grid, and whether it has anything to draw.

    `empty` is asked of the record rather than of the grid: a checkpoint that
    listed paths the grid could make nothing of has something wrong with it
    that an empty grid still shows, while a checkpoint that listed nothing has
    one fact and no need of a grid to carry it.
    """
    rgb, thermal = evidence_tally(grid)
    cells = evidence_cells(grid, image_dir)
    return EvidenceView(
        cells=cells,
        # min, not a fixed width: a checkpoint with one view gets a one-column
        # grid and a full-width picture rather than a quarter-width one beside
        # three empty slots that nothing was ever expected to fill.
        columns=max(1, min(len(cells), GRID_COLUMNS)),
        rgb=rgb,
        thermal=thermal,
        empty=not checkpoint.evidence_images,
        thermal_absent=(
            bool(grid)
            and all(cell.thermal is None for cell in grid)
            and any(cell.rgb is not None for cell in grid)
        ),
        # The line that replaces a column of identical cells names the same gap
        # those cells did, taken from detect_gaps rather than typed into the
        # template (decision 144).
        thermal_note=gap_note(gaps, GapType.MISSING_THERMAL, checkpoint.checkpoint_id),
    )


def evidence_cells(
    grid: Sequence[ViewCell], image_dir: Path | None
) -> tuple[EvidenceCell, ...]:
    """One checkpoint's cells, ready to draw - one per view it recorded.

    A cell that shows words instead of a picture names the gap type behind them
    where there is one. An RGB the record referenced and resolution could not
    open is a MISSING_IMAGE; a view whose RGB arrived with no thermal beside it
    is the MISSING_THERMAL detect_gaps raised for that checkpoint, which is why
    the thermal tag depends on the RGB rather than on the thermal itself
    (decision 14).
    """
    return tuple(
        EvidenceCell(
            view=cell.view,
            rgb_src=image_source(cell.rgb, image_dir),
            rgb_absent=image_absence(cell.rgb, "Image not captured", "Image not available"),
            rgb_gap=image_absence_gap(cell.rgb),
            thermal_src=image_source(cell.thermal, image_dir),
            thermal_absent=image_absence(cell.thermal, "No thermal", "Thermal not available"),
            thermal_gap=(
                image_absence_gap(cell.thermal)
                or (str(GapType.MISSING_THERMAL) if cell.thermal is None and cell.rgb is not None else None)
            ),
        )
        for cell in grid
    )


# --- the sensor table --------------------------------------------------------

#: The three measurement blocks 3.2 draws, in the order it names them, and the
#: fields each carries. The keys are the block names 5.7's subsystem_flags maps
#: its device flags onto, so the table and the trust rule cannot be talking
#: about different blocks.
#:
#: No unit appears here. The record states none - `accel_x` is a bare 0.157 -
#: so a unit written beside a field in this module would be the engine's own
#: assumption printed as though it were a property of the data, and unreadable
#: as such from the page. 5.7's `units` block holds them instead, where
#: config_hash carries the claim (decision 130).
SENSOR_BLOCKS = (
    ("environment", "Environment", (
        ("temperature_c", "Temperature"),
        ("humidity_pct", "Humidity"),
        ("pressure_hpa", "Pressure"),
    )),
    ("accelerometer", "Accelerometer", (
        ("accel_x", "Acceleration X"),
        ("accel_y", "Acceleration Y"),
        ("accel_z", "Acceleration Z"),
        ("vibration_peak", "Vibration peak"),
        ("vibration_rms_g", "Vibration RMS"),
    )),
    ("particulate", "Particulate", (
        ("pm1_0", "PM1.0"),
        ("pm2_5", "PM2.5"),
        ("pm4_0", "PM4.0"),
        ("pm10", "PM10"),
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
    absent_gap: str | None      # the gap type behind `absent`, where one was raised
    #: label, value, and the gap type where the field was refused rather than read
    rows: tuple[tuple[str, str, str | None], ...]


@dataclass(frozen=True)
class SensorView:
    """A checkpoint's whole sensor table.

    `unavailable` set means the reading is disqualified entirely and there are
    no blocks: 3.6 has the table read "Sensor unavailable" rather than listing
    three blocks that each say the same thing.

    `stale` set means the values still render, flagged with their age (2.8).
    """

    unavailable: GapNote | None
    stale: GapNote | None
    blocks: tuple[SensorBlockView, ...]
    warnings: tuple[SensorWarning, ...]


def reading(value: float | None, unit: str | None, places: int | None = None) -> str:
    """One measurement with its unit, or the words that say none was recorded.

    No configured unit prints the bare figure. The alternative is to supply one,
    and a reader cannot tell a supplied unit from a recorded one once both are
    set in the same type - so an unlabelled number is the honest output, and the
    config is where a unit stops being a guess (decision 130).
    """
    if value is None:
        return "Not recorded"
    if unit is None:
        return measure(value, places)
    return f"{measure(value, places)} {unit}"


def sensor_block_view(
    sensor: SensorBlock, gaps: Sequence[Gap], key: str, name: str,
    fields: Sequence[tuple[str, str]], field_units: dict[str, str],
    field_places: dict[str, int], refused: dict[str, GapNote] | None = None,
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
            name=name,
            absent=f"Not recorded - {device_name(flag)} offline",
            absent_gap=str(GapType.SUBSYSTEM_OFFLINE),
            rows=(),
        )

    # A block the record simply did not carry. No flag disqualified it, so
    # detect_gaps raised nothing and there is no gap type to name (decision 21).
    block = getattr(sensor, key, None)
    if block is None:
        return SensorBlockView(name=name, absent="Not recorded", absent_gap=None, rows=())

    # A field the loader refused shows the gap type instead of a figure. The
    # value is None either way; what differs is whether the record carried
    # something that could not be used (decision 168).
    refused = refused or {}
    return SensorBlockView(
        name=name,
        absent=None,
        absent_gap=None,
        rows=tuple(
            (
                label,
                reading(
                    getattr(block, field, None),
                    field_units.get(field),
                    field_places.get(field),
                ),
                refused[field].gap_type if field in refused else None,
            )
            for field, label in fields
        ),
    )


def sensor_view(
    checkpoint: Checkpoint, gaps: Sequence[Gap], field_units: dict[str, str],
    field_places: dict[str, int], refused: dict[str, GapNote] | None = None,
) -> SensorView:
    """One checkpoint's sensor table, built from its own gaps.

    The order matters and is 2.8's: a reading that is disqualified as a whole
    never reaches the per-block question, which is also why detect_gaps raises
    no SUBSYSTEM_OFFLINE alongside a SENSOR_UNAVAILABLE (decision 10).
    """
    mine = gaps_for(gaps, checkpoint.checkpoint_id)

    unavailable = gap_note(mine, GapType.SENSOR_UNAVAILABLE)
    if unavailable is not None:
        return SensorView(unavailable=unavailable, stale=None, blocks=(), warnings=())

    # detect_gaps always raises SENSOR_UNAVAILABLE for a checkpoint with no
    # sensor key at all (TA-14), so this is reached only by a caller that
    # passed a partial gap list. It says the same thing rather than failing on
    # the attribute, because a missing reading is exactly what it is.
    sensor = checkpoint.sensor
    if sensor is None:
        return SensorView(
            unavailable=GapNote(
                str(GapType.SENSOR_UNAVAILABLE), "no sensor reading was recorded",
            ),
            stale=None, blocks=(), warnings=(),
        )

    return SensorView(
        unavailable=None,
        stale=gap_note(mine, GapType.STALE_READING),
        blocks=tuple(
            sensor_block_view(
                sensor, mine, key, name, fields, field_units, field_places, refused,
            )
            for key, name, fields in SENSOR_BLOCKS
        ),
        warnings=sensor.warnings,
    )


# --- the block as a whole ----------------------------------------------------


@dataclass(frozen=True)
class CheckpointView:
    """Everything 3.2 draws for one checkpoint.

    `missed`, `no_evidence` and `no_findings` are the gaps that were raised for
    those three situations, not a second reading of the record. Each carries its
    type as well as its sentence, so the words on the page name the entry an
    auditor will find in the manifest. A checkpoint carrying any of them still
    renders in full: 3.6 is explicit that none is a reason to skip the section.
    """

    checkpoint: Checkpoint
    evidence: EvidenceView
    sensor: SensorView
    findings: tuple[Finding, ...]
    missed: GapNote | None
    no_evidence: GapNote | None
    no_findings: GapNote | None
    datatypes: tuple[GapNote, ...]
    refused: dict[str, GapNote]     # field name -> the refusal, for the cell itself


def checkpoint_views(
    record: Record,
    grids: dict[str, Sequence[ViewCell]],
    gaps: Sequence[Gap],
    image_dir: Path | None,
    field_units: dict[str, str],
    field_places: dict[str, int],
) -> list[CheckpointView]:
    """One view per checkpoint, in the order the record listed them.

    3.2 renders a run-level finding inside the checkpoint it names as well as
    in the summary, so the findings here are a second appearance of the same
    entries and not a different set.
    """
    views = []
    for checkpoint in record.checkpoints:
        refused = refused_fields(gaps, checkpoint.checkpoint_id)
        views.append(_checkpoint_view(
            record, checkpoint, grids, gaps, image_dir, field_units, field_places, refused,
        ))
    return views


def _checkpoint_view(
    record: Record,
    checkpoint: Checkpoint,
    grids: dict[str, Sequence[ViewCell]],
    gaps: Sequence[Gap],
    image_dir: Path | None,
    field_units: dict[str, str],
    field_places: dict[str, int],
    refused: dict[str, GapNote],
) -> CheckpointView:
    """One checkpoint's view. Split out so the refusals are read once and shared."""
    return (
        CheckpointView(
            checkpoint=checkpoint,
            evidence=evidence_view(
                checkpoint, grids.get(checkpoint.checkpoint_id, ()), image_dir, gaps,
            ),
            sensor=sensor_view(checkpoint, gaps, field_units, field_places, refused),
            findings=tuple(
                finding for finding in record.findings
                if finding.checkpoint_id == checkpoint.checkpoint_id
            ),
            missed=gap_note(gaps, GapType.MISSED_CHECKPOINT, checkpoint.checkpoint_id),
            no_evidence=gap_note(gaps, GapType.NO_EVIDENCE, checkpoint.checkpoint_id),
            no_findings=gap_note(gaps, GapType.NO_FINDINGS, checkpoint.checkpoint_id),
            # Every field of this checkpoint the loader refused for being the
            # wrong type. All of them, not the first: a record that got one
            # field wrong may have got several, and each names its own field.
            datatypes=tuple(
                GapNote(str(gap.gap_type), gap.detail)
                for gap in gaps
                if gap.gap_type is GapType.INCORRECT_DATATYPE
                and gap.item_id == checkpoint.checkpoint_id
            ),
            refused=refused,
        )
    )


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
    sensor_gaps: tuple[str, ...]   # the gap types behind `sensor`, in fixed order
    findings: int


def evidence_tally(grid: Sequence[ViewCell]) -> tuple[str, str]:
    """How many of the recorded views carry a usable RGB image, and a usable thermal.

    Counted out of the grid's own length, which is how many views the checkpoint
    recorded. "1 of 1" on a route that photographs each rack once says the same
    thing "8 of 8" says on a compass route: everything recorded arrived.
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
        return "reading disqualified"

    offline = [
        device_name(gap.detail.split("=", 1)[0])
        for gap in gaps if gap.gap_type is GapType.SUBSYSTEM_OFFLINE
    ]
    stale = "stale" if GapType.STALE_READING in kinds else None

    parts = [", ".join(offline) + " offline"] if offline else []
    if stale:
        parts.append(stale)
    return "; ".join(parts) if parts else "Recorded"


def sensor_summary_gaps(gaps: Sequence[Gap]) -> tuple[str, ...]:
    """The gap types behind one checkpoint's sensor cell, worst first.

    The cell already says what happened in a few words; these name the entries
    that say it in the manifest, so a reader working down the summary can find
    the same finding without opening the checkpoint's own section (4.4).

    SENSOR_UNAVAILABLE stands alone, because detect_gaps raises nothing else
    beside it (decision 19) and sensor_summary says nothing else either.
    """
    kinds = {gap.gap_type for gap in gaps}
    if GapType.SENSOR_UNAVAILABLE in kinds:
        return (str(GapType.SENSOR_UNAVAILABLE),)
    return tuple(
        str(kind) for kind in (GapType.SUBSYSTEM_OFFLINE, GapType.STALE_READING)
        if kind in kinds
    )


def summary_rows(
    record: Record, grids: dict[str, Sequence[ViewCell]], gaps: Sequence[Gap],
) -> list[SummaryRow]:
    """One row per checkpoint, in the order the record listed them.

    No total row. 3.1 asks for one row per checkpoint and nothing else, and the
    figures a total would add are the reconciliation page's job (3.3) - the same
    reasoning as decision 89 for the zone table.
    """
    rows = []
    for checkpoint in record.checkpoints:
        mine = gaps_for(gaps, checkpoint.checkpoint_id)
        rgb, thermal = evidence_tally(grids.get(checkpoint.checkpoint_id, ()))
        rows.append(SummaryRow(
            checkpoint=checkpoint,
            rgb=rgb,
            thermal=thermal,
            sensor=sensor_summary(mine),
            sensor_gaps=sensor_summary_gaps(mine),
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

#: The three measurements 2.6 names and 3.4's table draws. A template loops over
#: these rather than repeating nine near-identical expressions.
#:
#: Neither the unit nor the decimal places are here, for the same reason they
#: are not in SENSOR_BLOCKS: both are config, not properties of the field
#: (decisions 130 and 131). zone_columns() attaches whatever 5.7 configured
#: before the template sees it.
ZONE_COLUMNS = (
    ("temperature_c", "Temp"),
    ("humidity_pct", "Humidity"),
    ("vibration_rms_g", "Vibration RMS"),
)


#: 3.4's three measurements, split into the two tables they are read in, with
#: the device each set comes from. Nine numeric columns across one A4 width
#: could not be followed across a row; two tables of three and six can, and the
#: split falls where the data does - temperature and humidity are the bme680's,
#: vibration is the adxl345's, and it is the one a device flag suppresses on its
#: own (decision 164).
ZONE_TABLES = (
    ("Environment", ("temperature_c", "humidity_pct"), True),
    ("Vibration", ("vibration_rms_g",), False),
)


def zone_column_groups(
    field_units: dict[str, str], field_places: dict[str, int],
) -> tuple[tuple[str, tuple, bool], ...]:
    """The zone table's columns, grouped into the tables that carry them.

    Each group is a heading, its resolved columns, and whether that table
    carries the alerts count - which belongs beside the environment figures,
    since an alert is a fact about the zone rather than about a measurement.
    """
    resolved = {column[0]: column for column in zone_columns(field_units, field_places)}
    return tuple(
        (title, tuple(resolved[field] for field in fields if field in resolved), alerts)
        for title, fields, alerts in ZONE_TABLES
    )


def zone_columns(
    field_units: dict[str, str], field_places: dict[str, int],
) -> tuple[tuple[str, str, str | None, int | None], ...]:
    """3.4's columns with their configured unit and decimal places attached.

    None for either means none was configured. The template is handed the
    resolved row rather than the mappings, so it never looks either up for
    itself - the same reason the sensor table resolves its own in
    sensor_block_view (5.4). The min, mean and max of one column all take the
    field's places, which is what keeps a column to one precision.
    """
    return tuple(
        (field, label, field_units.get(field), field_places.get(field))
        for field, label in ZONE_COLUMNS
    )


def measure(value: float | None, places: int | None = None, figures: int = 4) -> str | None:
    """One measurement, rounded for print and never in exponent form.

    `places` is the field's configured decimal places and trailing zeros are
    kept, so every figure in a column has the same precision and 30.0 prints as
    "30.00" beside a neighbour's "29.73" rather than as "30". Two readings off
    one instrument should not look as though one was taken more carefully
    (decision 131).

    Without a configured entry the old rule applies: 4 significant figures. It
    suits a field whose range is not known in advance, and it is what every
    unconfigured field still gets.

    derive.py leaves a mean unrounded on purpose, so the rounding happens here,
    once, where every other formatting decision lives. A value extreme enough
    that %g would reach for an exponent is written out in full instead: 5e-05
    is not a figure to put in front of a facility manager. Fixed places never
    reach for one.
    """
    if value is None:
        return None
    if places is not None:
        return f"{value:.{places}f}"
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


def header_lines(record: Record) -> tuple[str, str]:
    """What the running head says on the left and on the right of every page.

    A page separated from the rest of the file still has to say what document
    it belongs to, which is the same reason 5.6 puts the run id in the footer.
    The facility name is what a reader recognises; the run id is what they
    match against the manifest.
    """
    # An en dash, not a hyphen: the head sets a title against a name rather than
    # joining two words, and a hyphen at that size reads as a typo in a document
    # that gets printed.
    return f"Inspection Report \u2013 {record.facility_name}", f"Run {record.run_id}"


def runtime_css(config: dict, footer: str, header: tuple[str, str] | None = None) -> str:
    """The stylesheet values that are not fixed by styles.css.

    Four things cannot live in a static file: the page size and brand colour
    are config (5.7), and the footer and running head name the run they were
    generated for (5.6). Putting them here keeps those config keys real rather
    than decorative.

    Everything else about the page, page breaks included, stays in styles.css as
    5.5 requires.
    """
    css = (
        f"@page {{ size: {setting(config, 'report', 'page_size')}; }}\n"
        f"@page {{ @bottom-left {{ content: {css_string(footer)}; }} }}\n"
        f":root {{ --primary-colour: {setting(config, 'branding', 'primary_colour')}; }}\n"
    )
    if header is not None:
        left, right = header
        css += (
            f"@page {{ @top-left {{ content: {css_string(left)}; }} }}\n"
            f"@page {{ @top-right {{ content: {css_string(right)}; }} }}\n"
        )
    return css


# --- rendering ---------------------------------------------------------------


def render_html(
    record: Record,
    gaps: list[Gap],
    derived: DerivedValues,
    grids: dict[str, list[ViewCell]],
    config: dict,
    image_dir: Path | None = None,
    contents: Sequence[ContentsRow] = (),
) -> str:
    """Render the whole report to HTML.

    Separate from render_pdf so the markup can be read and tested without
    producing a document.

    `image_dir` is where images.py writes the copies it has sized down (5.5).
    None means no resizing, which is what an HTML render wants: nothing is
    embedded, so there is no file size to keep within budget.
    """
    rows = count_rows(derived, disagreeing_counts(record))
    evidence = evidence_rows(record, gaps)
    # Read once and threaded down. A unit is a config value, so the sensor table
    # and the zone table read the same mapping and cannot print one field two
    # ways (decision 130).
    field_units = units(config)
    field_places = decimals(config)
    template = environment(field_places).get_template("full_report.html.j2")
    section_switches = sections(config)
    return template.render(
        record=record,
        # The contents page is rendered twice: once with no page numbers, to
        # find out where everything landed, and once with the numbers that
        # measuring produced (decision 155).
        contents=contents or contents_rows(section_switches),
        gaps=gaps,
        derived=derived,
        grids=grids,
        sections=section_switches,
        logo_uri=logo_uri(config),
        count_rows=rows,
        counts_disagree=any(row.disagrees for row in rows),
        # Whether the run recorded no checkpoints is empty_record_gaps' answer,
        # not a second test of the same array inside a template (5.4). A note
        # rather than a flag, so the page names the gap the manifest carries.
        empty_record=gap_note(gaps, GapType.EMPTY_RECORD),
        count_conflicts=run_count_conflicts(gaps),
        # The evidence count check, per checkpoint, whether or not it found
        # anything: a check the document never mentions cannot be told from one
        # that was never run (TA-25). The rows that did not reconcile are
        # separated here rather than in the template, so the reasons listed
        # under the table are the rows the table marked (5.4).
        evidence_rows=evidence,
        evidence_mismatches=[row for row in evidence if row.mismatch],
        # Every gap in one table, in the manifest's own order, so 4.4's
        # correspondence can be checked by eye (decision 151).
        gap_rows=gap_rows(record, gaps),
        run_level=RUN_LEVEL,
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
        zone_columns=zone_columns(field_units, field_places),
        zone_column_groups=zone_column_groups(field_units, field_places),
        # Which measurement the zone table leaves out, and why, comes from the
        # gaps already detected rather than from a template testing a device
        # flag for itself (5.5).
        particulate_offline=offline_block_gaps(gaps, "particulate"),
        # Every absence a checkpoint section prints is read back off the gaps
        # detect_gaps raised, so 4.4's two directions hold by construction
        # rather than by the page and the manifest agreeing twice over (5.4).
        checkpoint_zones=zone_groups(
            checkpoint_views(record, grids, gaps, image_dir, field_units, field_places)
        ),
        # No grid_columns here any more: how wide a grid fans out is a property
        # of the checkpoint, and each EvidenceView carries its own (decision 174).
        summary_rows=summary_rows(record, grids, gaps),
    )


def render_pdf(
    record: Record,
    gaps: list[Gap],
    derived: DerivedValues,
    grids: dict[str, list[ViewCell]],
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

    5.3 pins this signature and its return, so where the sections landed comes
    back from render_document instead. The work is the same one render.
    """
    return render_document(
        record, gaps, derived, grids, config, output_dir, provenance,
    )[0]


def render_document(
    record: Record,
    gaps: list[Gap],
    derived: DerivedValues,
    grids: dict[str, list[ViewCell]],
    config: dict,
    output_dir: Path = OUTPUT_DIR,
    provenance: Provenance | None = None,
) -> tuple[Path, PageMap]:
    """Render one run to a PDF, and report where each section landed in it.

    The PDF is laid out before it is written, so the page numbers the manifest
    carries and the contents page prints are read off the document that is
    about to be written rather than guessed at (4.4).

    Laid out more than once, because the contents page prints numbers that its
    own height can move. The first pass carries the rows with no numbers, the
    second the numbers the first produced; a third settles the rare case where
    filling them in pushed a section over a page boundary. It stops as soon as
    the numbers it rendered are the numbers it measured, so the usual run is two
    passes.
    """
    footer = provenance_line(
        provenance or stamp(record.run_id, config),
        setting(config, "branding", "footer_text"),
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"report_{record.run_id}.pdf"
    switches = sections(config)
    stylesheets = [
        CSS(filename=str(stylesheet_path(config))),
        CSS(string=runtime_css(config, footer, header_lines(record))),
    ]

    # The sized-down copies live only as long as it takes to embed them. They
    # are a rendering detail and not an output: 4.1 puts one PDF and one
    # manifest in output/ and nothing else.
    with tempfile.TemporaryDirectory(prefix="facilityops-images-") as image_dir:
        rows = contents_rows(switches)
        for attempt in range(3):
            html = render_html(
                record, gaps, derived, grids, config, Path(image_dir), rows,
            )
            document = HTML(string=html, base_url=str(TEMPLATE_DIR)).render(
                stylesheets=stylesheets,
            )
            mapped = page_map(document.pages, switches)
            if mapped.contents == rows:
                break
            rows = mapped.contents
        else:
            logger.warning(
                "contents page numbers did not settle after %d passes; "
                "the manifest describes the document written", attempt + 1,
            )
        document.write_pdf(path)

    logger.info(
        "rendered %d checkpoints to %s, %d pages", len(record.checkpoints), path,
        mapped.count,
    )
    return path, mapped
