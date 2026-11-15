"""Continuity: TC-06 and TC-07, against real routes sealed into tests/output/sealed/.

TC-06 is stretch3: runs on 2026-06-06 and twice on 2026-06-09, one a day
scheduled, so two days of the window have no run. TC-07 is the SIS facility
route: a run every day of its window, with the chain broken in a copy of its
sealed folder by changing one value in its first seal.
"""

import json
import shutil
from datetime import date, datetime, timedelta

import pytest

from common.paths import load_config
from compare.continuity import continuity, coverage, route_runs
from report.cli import TENANTS_CONFIG
from seal.canonical import load_json
from tests.helpers import DATA, sealed_route


@pytest.fixture(scope="module")
def tenants():
    return load_config(TENANTS_CONFIG)


def days_with_no_run(route: str) -> list[str]:
    """The days between the route's first and last run that have no run folder."""
    days = {time.date() for time, _ in route_runs(DATA / route)}
    first, last = min(days), max(days)
    every = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    return [day.isoformat() for day in every if day not in days]


# TC-06

def test_tc06_missing_runs_are_listed_by_date(tenants):
    route = "stretch3_checkpoint_route"
    score = continuity(DATA / route, sealed_route(route), tenants)

    missing = days_with_no_run(route)
    assert len(missing) == 2, "stretch3 should have two days with no run"
    assert score["runs_missing"] == missing

    # The two missing runs are in runs_expected, so they pull the score down:
    # it can be no more than the share of the schedule that was sealed.
    days = (date.fromisoformat(score["window"]["to"]) - date.fromisoformat(score["window"]["from"])).days + 1
    assert score["runs_expected"] == days
    assert score["runs_completed"] == len(route_runs(DATA / route))
    sealed = [run_id for _, run_id in route_runs(DATA / route)]
    mean = sum(coverage(DATA / route, run_id) for run_id in sealed) / len(sealed)
    assert score["continuity"] == round(len(sealed) * mean / score["runs_expected"], 2)


# TC-07

@pytest.fixture
def broken_chain(tmp_path):
    """A copy of the sealed SIS facility route, its first seal changed after sealing."""
    route = "sis_facility_checkpoint_route"
    copy = tmp_path / route
    shutil.copytree(sealed_route(route), copy)
    for path in copy.glob("*/seal_*.json"):
        if path.name.endswith(".record.json"):
            continue
        seal = load_json(path)
        if seal["chain"]["sequence"] == 1:
            seal["sealed_at"] = datetime(2000, 1, 1).strftime("%Y-%m-%dT%H:%M:%SZ")
            path.write_text(json.dumps(seal, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            return copy
    raise AssertionError("no seal with sequence 1")


def test_tc07_complete_runs_with_a_broken_chain(tenants, broken_chain):
    route = "sis_facility_checkpoint_route"
    intact = continuity(DATA / route, sealed_route(route), tenants)
    broken = continuity(DATA / route, broken_chain, tenants)

    # The run set is complete: every day of the window has its run.
    assert days_with_no_run(route) == []
    assert intact["chain_intact"] is True
    assert broken["runs_missing"] == []

    # Continuity is high and the broken chain does not move it ...
    assert broken["continuity"] >= 1.0
    for key in ("runs_expected", "runs_completed", "runs_missing", "coverage_mean", "continuity"):
        assert broken[key] == intact[key], key

    # ... and the chain is reported broken beside it, not hidden by it.
    # The second seal's link to the changed first seal fails; the first
    # seal's own checks do not look at sealed_at, so only one more is broken.
    assert broken["chain_intact"] is False
    assert broken["seals_broken"] == intact["seals_broken"] + 1
