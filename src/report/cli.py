"""Entry point: drives the pipeline of section 5.2.

The full pipeline is seven stages:

    load -> validate -> resolve_images -> detect_gaps -> derive -> render -> manifest

Each stage is a pure function of the previous stage's output, so a failure
localises to one stage rather than to the pipeline as a whole. `STAGES` holds
the ones that are built; a stage is added to it, with its slot on `State`, when
its module is written.

Built so far: load, validate.

Exit status:
    0  the requested stages completed
    1  the run failed for a reported reason, such as an unreadable record
    2  the command line was wrong (argparse)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable

from common import DATA_DIR, OUTPUT_DIR, PROJECT_ROOT, configure_logging, ensure_runtime_dirs
from common.loader import RecordParseError, load_record
from common.schema import Record

# Named explicitly: run as "python -m report.cli", __name__ is "__main__".
logger = logging.getLogger("report.cli")

__all__ = ["main", "STAGES"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Options:
    """Resolved command line arguments."""

    record: Path
    evidence_root: Path
    config: Path
    output_dir: Path
    stop_after: str | None
    dump_record: Path | None


@dataclass(frozen=True, slots=True, kw_only=True)
class State:
    """What the pipeline has produced so far.

    Every stage returns a new State rather than mutating the one it was given,
    so the output of any stage can be inspected without the next stage having
    touched it. Later stages add their own fields here as they are written.
    """

    record: Record | None = None


def stage_load(state: State, options: Options) -> State:
    """Parse the record into a Record. Raises RecordParseError if it cannot be read."""
    return replace(state, record=load_record(options.record))


def stage_validate(state: State, options: Options) -> State:
    """Report what the record disagrees with section 2 about.

    The anomalies were collected while parsing; this stage is where they are
    surfaced, so a degraded record is visible on the console as well as in the
    report. Nothing is rejected here - section 11 requires a bad record to
    produce a report rather than a failure.
    """
    record = _loaded(state)
    if not record.anomalies:
        logger.info("validate: record matches section 2, no anomalies")
        return state
    logger.warning("validate: %d field anomalies", len(record.anomalies))
    for anomaly in record.anomalies:
        logger.warning("  %s.%s: %s", anomaly.item_id, anomaly.field_name, anomaly.problem)
    return state


#: Stage name -> the function that runs it, in the order section 5.2 fixes.
STAGES: dict[str, Callable[[State, Options], State]] = {
    "load": stage_load,
    "validate": stage_validate,
}


def _loaded(state: State) -> Record:
    """Return the loaded record, or fail loudly if a stage ran out of order."""
    if state.record is None:
        raise RuntimeError("stage ran before load produced a record")
    return state.record


def run_pipeline(options: Options) -> State:
    """Run the built stages in order, stopping early if --stop-after asks."""
    state = State()
    for name, stage in STAGES.items():
        state = stage(state, options)
        logger.debug("stage %s complete", name)
        if options.stop_after == name:
            logger.info("stopping after %s, as requested", name)
            break
    return state


def record_as_json(record: Record) -> str:
    """Render a parsed record as JSON, for inspection and as an evidence artifact.

    A view of the loader's output, not an output of the engine: the deliverables
    are the PDF and the manifest (4.1). Values the contract does not carry as
    JSON - datetimes, the source path - are written as their text form, so the
    dump reads as what the loader made of the file rather than as the file.
    """
    return json.dumps(asdict(record), indent=2, ensure_ascii=False, default=str)


def _dump_record(record: Record, destination: Path) -> None:
    """Write the parsed record to a file, or to stdout when destination is "-"."""
    text = record_as_json(record)
    if str(destination) == "-":
        print(text)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    logger.info("wrote the parsed record to %s", destination)


def _summarise(state: State) -> None:
    """Log what the pipeline produced."""
    if state.record is None:
        return
    record = state.record
    logger.info(
        "record %s: %d checkpoints, %d findings, %d alerts, %d samples, %d anomalies",
        record.run_id, len(record.checkpoints), len(record.findings),
        len(record.sensor_alerts), len(record.sensor_samples), len(record.anomalies),
    )


def build_parser() -> argparse.ArgumentParser:
    """Define the command line."""
    parser = argparse.ArgumentParser(
        prog="facilityops-report",
        description="Render one inspection run record to a PDF report and a manifest.",
    )
    parser.add_argument("--record", type=Path, required=True,
                        help="run record JSON to render")
    parser.add_argument("--evidence-root", type=Path, default=DATA_DIR,
                        help="root that recorded evidence paths are rewritten against "
                             f"(default: {DATA_DIR})")
    config_default = PROJECT_ROOT / "config" / "report.yaml"
    parser.add_argument("--config", type=Path, default=config_default,
                        help=f"report configuration (default: {config_default})")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR,
                        help=f"where the PDF and manifest are written (default: {OUTPUT_DIR})")
    parser.add_argument("--stop-after", choices=tuple(STAGES), default=None,
                        help="run the pipeline only as far as this stage")
    parser.add_argument("--dump-record", type=Path, metavar="PATH", default=None,
                        help="write the parsed record as JSON to PATH, or to stdout for -")
    parser.add_argument("--list-stages", action="store_true",
                        help="print the stages that are built, in order, and exit")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="log at debug level")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the pipeline. Returns the process exit status."""
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    if "--list-stages" in argv:
        for position, name in enumerate(STAGES, 1):
            print(f"{position}. {name}")
        return 0

    args = parser.parse_args(argv)
    configure_logging(logging.DEBUG if args.verbose else logging.INFO)
    ensure_runtime_dirs()

    options = Options(
        record=args.record, evidence_root=args.evidence_root, config=args.config,
        output_dir=args.output_dir, stop_after=args.stop_after,
        dump_record=args.dump_record,
    )

    try:
        state = run_pipeline(options)
    except RecordParseError as error:
        logger.error("%s", error)
        return 1
    except OSError as error:
        logger.error("%s", error)
        return 1

    _summarise(state)
    if options.dump_record is not None and state.record is not None:
        _dump_record(state.record, options.dump_record)
    return 0


if __name__ == "__main__":
    sys.exit(main())
