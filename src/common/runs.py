"""The run before this one, found from the run folders beside it.

A run folder is named after its run_id, <YYYYMMDD_HHMMSS>-<facility_id>, so a
route's runs sort by time from their folder names alone. Both layouts work,
because the record sits at the same place inside every run folder:

    data/<run_id>/records/run.json
    data_tenant/<facility_id>/<run_id>/run.json

data/ holds every route's runs side by side, so a sibling counts only when its
name ends in this run's facility_id. Only the previous run's record is read.

In common, not report: the seal chain links to the same previous run, and
src/seal may import nothing but common.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from common.loader import RecordParseError, load_record
from common.schema import Record

RUN_FOLDER = re.compile(r"(\d{8}_\d{6})-(.+)")


def previous_run(record_path: Path, record: Record) -> tuple[Record | None, str | None]:
    """The run of the same route just before this one, or None and the reason.

    The run folder is the one above record_path named after record.run_id.
    Of its siblings named <time>-<facility_id>, the latest with a time before
    this run's is the previous run.

    None when this run's folder cannot be found or its name has no time in it,
    when no earlier run is there, or when the earlier run's record is missing
    or cannot be read. The run before that one is never used in its place.
    """
    record_path = record_path.resolve()
    run_dir = next((p for p in record_path.parents if p.name == record.run_id), None)
    if run_dir is None:
        return None, f"no folder named {record.run_id} above {record_path}"
    own_time = folder_time(run_dir.name, record.facility_id)
    if own_time is None:
        return None, f"folder {run_dir.name} is not named <YYYYMMDD_HHMMSS>-{record.facility_id}"

    earlier = []
    for folder in run_dir.parent.iterdir():
        time = folder_time(folder.name, record.facility_id) if folder.is_dir() else None
        if time is not None and time < own_time:
            earlier.append((time, folder))
    if not earlier:
        return None, f"no run of {record.facility_id} before {record.run_id}"

    _, folder = max(earlier)
    try:
        return load_record(folder / record_path.relative_to(run_dir)), None
    except RecordParseError as error:
        return None, f"the previous run cannot be read: {error}"


def folder_time(name: str, facility_id: str) -> datetime | None:
    """The time a run folder's name starts with, or None when the name is not
    <YYYYMMDD_HHMMSS>-<facility_id>."""
    match = RUN_FOLDER.fullmatch(name)
    if match is None or match.group(2) != facility_id:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
    except ValueError:
        return None
