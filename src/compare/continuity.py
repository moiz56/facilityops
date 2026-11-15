"""Record continuity score for a route over a window.

continuity = (runs_completed * coverage_mean) / runs_expected

Says how complete the record is, not how healthy the facility is.

Read from one route folder, data_tenant/<route>/, and the seals beside its
runs in the sealed folder, output/sealed/<route>/<run_id>/. A run's date is
the date in its folder name, <YYYYMMDD_HHMMSS>-<route>.

Continuity and chain integrity are separate claims and both are reported:
a complete set of runs with a broken chain still scores high, with
chain_intact false.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from common.loader import RecordParseError, load_record
from common.runs import folder_time
from common.schema import Record
from report.derive import computed_counts
from seal.create import seal_path
from seal.verify import INTACT, PASS, TAMPERED, VerifyError, result, verify_seal

#: Places coverage_mean and continuity are rounded to.
PLACES = 2


class ContinuityError(Exception):
    """The route, its schedule or the window cannot be used, so no score is given."""


def runs_per_day(tenants: dict, route_id: str) -> int:
    """The route's runs_per_day, from the tenant whose facility_ids list it."""
    for tenant, entry in (tenants.get("tenants") or {}).items():
        if isinstance(entry, dict) and route_id in (entry.get("facility_ids") or []):
            value = entry.get("runs_per_day")
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ContinuityError(
                    f"tenants.yaml: '{tenant}.runs_per_day' must be a whole number of 1 or more, got {value!r}"
                )
            return value
    raise ContinuityError(f"tenants.yaml: no tenant lists route '{route_id}'")


def route_runs(route_dir: Path) -> list[tuple[datetime, str]]:
    """Each run in the route folder, (run time, run_id), oldest first.

    A folder not named <YYYYMMDD_HHMMSS>-<route> is not a run and is skipped.
    """
    runs = []
    for folder in route_dir.iterdir():
        time = folder_time(folder.name, route_dir.name) if folder.is_dir() else None
        if time is not None:
            runs.append((time, folder.name))
    return sorted(runs)


def seal_file(sealed_dir: Path, run_id: str) -> Path:
    """Where a run's seal is: in its own run folder in the sealed folder."""
    return seal_path(sealed_dir / run_id, run_id)


def coverage(route_dir: Path, run_id: str) -> float:
    """The run's checkpoint completion: COMPLETED out of all its checkpoints.

    0 for a run with no checkpoints, or whose record cannot be read: nothing
    of it can be counted as complete.
    """
    try:
        counts = computed_counts(load_record(route_dir / run_id / "run.json"))
    except RecordParseError:
        return 0.0
    return counts.completed / counts.required if counts.required else 0.0


def seal_checks(route_dir: Path, sealed_dir: Path, run_ids: list[str]) -> dict:
    """seals_verified, seals_broken and chain_intact over the given sealed runs.

    Each seal is checked by verify_seal against the run's record in the route
    folder, whose evidence paths start with the run_id, so the route folder is
    the evidence root. INTACT is verified, TAMPERED is broken, and COULD NOT
    VERIFY, or a seal that cannot be read at all, is neither.

    chain_intact is true only when there is at least one seal and every
    seal's chain check passed: a link that could not be checked is not
    claimed as intact.
    """
    verified = broken = 0
    chain_intact = bool(run_ids)
    for run_id in run_ids:
        try:
            _, checks = verify_seal(seal_file(sealed_dir, run_id), route_dir / run_id / "run.json", route_dir)
        except VerifyError:
            chain_intact = False
            continue
        _, code = result(checks)
        verified += code == INTACT
        broken += code == TAMPERED
        if any(check.status != PASS for check in checks if check.name == "chain"):
            chain_intact = False
    return {"seals_verified": verified, "seals_broken": broken, "chain_intact": chain_intact}


def continuity(
    route_dir: Path, sealed_dir: Path, tenants: dict,
    window_from: date | None = None, window_to: date | None = None,
    before: datetime | None = None,
) -> dict:
    """The continuity score of the route in route_dir.

    The window counts both its end days. A date left out is the first or last
    run date in the folder. Only runs dated inside the window are counted.

    runs_expected is runs_per_day for every day of the window. runs_completed
    is the runs in the window with a seal; a day with more sealed runs than
    the schedule counts them all, so continuity can be above 1. runs_missing
    lists each day short of runs_per_day sealed runs, once for every run it
    is short. coverage_mean is over the sealed runs, and None when there are
    none. continuity uses the unrounded coverage_mean.

    before, when given, leaves out every run at or after that time, so a
    run's own report never counts it: its seal is made from that report.
    """
    route_id = route_dir.name
    per_day = runs_per_day(tenants, route_id)
    runs = [
        (time.date(), run_id) for time, run_id in route_runs(route_dir)
        if before is None or time < before
    ]
    if not runs and (window_from is None or window_to is None):
        raise ContinuityError(f"no runs in {route_dir}, so the window needs both --from and --to")
    window_from = window_from or runs[0][0]
    window_to = window_to or runs[-1][0]
    if window_from > window_to:
        raise ContinuityError(f"the window starts on {window_from}, after it ends on {window_to}")

    sealed = [
        (day, run_id) for day, run_id in runs
        if window_from <= day <= window_to and seal_file(sealed_dir, run_id).is_file()
    ]
    days = (window_to - window_from).days + 1
    runs_expected = per_day * days

    sealed_on = {}
    for day, _ in sealed:
        sealed_on[day] = sealed_on.get(day, 0) + 1
    runs_missing = []
    for offset in range(days):
        day = window_from + timedelta(days=offset)
        runs_missing += [day.isoformat()] * max(per_day - sealed_on.get(day, 0), 0)

    coverages = [coverage(route_dir, run_id) for _, run_id in sealed]
    mean = sum(coverages) / len(coverages) if coverages else None
    score = round(len(sealed) * mean / runs_expected, PLACES) if mean is not None else 0.0

    return {
        "route_id": route_id,
        "window": {"from": window_from.isoformat(), "to": window_to.isoformat()},
        "runs_expected": runs_expected,
        "runs_completed": len(sealed),
        "runs_missing": runs_missing,
        "coverage_mean": round(mean, PLACES) if mean is not None else None,
        "continuity": score,
        "continuity_formatted": f"{score:.0%}",
        **seal_checks(route_dir, sealed_dir, [run_id for _, run_id in sealed]),
    }


def route_continuity(
    record_path: Path, record: Record, sealed_root: Path, tenants: dict,
) -> tuple[dict | None, str | None]:
    """The continuity of this run's route, for its report, or None and why not.

    Only the data_tenant layout has a route folder: the run folder, named
    after run_id, sits in a folder named after facility_id. The window runs
    from the route's first run to this run's date, and only the runs before
    this one are counted. The seals are in sealed_root/<facility_id>.
    """
    run_dir = next((p for p in record_path.resolve().parents if p.name == record.run_id), None)
    if run_dir is None:
        return None, f"no folder named {record.run_id} above {record_path}"
    route_dir = run_dir.parent
    if route_dir.name != record.facility_id:
        return None, f"{run_dir.name} is not in a route folder named {record.facility_id}"
    own_time = folder_time(run_dir.name, record.facility_id)
    if own_time is None:
        return None, f"folder {run_dir.name} is not named <YYYYMMDD_HHMMSS>-{record.facility_id}"
    if not any(time < own_time for time, _ in route_runs(route_dir)):
        return None, f"no run of {record.facility_id} before {record.run_id}"
    try:
        return continuity(
            route_dir, sealed_root / record.facility_id, tenants,
            window_to=own_time.date(), before=own_time,
        ), None
    except ContinuityError as error:
        return None, str(error)
