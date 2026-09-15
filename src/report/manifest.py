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
from pathlib import Path

from common.paths import CONFIG_PATH, ResolvedImage, load_config, setting
from common.provenance import Provenance, stamp
from common.schema import Record
from report.derive import alert_counts, computed_counts, declared_counts
from report.gaps import RUN_LEVEL, Gap

# Every version in the manifest is a config value. `manifest.version` is the
# shape of this file; the engine and template versions, the generation time and
# the run id come from one common.provenance.stamp() call, which 5.6 names as
# their home, so the manifest and the page footer cannot disagree about any of
# the four.

#: Fields that cannot be real until render.py exists.
PLACEHOLDER_FIELDS = ("page_count", "sections")


def hash_config(config: dict) -> str:
    """SHA-256 of a config as actually applied, prefixed sha256: (4.4).

    Hashes the parsed values rather than the file bytes, so reordering keys or
    editing a comment does not change the hash, and changing a value does.
    """
    applied = json.dumps(config, sort_keys=True, default=str)
    return f"sha256:{hashlib.sha256(applied.encode('utf-8')).hexdigest()}"


def config_hash(config_path: Path = CONFIG_PATH) -> str:
    """The hash of one config file. Reads it, then hands it to hash_config.

    Kept separate so a caller that has already read the config does not read it
    a second time only to hash it.
    """
    return hash_config(load_config(config_path))


def build_manifest(
    record: Record,
    gaps: list[Gap],
    images: dict[str, list[ResolvedImage]],
    pdf_path: Path,
    config_path: Path = CONFIG_PATH,
    provenance: Provenance | None = None,
) -> dict:
    """Build the manifest for one run.

    `provenance` is the stamp the report was rendered with. Passing it is how
    the manifest and the page footer come to carry the same generation time;
    without one the manifest stamps itself, which keeps the call shape simple
    for a caller that only wants the manifest.

    `page_count` and `sections` are placeholders until the PDF is rendered.
    Everything else is real.
    """
    found = [image for images_of in images.values() for image in images_of]

    # Read once. The version stamp and the hash must describe the same config,
    # and 4.4 defines the hash as the config as actually applied.
    config = load_config(config_path)
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
