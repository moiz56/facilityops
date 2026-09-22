"""Loading a corpus: every run record under a directory, oldest first.

Parsing a record is common/loader.py's job and is not repeated here. This
module only finds the files and puts what they hold in order, because every
derivation takes a list of records and several runs are the normal case rather
than the exception.

Where the files are is config/agent_path.yaml's answer, passed in. Nothing here
opens a config file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from common.loader import load_record
from common.schema import Record


def find_records(directory: Path, patterns: Sequence[str]) -> list[Path]:
    """Every record file under `directory` matching any of `patterns`.

    A run keeps its record at `<run>/run.json` or at `<run>/records/run.json`,
    and both layouts appear in the corpus. Matches are collected into a set
    first so a run matching two patterns is still read once.

    Sorted on the way out because a set of paths iterates in hash order, which
    differs between processes. This is not the corpus order - load_corpus
    decides that - it is what makes the list the same list every run.
    """
    found: set[Path] = set()
    for pattern in patterns:
        found.update(directory.glob(pattern))
    return sorted(found)


def load_corpus(directory: Path, patterns: Sequence[str]) -> list[Record]:
    """Every record under `directory`, oldest first.

    Ordered by start_time. A record that recorded none sorts after those that
    did - rather than being dropped, or given a date it does not have.

    The tiebreak is the run id's leading timestamp, not the whole id. A run id
    is `<YYYYMMDD_HHMMSS>-<route>`, and the route part is a name that can be
    changed; sorting on it would let a rename reorder the corpus. The timestamp
    part is fixed width, so comparing it as text orders it as time.
    """
    found = find_records(directory,patterns)
    records = [load_record(path) for path in found]
    records = [load_record(path) for path in find_records(directory, patterns)]
    # The first key keeps a missing start_time out of a comparison with a real
    # one. Sorting is stable, so records tying on all three keep the order
    # find_records read them in, which is why that function sorts at all.
    return sorted(
        records,
        key=lambda record: (
            record.start_time is None,
            record.start_time,
            record.run_id.split("-", 1)[0],
        ),
    )
