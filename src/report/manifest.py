"""Build the manifest JSON that sits beside the PDF.

Downstream code reads the manifest, not the PDF, so its shape is fixed and this
module matches it field for field.

Two fields cannot be filled until the PDF exists: `page_count` and `sections`
both describe the rendered document. They carry placeholders for now, and
PLACEHOLDER_FIELDS names them so nothing ships believing they are real.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from common.paths import CONFIG_PATH, ResolvedImage, load_config
from common.schema import Record
from report.derive import alert_counts, computed_counts, declared_counts
from report.gaps import RUN_LEVEL, Gap

__all__ = [
    "MANIFEST_VERSION", "ENGINE_VERSION", "TEMPLATE_VERSION", "PLACEHOLDER_FIELDS",
    "config_hash", "build_manifest", "write_manifest",
]

MANIFEST_VERSION = "1.0"
TEMPLATE_VERSION = "full_report_v1"

#: Moves to common/provenance.py, which is the named home for version stamping.
ENGINE_VERSION = "0.1.0"

#: Fields that cannot be real until render.py exists.
PLACEHOLDER_FIELDS = ("page_count", "sections")


def config_hash(config_path: Path = CONFIG_PATH) -> str:
    """SHA-256 of the config as actually applied, prefixed sha256:.

    Hashes the parsed values rather than the file bytes, so reordering keys or
    editing a comment does not change the hash, and changing a value does.
    """
    applied = json.dumps(load_config(config_path), sort_keys=True, default=str)
    return f"sha256:{hashlib.sha256(applied.encode('utf-8')).hexdigest()}"


def build_manifest(
    record: Record,
    gaps: list[Gap],
    images: dict[str, list[ResolvedImage]],
    pdf_path: Path,
    config_path: Path = CONFIG_PATH,
) -> dict:
    """Build the manifest for one run.

    `page_count` and `sections` are placeholders until the PDF is rendered.
    Everything else is real.
    """
    found = [image for images_of in images.values() for image in images_of]

    return {
        "manifest_version": MANIFEST_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_run_id": record.run_id,
        "facility_id": record.facility_id,
        "engine_version": ENGINE_VERSION,
        "template_version": TEMPLATE_VERSION,
        "config_hash": config_hash(config_path),
        "report_file": pdf_path.name,

        # Placeholders. Both describe the rendered PDF, which does not exist yet.
        "page_count": 0,
        "sections": [],

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
