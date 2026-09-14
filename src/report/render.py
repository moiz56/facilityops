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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from weasyprint import CSS, HTML

from common import OUTPUT_DIR, PROJECT_ROOT
from common.paths import setting
from common.provenance import Provenance, stamp
from common.schema import Finding, Point2D, Pose, Record, SensorAlert
from report.derive import DerivedValues, ZoneStat
from report.gaps import RUN_LEVEL, Gap, GapType, disagreeing_counts, offline_block_gaps
from report.images import DirectionCell

logger = logging.getLogger("report.render")

__all__ = [
    "TEMPLATE_DIR", "STYLES_PATH", "SECTION_NAMES",
    "environment", "show", "moment", "sections",
    "CountRow", "count_rows", "CoverageItem", "coverage_items",
    "run_count_conflicts",
    "VERDICT_STYLES", "verdict_style",
    "SEVERITY_STYLES", "severity_style", "place",
    "CONFIRMED_STATUSES", "REVIEW_STATUS", "FindingGroup", "finding_groups",
    "ALERT_SEVERITIES", "AlertGroup", "alert_groups",
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
) -> str:
    """Render the whole report to HTML.

    Separate from render_pdf so the markup can be read and tested without
    producing a document.
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
    html = render_html(record, gaps, derived, grids, config)
    footer = provenance_line(
        provenance or stamp(record.run_id, config),
        setting(config, "branding", "footer_text"),
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"report_{record.run_id}.pdf"

    HTML(string=html, base_url=str(TEMPLATE_DIR)).write_pdf(
        path,
        stylesheets=[
            CSS(filename=str(STYLES_PATH)),
            CSS(string=runtime_css(config, footer)),
        ],
    )
    logger.info("rendered %d checkpoints to %s", len(record.checkpoints), path)
    return path
