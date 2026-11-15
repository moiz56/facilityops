"""Shared by the Milestone 4 tests."""

import re
import shutil
from pathlib import Path

from common import PROJECT_ROOT
from common.paths import DEFAULT_CONFIG, PATH_PREFIX, resolve_record_images, setting
from common.schema import Record
from report.derive import derive_report_values
from report.gaps import detect_gaps
from report.images import build_view_grid
from report.render import render_html
from seal.cli import seal_route

DATA = PROJECT_ROOT / "data_tenant"

#: Where real routes are sealed for the tests, so a failing case can be read.
SEALED = Path(__file__).resolve().parent / "output" / "sealed"

_sealed_this_session: set[str] = set()


def section_html(record: Record, evidence_root: Path, config: dict, section: str, **sections) -> str:
    """One section of the report for this run, as HTML. No PDF, no agents.

    sections are render_html's own arguments for the M4 sections, such as
    comparison, previous or standards. section is the anchor on the
    section's <section> tag.
    """
    images = resolve_record_images(record, evidence_root)
    grids = {cid: build_view_grid(found) for cid, found in images.items()}
    html = render_html(
        record, detect_gaps(record, images, grids), derive_report_values(record), grids, config,
        **sections,
    )
    found = re.search(rf'<section class="section [^"]*" id="{section}">.*?</section>', html, re.S)
    assert found, f"the report has no {section} section"
    return found.group(0)


def sealed_route(route: str) -> Path:
    """data_tenant/<route> sealed into tests/output/sealed/<route>. Returns that folder.

    Sealed afresh the first time a session asks for the route, then reused:
    sealing renders every run's report, so it is slow. A test that changes a
    sealed route copies it first.
    """
    out = SEALED / route
    if route not in _sealed_this_session:
        if out.exists():
            shutil.rmtree(out)
        sealed_by = f"facilityops-report {setting(DEFAULT_CONFIG, 'provenance', 'engine_version')}"
        counts = seal_route(DATA / route, out, PATH_PREFIX, sealed_by)
        assert counts["failed"] == 0, counts
        _sealed_this_session.add(route)
    return out
