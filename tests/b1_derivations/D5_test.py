"""B-1 questions answered by D5 (group_max).

Each case is a question and the status its answer PDF should show: OK,
ABSTAINED, DEGRADED_TEMPLATE_ONLY, REFUSED_UNVERIFIABLE or PROVIDER_UNAVAILABLE.
The PDFs are written to tests/output/b1/D5/.
"""

import pytest

CASES = [
    # --- Per zone ---
    # Every zone, one run.
    ("Maximum vibration per zone in the latest run?", "OK"),
    # One zone, named as recorded, with the reading that reached it.
    ("Peak temperature in rowA_back in the latest run, and which checkpoint recorded it?", "OK"),
    # Two zones, written in words, across every run.
    ("Maximum temperature in row A back and row B back, per run?", "OK"),
    # The zone with the highest peak, ties kept.
    ("Which zone had the highest peak temperature in the latest run?", "OK"),
    # The zone with the lowest peak: the lowest of the zone maxima is allowed.
    ("Which zone had the lowest peak vibration in the latest run?", "OK"),
    # When the peak was taken, on a day with no year.
    ("When was the peak temperature in each zone recorded for the runs on 19 August?", "OK"),
    # A zone whose checkpoint was missed: NOT_COMPUTABLE, with its reason.
    ("Maximum temperature in undock_clear in the run on 5 August?", "OK"),

    # --- Per checkpoint: answered with the zone it is in and its source_id ---
    # One checkpoint inside a row zone.
    ("Maximum vibration at a3_back in the latest run?", "OK"),
    # Two checkpoints in different zones.
    ("Peak temperature at a1_back and b3_back in the latest run?", "OK"),
    # Whether a named checkpoint was the one that reached its zone's peak.
    ("Was a3_back the checkpoint with the highest temperature in its zone in the latest run?", "OK"),
    # One checkpoint across every run: only the runs that have it.
    ("Maximum temperature at a3_back, per run?", "OK"),

    # --- Where the peak was ---
    # The run's peak, and the zone and checkpoint it came from.
    ("Where was the highest vibration reading in the latest run: which zone and which checkpoint?", "OK"),
    # Across runs, ties kept.
    ("Which run had the highest peak temperature, and at which checkpoint?", "OK"),

    # --- Zone to checkpoints ---
    ("Which checkpoints are in the rowA_back zone in the latest run?", "OK"),

    # --- A name that is both a zone and a checkpoint ---
    # Meant as the stop.
    ("Maximum vibration at checkpoint 1 in the first run?", "OK"),
    # Meant as the area.
    ("Peak temperature in the checkpoint_3 zone, per run?", "OK"),

    # --- Several maxima ---
    ("Maximum temperature and vibration per zone in the latest run?", "OK"),
    # Particulate: its sensor was flagged, so the answer states why there is no maximum.
    ("Peak PM2.5 per zone in the latest run?", "OK"),

    # --- Abstain ---
    # A zone the records do not hold.
    ("Maximum vibration in rowZ_back in the latest run?", "ABSTAINED"),
    # A checkpoint the records do not hold.
    ("Maximum vibration at checkpoint 99 in the latest run?", "ABSTAINED"),
    # Only maxima are held: the lowest reading is not.
    ("What was the lowest temperature reading in rowA_back in the latest run?", "ABSTAINED"),
    # A reading with no maximum configured (humidity).
    ("Maximum humidity per zone in the latest run?", "ABSTAINED"),
    # Out of the date range.
    ("Peak vibration per zone for the runs on 2 July 2026?", "ABSTAINED"),
]

@pytest.mark.parametrize("question, expected", CASES)
def test_d5(ask, question, expected):
    assert ask(question, "D5") == expected
