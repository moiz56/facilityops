"""Shared helpers: filesystem layout and directory setup.

Importing this package has no side effects; callers invoke ensure_runtime_dirs().
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
EVIDENCE_DIR = DATA_DIR / "evidence"
RECORDS_DIR = DATA_DIR / "records"
OUTPUT_DIR = PROJECT_ROOT / "output"


def ensure_runtime_dirs() -> None:
    """Create the data and output directories if they are missing."""
    for directory in (DATA_DIR, EVIDENCE_DIR, RECORDS_DIR, OUTPUT_DIR):
        directory.mkdir(parents=True, exist_ok=True)
