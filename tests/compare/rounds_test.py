"""Comparison: TC-01 to TC-05, against real runs in data_tenant.

The pair is two runs of the SIS facility route a few hours apart. The
second run's route lost checkpoints and missed others, so one pair holds a
route change, a missed checkpoint, changed readings and an unattributed
difference. TC-05 compares one run with itself.
"""

import json
import re

import pytest

from common import PROJECT_ROOT
from common.loader import load_record
from common.paths import CONFIG_PATH, load_config
from compare.rounds import Attribution, compare_rounds
from tests.helpers import section_html

ROUTE = PROJECT_ROOT / "data_tenant" / "sis_facility_checkpoint_route"
PREVIOUS = "20260729_082245-sis_facility_checkpoint_route"
CURRENT = "20260729_110105-sis_facility_checkpoint_route"

#: The words the comparison never uses, in any field name, value or sentence:
#: TC-03's six, and the two more the comparison rule itself names.
JUDGING = (
    "improved", "worse", "better", "degraded", "rising", "falling",
    "deteriorated", "concerning",
)


@pytest.fixture(scope="module")
def config():
    return load_config(CONFIG_PATH)


@pytest.fixture(scope="module")
def previous():
    return load_record(ROUTE / PREVIOUS / "run.json")


@pytest.fixture(scope="module")
def current():
    return load_record(ROUTE / CURRENT / "run.json")


@pytest.fixture(scope="module")
def comparison(previous, current, config):
    return compare_rounds(previous, current, config)


def comparison_html(previous, current, comparison, config) -> str:
    """The report's comparison section for this pair."""
    return section_html(current, ROUTE, config, "comparison", comparison=comparison, previous=previous)


def entries(comparison: dict) -> list[dict]:
    """Every entry that carries an attribution, from every list in the comparison."""
    found = []
    def walk(value):
        if isinstance(value, dict):
            if "attribution" in value:
                found.append(value)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(comparison)
    return found


def test_tc01_route_change_is_not_missed(comparison):
    """A checkpoint absent from the route is ROUTE_CHANGE; one missed in the run is MISSED."""
    changed = comparison["checkpoints"]["only_in_previous"] + comparison["checkpoints"]["only_in_current"]
    by_attribution = {}
    for entry in changed:
        by_attribution.setdefault(entry["attribution"], []).append(entry["id"])

    assert by_attribution.get(Attribution.ROUTE_CHANGE), "the pair has no route change"
    assert by_attribution.get(Attribution.MISSED), "the pair has no missed checkpoint"
    assert not set(by_attribution[Attribution.ROUTE_CHANGE]) & set(by_attribution[Attribution.MISSED])

    # A missed checkpoint carries the run's missed_reason; a route change has none to carry.
    for entry in changed:
        if entry["attribution"] == Attribution.MISSED:
            assert "missed_reason" in entry
        if entry["attribution"] == Attribution.ROUTE_CHANGE:
            assert "missed_reason" not in entry


def test_tc01_route_change_is_absent_from_the_other_route(previous, current, comparison):
    """ROUTE_CHANGE is only ever a checkpoint the other run's route does not hold."""
    before = {cp.checkpoint_id for cp in previous.checkpoints}
    now = {cp.checkpoint_id for cp in current.checkpoints}
    for entry in comparison["checkpoints"]["only_in_previous"]:
        if entry["attribution"] == Attribution.ROUTE_CHANGE:
            assert entry["id"] in before and entry["id"] not in now
        else:
            assert entry["id"] in now
    for entry in comparison["checkpoints"]["only_in_current"]:
        if entry["attribution"] == Attribution.ROUTE_CHANGE:
            assert entry["id"] in now and entry["id"] not in before
        else:
            assert entry["id"] in before


def test_tc02_differing_reading_has_both_values_and_signed_difference(comparison, config):
    changed = [e for e in comparison["reading_changes"] if e["attribution"] == Attribution.VALUE_CHANGED]
    assert changed, "the pair has no differing reading"
    places = config["decimals"]
    for entry in changed:
        assert entry["previous"] is not None and entry["current"] is not None
        field = entry["field"].split(".")[-1]
        digits = places.get(field)
        expected = entry["current"] - entry["previous"] if digits is None else (
            round(round(entry["current"], digits) - round(entry["previous"], digits), digits)
        )
        assert entry["difference"] == pytest.approx(expected)
        assert entry["difference_formatted"][0] in "+-"
        assert set(entry) == {
            "checkpoint_id", "field", "previous", "current", "difference",
            "difference_formatted", "attribution",
        }


@pytest.mark.parametrize("pair", ["differing", "identical"])
def test_tc03_no_judging_word_anywhere(pair, previous, current, config):
    """Not in any field name, any value, or any sentence of the report's section."""
    before = previous if pair == "differing" else current
    comparison = compare_rounds(before, current, config)
    text = json.dumps(comparison) + " " + comparison_html(before, current, comparison, config)
    found = [w for w in JUDGING if re.search(rf"\b{w}\b", text, re.I)]
    assert found == []


def test_tc04_unexplained_difference_is_unattributed_with_no_reason(comparison):
    unattributed = [e for e in entries(comparison) if e["attribution"] == Attribution.UNATTRIBUTED]
    assert unattributed, "the pair has no unattributed difference"
    for entry in unattributed:
        assert "reason" not in entry and "missed_reason" not in entry


def test_tc05_identical_rounds_state_nothing_differs(current, config):
    comparison = compare_rounds(current, current, config)
    assert comparison["days_between"] == 0
    assert comparison["checkpoints"]["only_in_previous"] == []
    assert comparison["checkpoints"]["only_in_current"] == []
    assert comparison["result_changes"] == []
    assert comparison["reading_changes"] == []
    assert comparison["finding_changes"]["only_in_previous"] == []
    assert comparison["finding_changes"]["only_in_current"] == []
    assert comparison["sensors"] == {"only_in_previous": [], "only_in_current": []}
    coverage = comparison["coverage_changes"]
    for name in ("evidence", "missed", "could_not_read"):
        assert coverage[f"{name}_previous"] == coverage[f"{name}_current"]

    html = comparison_html(current, current, comparison, config)
    assert "0 marked as differing" in html
    assert "No measurement differs" in html
