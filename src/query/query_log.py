"""The query log: one JSON line per question, appended to a file.

Append-only, and write-only from here: nothing in this module reads a log
back, so nothing reachable through ask can return one.
"""

from __future__ import annotations

import json
from pathlib import Path


def append_entry(path: Path, entry: dict) -> None:
    """Add entry to the end of the log at path, creating the file if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as log:
        log.write(json.dumps(entry) + "\n")
