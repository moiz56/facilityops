"""Loading a corpus: every run record under a directory, oldest first.

Parsing a record is common/loader.py's job. The directory and file patterns
come from config/agent_path.yaml, passed in.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from common.loader import load_record
from common.schema import Record


def find_records(directory: Path, patterns: Sequence[str]) -> list[Path]:
    """Every record file under directory matching any pattern, each once.

    A run keeps its record at <run>/run.json or <run>/records/run.json. Sorted
    so the list is the same every run (a set iterates in hash order).
    """
    found: set[Path] = set()
    for pattern in patterns:
        found.update(directory.glob(pattern))
    return sorted(found)


def load_corpus(directory: Path, patterns: Sequence[str]) -> list[Record]:
    """Every record under directory, oldest first.

    Ordered by start_time; a record with none sorts last rather than being
    dropped. Ties break on the run id's leading timestamp (<YYYYMMDD_HHMMSS>),
    not the whole id, so renaming a route cannot reorder the corpus. The sort
    is stable, so full ties keep find_records' order.
    """
    records = [load_record(path) for path in find_records(directory, patterns)]
    return sorted(
        records,
        key=lambda record: (
            record.start_time is None,
            record.start_time,
            record.run_id.split("-", 1)[0],
        ),
    )
