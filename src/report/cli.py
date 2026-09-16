"""Command line entry point.

The full pipeline is seven stages, in this order:

    load -> validate -> resolve_images -> detect_gaps -> derive -> render -> manifest

All seven run. The report itself is built a section at a time: render produces
whatever templates exist, and each new one is an include in full_report.html.j2
rather than a change here.

Exit status:
    0  finished
    1  the run failed for a reported reason - an unreadable record, or a config
       that is missing a setting the engine needs
    2  the command line was wrong (argparse)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from common import DATA_DIR, OUTPUT_DIR, PROJECT_ROOT, configure_logging, ensure_runtime_dirs
from common.loader import RecordParseError, load_record
from common.paths import (
    CONFIG_PATH, ConfigError, ResolvedImage, load_config, resolve_record_images,
)
from common.provenance import stamp
from common.schema import Record
from report.derive import DerivedValues, derive_report_values
from report.gaps import RUN_LEVEL, Gap, detect_gaps
from report.images import ViewCell, build_view_grid
from report.manifest import build_manifest, write_manifest
from report.render import render_document

# Named explicitly: run as "python -m report.cli", __name__ would be "__main__".
logger = logging.getLogger("report.cli")

@dataclass(frozen=True)
class Artifacts:
    """What the pipeline produced. Later stages add their outputs here."""

    record: Record
    images: dict[str, list[ResolvedImage]]
    grids: dict[str, list[ViewCell]]
    gaps: list[Gap]
    derived: DerivedValues
    pdf_path: Path
    manifest: dict


def run_pipeline(
    record_path: Path,
    evidence_root: Path,
    output_dir: Path,
    config_path: Path = CONFIG_PATH,
) -> Artifacts:
    """Run the pipeline stages in order and return what they produced.

    Raises RecordParseError if the record cannot be read at all.
    """
    # load
    record = load_record(record_path)

    # validate
    report_anomalies(record)

    # resolve_images
    images = resolve_record_images(record, evidence_root)
    grids = {
        checkpoint_id: build_view_grid(found)
        for checkpoint_id, found in images.items()
    }

    # detect_gaps
    gaps = detect_gaps(record, images, grids)

    # derive
    derived = derive_report_values(record)

    # One stamp for the whole run, so the page footer and the manifest agree on
    # when the report was generated and which engine made it (5.6).
    config = load_config(config_path)
    provenance = stamp(record.run_id, config)

    # render. render_document is render_pdf plus the page numbers it measured
    # while laying the document out; the manifest needs them and nothing else
    # can know them (4.4).
    pdf_path, pages = render_document(
        record, gaps, derived, grids, config, output_dir, provenance,
    )

    # manifest. The same config path the render used, so the version stamp and
    # the config hash describe the config that was actually applied (4.4).
    manifest = build_manifest(
        record, gaps, images, pdf_path, config_path, provenance, pages, config,
    )

    return Artifacts(
        record=record, images=images, grids=grids, gaps=gaps,
        derived=derived, pdf_path=pdf_path, manifest=manifest,
    )


def report_anomalies(record: Record) -> None:
    """Log whatever the record disagrees with the data contract about.

    Nothing is rejected. A degraded record still produces a report that says
    what is wrong with it.
    """
    if not record.anomalies:
        logger.info("no anomalies")
        return

    logger.warning("%d field anomalies", len(record.anomalies))
    for anomaly in record.anomalies:
        logger.warning("  %s.%s: %s", anomaly.item_id, anomaly.field_name, anomaly.problem)


def write_json(data: object, destination: Path) -> None:
    """Write JSON to a file, or to stdout for '-'.

    Datetimes and paths have no JSON form, so they are written as text.
    """
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if str(destination) == "-":
        print(text)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    logger.info("wrote %s", destination)


def dump_record(record: Record, destination: Path) -> None:
    """Write the parsed record as JSON: what the loader made of the input file."""
    write_json(asdict(record), destination)


def dump_artifacts(artifacts: Artifacts, destination: Path) -> None:
    """Write the resolved images and the grids as JSON.

    Not the record: --dump-record writes that, and including it here buries the
    images under a few hundred sensor samples.

    The grids repeat the images they hold, so each cell can be read on its own.
    """
    write_json(
        {
            "images": {
                checkpoint_id: [asdict(image) for image in found]
                for checkpoint_id, found in artifacts.images.items()
            },
            "grids": {
                checkpoint_id: [asdict(cell) for cell in grid]
                for checkpoint_id, grid in artifacts.grids.items()
            },
        },
        destination,
    )


def log_summary(artifacts: Artifacts) -> None:
    """Log what each stage produced. Per-checkpoint detail at debug level."""
    record = artifacts.record
    logger.info(
        "record %s: %d checkpoints, %d findings, %d alerts, %d samples, %d events",
        record.run_id, len(record.checkpoints), len(record.findings),
        len(record.sensor_alerts), len(record.sensor_samples), len(record.event_log),
    )

    found = [image for images in artifacts.images.values() for image in images]
    unusable = [image for image in found if not image.readable]
    logger.info(
        "images: %d referenced, %d usable, %d unusable, %d thermal",
        len(found), len(found) - len(unusable), len(unusable),
        sum(1 for image in found if image.is_thermal),
    )
    for image in unusable:
        logger.warning("  %s: %s", image.original_uri, image.reason)

    cells = [cell for grid in artifacts.grids.values() for cell in grid]
    logger.info(
        "grids: %d checkpoints, %d cells, %d with an image, %d thermal pairs, %d empty",
        len(artifacts.grids), len(cells),
        sum(1 for cell in cells if cell.rgb is not None),
        sum(1 for cell in cells if cell.thermal is not None),
        sum(1 for cell in cells if cell.rgb is None and cell.thermal is None),
    )

    by_type: dict[str, int] = {}
    for gap in artifacts.gaps:
        by_type[gap.gap_type.name] = by_type.get(gap.gap_type.name, 0) + 1
    logger.info(
        "gaps: %d across %d checkpoints, %d run-level",
        len(artifacts.gaps),
        len({g.item_id for g in artifacts.gaps if g.item_id != RUN_LEVEL}),
        sum(1 for g in artifacts.gaps if g.item_id == RUN_LEVEL),
    )
    for name, count in sorted(by_type.items(), key=lambda pair: -pair[1]):
        logger.info("  %-20s %d", name, count)

    for checkpoint_id, grid in artifacts.grids.items():
        no_rgb = [cell.view or "unlabelled" for cell in grid if cell.rgb is None]
        no_thermal = [
            cell.view or "unlabelled" for cell in grid
            if cell.rgb is not None and cell.thermal is None
        ]
        logger.debug(
            "  %s: %d views recorded%s%s",
            checkpoint_id, len(grid),
            f", no RGB for {', '.join(no_rgb)}" if no_rgb else "",
            f", no thermal for {', '.join(no_thermal)}" if no_thermal else "",
        )


def build_parser() -> argparse.ArgumentParser:
    """Define the command line."""
    config_default = PROJECT_ROOT / "config" / "report.yaml"
    parser = argparse.ArgumentParser(
        prog="facilityops-report",
        description="Render one inspection run record to a PDF report and a manifest.",
    )
    parser.add_argument("--record", type=Path, required=True,
                        help="run record JSON to render")
    parser.add_argument("--evidence-root", type=Path, default=DATA_DIR,
                        help=f"root that recorded evidence paths are rewritten against "
                             f"(default: {DATA_DIR})")
    parser.add_argument("--config", type=Path, default=config_default,
                        help=f"report configuration (default: {config_default})")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR,
                        help=f"where the PDF and manifest are written (default: {OUTPUT_DIR})")
    parser.add_argument("--dump-record", type=Path, metavar="PATH", nargs="?",
                        default=None, const=Path("-"),
                        help="write the parsed record as JSON to PATH; "
                             "with no PATH, or with -, write it to stdout")
    parser.add_argument("--dump-manifest", type=Path, metavar="PATH", nargs="?",
                        default=None, const=Path("-"),
                        help="write the manifest as JSON to PATH; "
                             "with no PATH, or with -, write it to stdout")
    parser.add_argument("--dump-artifacts", type=Path, metavar="PATH", nargs="?",
                        default=None, const=Path("-"),
                        help="write everything the pipeline produced as JSON to PATH; "
                             "with no PATH, or with -, write it to stdout")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="log at debug level")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the pipeline. Returns the process exit status."""
    args = build_parser().parse_args(argv)
    configure_logging(logging.DEBUG if args.verbose else logging.INFO)
    ensure_runtime_dirs()

    try:
        artifacts = run_pipeline(
            args.record, args.evidence_root, args.output_dir, args.config
        )
    except (RecordParseError, ConfigError, OSError) as error:
        logger.error("%s", error)
        return 1

    log_summary(artifacts)

    written = write_manifest(artifacts.manifest, args.output_dir)
    logger.info("wrote %s", written)

    if args.dump_record is not None:
        dump_record(artifacts.record, args.dump_record)
    if args.dump_artifacts is not None:
        dump_artifacts(artifacts, args.dump_artifacts)
    if args.dump_manifest is not None:
        write_json(artifacts.manifest, args.dump_manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
