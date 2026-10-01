"""B-1 questions answered by D8 (run_date_range).

Each case is a question and the status its answer PDF should show: OK,
ABSTAINED, DEGRADED_TEMPLATE_ONLY, REFUSED_UNVERIFIABLE or PROVIDER_UNAVAILABLE.
The PDFs are written to tests/output/b1/D8/.
"""

import pytest

# The runs loaded (from the extended record): 19 runs on 7 days, 5 to 31 August
# 2026: 5 Aug (Wed, 1), 8 Aug (Sat, 1), 14 Aug (Fri, 1), 18 Aug (Tue, 5),
# 19 Aug (Wed, 6), 21 Aug (Fri, 2), 31 Aug (Mon, 3). Update the dates below if
# the data changes.
CASES = [
    # --- The period ---
    ("Over what period were the runs taken?", "OK"),
    # The first run: its time and which run it was.
    ("When was the first run, and which run was it?", "OK"),
    ("When was the most recent inspection run?", "OK"),
    # span_days.
    ("How many days does the inspection data cover?", "OK"),

    # --- How often ---
    # day_count with run_count and span_days.
    ("How often were inspection runs carried out?", "OK"),
    ("How many runs are loaded, and on how many days?", "OK"),
    # days_without_run: a count only.
    ("How many days in the period had no run?", "OK"),

    # --- Days ---
    ("On which days were runs taken, and how many each day?", "OK"),
    # The busiest day, ties kept.
    ("Which day had the most runs?", "OK"),
    # The fewest: several days tie.
    ("Which days had the fewest runs?", "OK"),
    # Weekday and weekend.
    ("Were any runs taken on a weekend?", "OK"),
    ("On which weekdays were runs taken?", "OK"),

    # --- One day or a span of days ---
    # A day with no year.
    ("How many runs were taken on 18 August?", "OK"),
    # A day-first date, with each run's start time.
    ("Which runs were taken on 19/8/2026, and at what time?", "OK"),
    # A span of days.
    ("Which runs happened between 14 and 21 August 2026?", "OK"),

    # --- Named runs ---
    ("When did the third run start?", "OK"),
    ("What time did the latest run start?", "OK"),

    # --- Abstain ---
    # A day inside the period with no run: the router finds no run for that day
    # and abstains, rather than answering "none".
    ("Were there any runs on 10 August?", "ABSTAINED"),
    # Before the first run.
    ("How many runs were taken in July 2026?", "ABSTAINED"),
    # Not held: gaps between runs, weekly rates, how long a run took.
    ("What was the longest gap between two runs?", "ABSTAINED"),
    ("How many runs were carried out per week on average?", "ABSTAINED"),
    ("How long did the latest run take?", "ABSTAINED"),
    # Only the loaded runs: not how long the site has been inspected.
    ("How long has this facility been inspected by the robot?", "ABSTAINED"),
]

@pytest.mark.parametrize("question, expected", CASES)
def test_d8(ask, question, expected):
    assert ask(question, "D8") == expected
