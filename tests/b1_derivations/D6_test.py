"""B-1 questions answered by D6 (rank_top_n).

Each case is a question and the status its answer PDF should show: OK,
ABSTAINED, DEGRADED_TEMPLATE_ONLY, REFUSED_UNVERIFIABLE or PROVIDER_UNAVAILABLE.
The PDFs are written to tests/output/b1/D6/.
"""

import pytest

CASES = [
    # --- The ranking itself ---
    # Every ranked reading, one run.
    ("What were the five highest vibration readings in the latest run?", "OK"),
    # Temperature ranking, every run.
    ("Top five temperature readings in each run?", "OK"),
    # Fewer than held: the top three, ties kept.
    ("Which three checkpoints had the highest vibration in the latest run?", "OK"),
    # The single highest, ties kept.
    ("Which checkpoint had the highest temperature reading in the first run?", "OK"),
    # More than held: the answer says only five are kept.
    ("List the top ten vibration readings in the latest run.", "OK"),
    # In rank order, with the readings.
    ("Rank the checkpoints by vibration in the latest run, with their readings.", "OK"),

    # --- Checkpoints ---
    # Whether a named checkpoint is in the ranking, and where.
    ("Did a3_back make the top five for vibration in the latest run?", "OK"),
    # Two checkpoints at once, with their rank.
    ("Where did a1_back and a3_back rank for temperature in the latest run?", "OK"),
    # One checkpoint across every run.
    ("In which runs was a3_back among the five highest vibration readings?", "OK"),
    # A name that is both a zone and a checkpoint, meant as the stop.
    ("Was checkpoint 1 in the top five temperature readings in the first run?", "OK"),

    # --- Counts ---
    # Out of how many readings the ranking was taken.
    ("Out of how many checkpoint readings was the vibration ranking taken in the latest run?", "OK"),
    # How many are ranked (more than n on a tie, fewer when few were eligible).
    ("How many readings are in the temperature ranking for each run?", "OK"),

    # --- Across runs and dates ---
    # The run with the highest top reading, ties kept.
    ("Which run had the highest vibration reading, and at which checkpoint?", "OK"),
    # A day with no year: every run that started that day.
    ("Top three temperature readings for the runs on 19 August?", "OK"),
    # A day-first date.
    ("What were the highest vibration readings on 31/8/2026?", "OK"),

    # --- Several rankings ---
    # Two rankings at once (UNION ALL), never compared with each other.
    ("Top five vibration and temperature readings in the latest run?", "OK"),
    # Particulate: its sensor was flagged, so the run says why there is no ranking.
    ("What were the highest PM2.5 readings in the latest run?", "OK"),

    # --- Abstain ---
    # Rankings hold no zones.
    ("What were the five hottest checkpoints in rowA_back in the latest run?", "ABSTAINED"),
    ("Which zone were the top vibration readings in, in the latest run?", "ABSTAINED"),
    # Only the highest are held: not the lowest, not the rest.
    ("Which checkpoint had the lowest vibration reading in the latest run?", "ABSTAINED"),
    # A reading no ranking holds (humidity).
    ("Top five humidity readings in the latest run?", "ABSTAINED"),
    # A checkpoint the records do not hold.
    ("Did checkpoint 99 make the top five for vibration in the latest run?", "ABSTAINED"),
    # Out of the date range.
    ("Top five vibration readings for the runs on 2 July 2026?", "ABSTAINED"),
    # A mean is not a ranking, and no figure ranks means.
    ("Rank the checkpoints by their average vibration across all runs.", "ABSTAINED"),
]

@pytest.mark.parametrize("question, expected", CASES)
def test_d6(ask, question, expected):
    assert ask(question, "D6") == expected
