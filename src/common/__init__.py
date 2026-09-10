"""Shared helpers: filesystem layout, directory setup and logging.

Importing this package has no side effects beyond attaching a null log handler;
callers invoke ensure_runtime_dirs() and configure_logging() themselves.
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"

# No EVIDENCE_DIR or RECORDS_DIR constant: a supplied run brings its own tree,
# rooted at data/<run_id>/, so there are no fixed evidence and records
# directories to create. The evidence root and record path are CLI arguments.


def ensure_runtime_dirs() -> None:
    """Create the data and output directories if they are missing."""
    for directory in (DATA_DIR, OUTPUT_DIR):
        directory.mkdir(parents=True, exist_ok=True)


LOG_FORMAT = "%(levelname)-8s %(name)s: %(message)s"

# Library code emits records but never decides where they go. Without this
# handler Python prints a "no handlers" warning when an entry point has not
# called configure_logging().
logging.getLogger(__name__).addHandler(logging.NullHandler())


def configure_logging(level: int = logging.INFO) -> None:
    """Send log records to stderr at the given level.

    Called by the CLI entry point, never on import. stdout is left clear so a
    caller can redirect report output without log lines mixed into it.
    """
    logging.basicConfig(level=level, format=LOG_FORMAT, stream=sys.stderr)
