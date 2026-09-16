"""Build the manifest JSON that sits beside the PDF.

Downstream code reads the manifest, not the PDF, so its shape is fixed and this
module matches it field for field.

Two fields describe the rendered document rather than the record: `page_count`
and `sections`. They are measured by render_document, which lays the PDF out,
reads back the page each section landed on and hands the result here, so 4.4's
rule that the section ranges match the real PDF holds by construction.

A caller that only wants a manifest can still omit them, and then they say so:
PLACEHOLDER_FIELDS names the two, and they come back 0 and empty rather than
guessed at.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from common.paths import CONFIG_PATH, ResolvedImage, load_config, setting
from common.provenance import Provenance, stamp
from common.schema import Record
from report.derive import alert_counts, computed_counts, declared_counts
from report.gaps import RUN_LEVEL, Gap
from report.render import PageMap

# Every version in the manifest is a config value. `manifest.version` is the
# shape of this file; the engine and template versions, the generation time and
# the run id come from one common.provenance.stamp() call, which 5.6 names as
# their home, so the manifest and the page footer cannot disagree about any of
# the four.

#: Fields that are only real when the rendered document is passed in.
PLACEHOLDER_FIELDS = ("page_count", "sections")


def hash_config(config: dict) -> str:
    """SHA-256 of a config as actually applied, prefixed sha256: (4.4).

    Hashes the parsed values rather than the file bytes, so reordering keys or
    editing a comment does not change the hash, and changing a value does.
    """
    applied = json.dumps(config, sort_keys=True, default=str)
    return f"sha256:{hashlib.sha256(applied.encode('utf-8')).hexdigest()}"


def build_manifest(
    record: Record,
    gaps: list[Gap],
    images: dict[str, list[ResolvedImage]],
    pdf_path: Path,
    config_path: Path = CONFIG_PATH,
    provenance: Provenance | None = None,
    pages: PageMap | None = None,
    config: dict | None = None,
) -> dict:
    """Build the manifest for one run.

    `provenance` is the stamp the report was rendered with. Passing it is how
    the manifest and the page footer come to carry the same generation time;
    without one the manifest stamps itself, which keeps the call shape simple
    for a caller that only wants the manifest.

    `pages` is what render_document measured off the PDF it wrote: the page
    count, and the first and last page of every section the config turned on.
    Another trailing argument with a default, so 5.3's five-argument call still
    works - without it those two fields are 0 and empty rather than invented.

    `config` is the config the caller has already read, if it has one. The
    pipeline has: it loads the file once and renders with it, and passing it
    here is what stops the same file being parsed a second time to hash it.
    A caller that only wants a manifest still passes a path and nothing else.
    """
    found = [image for images_of in images.values() for image in images_of]

    # Read once. The version stamp and the hash must describe the same config,
    # and 4.4 defines the hash as the config as actually applied.
    config = load_config(config_path) if config is None else config
    provenance = provenance or stamp(record.run_id, config)

    return {
        "manifest_version": setting(config, "manifest", "version"),
        "generated_at": provenance.generated_at,
        "source_run_id": provenance.source_run_id,
        "facility_id": record.facility_id,
        "engine_version": provenance.engine_version,
        "template_version": provenance.template_version,
        "config_hash": hash_config(config),
        "report_file": pdf_path.name,

        # Measured off the document that was written, never estimated (4.4).
        "page_count": pages.count if pages else 0,
        "sections": [dict(section) for section in pages.sections] if pages else [],

        "checkpoints_rendered": len(record.checkpoints),
        # Distinct checkpoints carrying at least one gap, not a count of gaps.
        "checkpoints_with_gaps": len(
            {gap.item_id for gap in gaps if gap.item_id != RUN_LEVEL}
        ),
        "images_referenced": len(found),
        "images_rendered": sum(1 for image in found if image.readable),
        "gaps": [asdict(gap) for gap in gaps],
        "counts": {
            "declared": asdict(declared_counts(record)),
            "computed": asdict(computed_counts(record)),
        },
        "alerts": alert_counts(record),
    }


def write_manifest(manifest: dict, output_dir: Path) -> Path:
    """Write the manifest beside the PDF, named after the run.

    The run id is used exactly as recorded; it already carries a timestamp.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"report_{manifest['source_run_id']}.manifest.json"
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path
