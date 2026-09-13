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

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from weasyprint import CSS, HTML

from common import OUTPUT_DIR, PROJECT_ROOT
from common.paths import setting
from common.provenance import Provenance, stamp
from common.schema import Record
from report.derive import DerivedValues
from report.gaps import RUN_LEVEL, Gap, GapType, disagreeing_counts
from report.images import DirectionCell

logger = logging.getLogger("report.render")

__all__ = [
    "TEMPLATE_DIR", "STYLES_PATH", "SECTION_NAMES",
    "environment", "show", "moment", "sections",
    "CountRow", "count_rows", "CoverageItem", "coverage_items",
    "run_count_conflicts",
    "VERDICT_STYLES", "verdict_style",
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
