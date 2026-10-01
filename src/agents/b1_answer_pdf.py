"""B-1's answer to one question, written as a PDF.

One document per question: the question, the answer with every value marked
and numbered, where each value comes from, the runs consulted, the
verification, and how the answer was found. Set in the report's own
stylesheet (src/report/templates, chosen by branding.stylesheet in
report.yaml), with page size, brand colour and a provenance footer from the
same config, as the report sets them.

When the answer lists evidence images (the evidence lookup), they are drawn
under it, found and paired the way the report does it (common.paths,
report.images): grouped by run and checkpoint, RGB and thermal of one view in
one cell.

Nothing here decides anything: it lays out what b1_analytical.answer_question
and the envelope already hold.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from weasyprint import CSS, HTML

from agents.b1_analytical import ABSTAIN, run_of
from agents.schema import B1Result, DerivationConfig, ExtendedRecord
from agents.utils import format_timestamp
from common import PROJECT_ROOT
from common.paths import ResolvedImage, resolve_evidence, setting
from report.images import GRID_COLUMNS, build_view_grid, prepare_image

TEMPLATES = Path(__file__).parent / "templates"
REPORT_TEMPLATES = PROJECT_ROOT / "src" / "report" / "templates"

# Status -> the report's badge colour class.
STATUS_STYLE = {
    "OK": "pass",
    "DEGRADED_TEMPLATE_ONLY": "warn",
    "REFUSED_UNVERIFIABLE": "fail",
    "PROVIDER_UNAVAILABLE": "fail",
}
VERDICT_STYLE = {"PASS": "pass", "WARN": "warn", "FAIL": "fail"}

# What the answer page adds to the report's stylesheet.
ANSWER_CSS = """
.b1-answer-text { white-space: pre-line; font-size: 11pt; line-height: 1.4; margin: 0 0 8pt; }
.b1-cited { font-weight: bold; }
.b1-ref { font-size: 6.5pt; color: var(--heading-grey); margin-left: 0.5pt; }
.b1-citations td:first-child { width: 6mm; }
.b1-sql {
  font-family: var(--mono); font-size: 7.5pt; white-space: pre-wrap;
  border-left: 1.5pt solid var(--primary-colour); padding: 2pt 0 2pt 4mm; margin: 6pt 0;
}
"""


# Where a cited evidence image sits in the records: a checkpoint's image, a
# checkpoint's finding (detection) image, or a run-level finding's image.
IMAGE_PATH = re.compile(
    r"records\[(\d+)\]\.(?:checkpoints\[(\d+)\]\.(?:evidence_images|annotated_images)\[\d+\]"
    r"|checkpoints\[(\d+)\]\.detections\[\d+\]\.evidence_image"
    r"|findings\[(\d+)\]\.evidence_image)$"
)


def write_answer_pdf(
    question: str, result: B1Result, envelope: dict, extended: ExtendedRecord, config: DerivationConfig,
    report_config: dict, out_dir: Path, evidence_root: Path | None = None,
) -> Path:
    """Render the answer to out_dir and return the PDF's path.

    config gives the timestamp format; report_config the stylesheet, page size
    and brand colour. evidence_root is where evidence image paths resolve;
    without it no image is drawn. The file is named by when the answer was
    generated and the question's first words, so each question gets its own
    document.
    """
    output = result.output or {}
    answer = output.get("answer")
    citations = numbered_citations(answer, output.get("citations", []), extended)
    trace = result.trace or {}
    route = (trace.get("router") or {}).get("route") or {}
    by_id = {record.run_id: record for record in extended.records}
    runs = [by_id[run_id] for run_id in output.get("records_consulted", []) if run_id in by_id]

    context = {
        "question": question,
        "facility": runs[0].facility_name if runs else (extended.records[0].facility_name if extended.records else None),
        "generated_at": envelope["generated_at"],
        "status": "ABSTAINED" if answer == ABSTAIN else result.status,
        "status_style": "info" if answer == ABSTAIN else STATUS_STYLE.get(result.status, "other"),
        "answer": segments(answer, citations) if answer else None,
        "degraded": result.status == "DEGRADED_TEMPLATE_ONLY",
        "reason": result.reason or "",
        "citations": citations,
        "runs": [
            {
                "run_id": record.run_id,
                "start_time": format_timestamp(record.start_time, config) or "not recorded",
                "final_status": record.final_status,
                "status_style": VERDICT_STYLE.get(record.final_status, "other"),
            }
            for record in runs
        ],
        "verification": result.verification,
        "route": route.get("derivations", []),
        "answer_from": trace.get("answer_from"),
        "attempts": result.attempts,
        "model": envelope["model"],
        "prompt_version": envelope["prompt_version"],
        "sqls": [step["sql"] for step in trace.get("sql_writer", []) if step.get("sql")],
    }

    env = Environment(
        loader=FileSystemLoader(TEMPLATES), autoescape=True, undefined=StrictUndefined,
        trim_blocks=True, lstrip_blocks=True,
    )

    footer = (f"{envelope['agent']} {envelope['agent_version']} · {envelope['model'] or 'no model'} · "
              f"{envelope['prompt_version']} · {envelope['generated_at']}")
    runtime = (
        f"@page {{ size: {setting(report_config, 'report', 'page_size')}; }}\n"
        f"@page {{ @bottom-left {{ content: {css_string(footer)}; }} }}\n"
        f":root {{ --primary-colour: {setting(report_config, 'branding', 'primary_colour')}; }}\n"
    )
    stylesheet = REPORT_TEMPLATES / setting(report_config, "branding", "stylesheet")

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"\D", "", envelope["generated_at"])
    slug = re.sub(r"[^a-z0-9]+", "_", question.lower()).strip("_")[:40] or "question"
    path = out_dir / f"b1_answer_{stamp}_{slug}.pdf"
    # Images are sized down into a folder that must outlive the render.
    with tempfile.TemporaryDirectory(prefix="b1-images-") as image_dir:
        context["evidence"] = evidence_groups(result, extended, evidence_root, Path(image_dir))
        html = env.get_template("b1_answer.j2").render(**context)
        HTML(string=html, base_url=str(REPORT_TEMPLATES)).write_pdf(
            path, stylesheets=[CSS(filename=str(stylesheet)), CSS(string=runtime + ANSWER_CSS)],
        )
    return path


def evidence_groups(
    result: B1Result, extended: ExtendedRecord, evidence_root: Path | None, image_dir: Path,
) -> list[dict]:
    """The evidence images the answer lists, ready to draw: one group per run
    and checkpoint, in the order the answer lists them, each with the report's
    view grid (RGB and thermal of one view paired). [] when the answer is not
    shown, is an abstention, lists no image, or there is no evidence_root.
    """
    answer = (result.output or {}).get("answer")
    if evidence_root is None or not answer or answer == ABSTAIN:
        return []
    groups: dict[tuple, dict] = {}
    for step in (result.trace or {}).get("sql_writer", []):
        for slot_id, source in step.get("slots", {}).items():
            match = IMAGE_PATH.match(source)
            if not match:
                continue
            i = int(match.group(1))
            record = extended.records[i]
            j = match.group(2) or match.group(3)
            if j is not None:
                checkpoint = record.checkpoints[int(j)]
                cid, name = checkpoint.checkpoint_id, checkpoint.checkpoint_name
            else:   # a run-level finding: its own checkpoint_id, if it has one
                cid = record.findings[int(match.group(4))].checkpoint_id
                name = cid
            group = groups.setdefault((record.run_id, cid), {
                "run_id": record.run_id, "checkpoint_id": cid, "checkpoint_name": name, "uris": [],
            })
            uri = step["values"][slot_id]
            if uri not in group["uris"]:
                group["uris"].append(uri)

    drawn = []
    for group in groups.values():
        images = [resolve_evidence(uri, evidence_root, checkpoint_id=group["checkpoint_id"]) for uri in group["uris"]]
        grid = build_view_grid(images)
        cells = [
            {"view": cell.view, "rgb": image_cell(cell.rgb, image_dir), "thermal": image_cell(cell.thermal, image_dir)}
            for cell in grid
        ]
        drawn.append({
            "run_id": group["run_id"], "checkpoint_id": group["checkpoint_id"],
            "checkpoint_name": group["checkpoint_name"], "cells": cells,
            "columns": max(1, min(GRID_COLUMNS, len(cells))),
            "thermal": any(cell["thermal"] for cell in cells),
        })
    return drawn


def image_cell(image: ResolvedImage | None, image_dir: Path) -> dict | None:
    """One image to draw: {src} for a file, {absent} with why for one that
    will not show, or None when the answer did not list this half of the view."""
    if image is None:
        return None
    local = prepare_image(image, image_dir)
    if local is None:
        return {"src": None, "absent": f"Image not available - {image.reason}"}
    return {"src": local.resolve().as_uri(), "absent": None}


def numbered_citations(answer: str | None, citations: list[dict], extended: ExtendedRecord) -> list[dict]:
    """The citations in the order their values appear, numbered from 1, each
    with its text in the answer and the run its source is about."""
    if not answer:
        return []
    ordered = sorted(citations, key=lambda c: c["claim_span"][0])
    return [
        {
            "n": n,
            "span": c["claim_span"],
            "text": answer[c["claim_span"][0]:c["claim_span"][1]],
            "run": run_of(c["source_field"], extended) or "all runs",
            "source_field": c["source_field"],
        }
        for n, c in enumerate(ordered, start=1)
    ]


def segments(answer: str, citations: list[dict]) -> list[dict]:
    """The answer cut into plain text and cited values, in order: {text, n},
    n being the value's citation number, or None for text between values."""
    parts, pos = [], 0
    for c in citations:
        start, end = c["span"]
        if start > pos:
            parts.append({"text": answer[pos:start], "n": None})
        parts.append({"text": answer[start:end], "n": c["n"]})
        pos = end
    if pos < len(answer):
        parts.append({"text": answer[pos:], "n": None})
    return parts


def css_string(text: str) -> str:
    """One string, quoted for a CSS content property."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
