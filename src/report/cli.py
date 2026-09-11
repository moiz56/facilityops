"""Command line entry point.

The full pipeline is seven stages, in this order:

    load -> validate -> resolve_images -> detect_gaps -> derive -> render -> manifest

Built so far: load and validate. Each new stage is one more call in main(), in
that order, so the pipeline reads top to bottom.

Exit status:
    0  finished
    1  the run failed for a reported reason, such as an unreadable record
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
from common.paths import ResolvedImage, resolve_record_images
from common.schema import Record
from report.images import DirectionCell, build_direction_grid

# Named explicitly: run as "python -m report.cli", __name__ would be "__main__".
logger = logging.getLogger("report.cli")

__all__ = ["Artifacts", "run_pipeline", "log_summary", "main"]


@dataclass(frozen=True)
class Artifacts:
    """What the pipeline produced. Later stages add their outputs here."""

    record: Record
    images: dict[str, list[ResolvedImage]]
    grids: dict[str, list[DirectionCell]]


def run_pipeline(record_path: Path, evidence_root: Path) -> Artifacts:
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
        checkpoint_id: build_direction_grid(found)
        for checkpoint_id, found in images.items()
    }

    # detect_gaps, derive, render, manifest go here as their modules are written.

    return Artifacts(record=record, images=images, grids=grids)


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


def dump_record(record: Record, destination: Path) -> None:
    """Write the parsed record as JSON, to a file or to stdout for '-'.

    A view of what the loader made of the file, not an output of the engine.
    Datetimes and paths are written as text.
    """
    text = json.dumps(asdict(record), indent=2, ensure_ascii=False, default=str)
    if str(destination) == "-":
        print(text)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    logger.info("wrote the parsed record to %s", destination)


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

    for checkpoint_id, grid in artifacts.grids.items():
        missing = [cell.direction for cell in grid if cell.rgb is None]
        no_thermal = [cell.direction for cell in grid if cell.rgb is not None and cell.thermal is None]
        logger.debug(
            "  %s: %d of 8 directions%s%s",
            checkpoint_id, 8 - len(missing),
            f", no image for {', '.join(missing)}" if missing else "",
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
    parser.add_argument("--dump-record", type=Path, metavar="PATH", default=None,
                        help="write the parsed record as JSON to PATH, or to stdout for -")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="log at debug level")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the pipeline. Returns the process exit status."""
    args = build_parser().parse_args(argv)
    configure_logging(logging.DEBUG if args.verbose else logging.INFO)
    ensure_runtime_dirs()

    try:
        artifacts = run_pipeline(args.record, args.evidence_root)
    except (RecordParseError, OSError) as error:
        logger.error("%s", error)
        return 1

    log_summary(artifacts)

    if args.dump_record is not None:
        dump_record(artifacts.record, args.dump_record)
    return 0


if __name__ == "__main__":
    sys.exit(main())
