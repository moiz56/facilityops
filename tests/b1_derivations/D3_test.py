"""B-1 questions answered by D3 (proportion).

Each case is a question and the status its answer PDF should show: OK,
ABSTAINED, DEGRADED_TEMPLATE_ONLY, REFUSED_UNVERIFIABLE or PROVIDER_UNAVAILABLE.
The PDFs are written to tests/output/b1/D3/.
"""

import pytest

CASES = [
    # The share in one run.
    ("how much of the route did the robot actually get through on its last run?", "OK"),
    # The share with its numerator and denominator, every run.
    ("can you break down the completion rate for every run, and how many stops each one had?", "OK"),
    # The items in the numerator.
    ("On the very first run, which stops did the robot manage to finish?", "OK"),
    # The run with the highest share, ties kept.
    ("which run was the most complete one? i want the best completion rate", "OK"),
    # A day with no year: every run that started that day.
    ("what fraction of the checkpoints got done on the 19th of august?", "OK"),
    # Out of the date range: no run started that day, so the router abstains.
    ("what % of the route got covered on sept 10?", "ABSTAINED"),
    # Only the items that meet the condition are stored.
    ("which stops got skipped in the most recent run?", "ABSTAINED"),
    # A share of a condition no D3 instance holds (failures are D2, a count).
    ("whats the failure rate for the checkpoints in the latest run?", "ABSTAINED"),
]

@pytest.mark.parametrize("question, expected", CASES)
def test_d3(ask, question, expected):
    assert ask(question, "D3") == expected
