"""B-1 questions answered by D4 (group_mean).

Each case is a question and the status its answer PDF should show: OK,
ABSTAINED, DEGRADED_TEMPLATE_ONLY, REFUSED_UNVERIFIABLE or PROVIDER_UNAVAILABLE.
The PDFs are written to tests/output/b1/D4/.
"""

import pytest

CASES = [
    # --- Per zone ---
    # Every zone, one run.
    ("Mean vibration per zone in the latest run?", "OK"),
    # One zone, named as recorded.
    ("Mean temperature in rowA_back in the latest run?", "OK"),
    # Two zones, written in words, across every run.
    ("Mean temperature in row A back and row B back, per run?", "OK"),
    # The zone with the highest mean, ties kept.
    ("Which zone had the highest mean temperature in the latest run?", "OK"),
    # A zone with no checkpoints of its own, on a day with no year.
    ("Mean temperature in the ac_1 zone for the runs on 19 August?", "OK"),
    # A zone whose checkpoint was missed: NOT_COMPUTABLE, with its reason.
    ("Mean temperature in undock_clear in the run on 5 August?", "OK"),

    # --- Per checkpoint: answered with the zone it is in ---
    # One checkpoint inside a row zone.
    ("Mean vibration at a3_back in the latest run?", "OK"),
    # Two checkpoints in different zones.
    ("Mean temperature at a1_back and b3_back in the latest run?", "OK"),
    # Which zone a checkpoint is in, and that zone's mean.
    ("Which zone is a3_back in, and what was its mean temperature in the latest run?", "OK"),
    # One checkpoint across every run: only the runs that have it.
    ("Mean temperature at a3_back, per run?", "OK"),

    # --- Zone to checkpoints ---
    ("Which checkpoints are in the rowA_back zone in the latest run?", "OK"),

    # --- A name that is both a zone and a checkpoint ---
    # Meant as the stop.
    ("Mean vibration at checkpoint 1 in the first run?", "ABSTAINED"),
    # Meant as the area.
    ("Mean temperature in the checkpoint_3 zone, per run?", "OK"),

    # --- Several means ---
    ("Mean temperature and vibration per zone for the runs on 19 August?", "OK"),
    # Particulate: its sensor was flagged, so the answer states why there is no mean.
    ("Mean PM2.5 per zone in the latest run?", "OK"),

    # --- Abstain ---
    # A zone the records do not hold.
    ("Mean vibration in rowZ_back in the latest run?", "ABSTAINED"),
    # A checkpoint the records do not hold.
    ("Mean vibration at checkpoint 99 in the latest run?", "ABSTAINED"),
    # A reading with no mean configured (humidity).
    ("Mean humidity per zone in the latest run?", "ABSTAINED"),
    # A whole-run mean: every mean is per zone, and a mean of zone means is not it.
    ("Overall mean temperature for the latest run?", "ABSTAINED"),
    # Out of the date range.
    ("Mean vibration per zone for the runs on 2 July 2026?", "ABSTAINED"),
]

@pytest.mark.parametrize("question, expected", CASES)
def test_d4(ask, question, expected):
    assert ask(question, "D4") == expected
