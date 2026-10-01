"""B-1 questions answered by D1 (threshold_compare).

Each case is a question and the status its answer PDF should show: OK,
ABSTAINED, DEGRADED_TEMPLATE_ONLY, REFUSED_UNVERIFIABLE or PROVIDER_UNAVAILABLE.
The PDFs are written to tests/output/b1/D1/.
"""

import pytest

CASES = [
    # The checkpoints over the limit in one run.
    ("Which checkpoints went over the temperature limit in the latest run?", "OK"),
    # The over and not-over counts, every run.
    ("How many checkpoints exceeded the temperature threshold in each run?", "OK"),
    # A day with no year: the router completes it from the days the runs started on.
    ("Which checkpoints exceeded the temperature limit on Aug 31st? and Aug 21th?", "OK"),
    # A day written day first, and the delta.
    ("By how much did each checkpoint go over the temperature limit in the runs on 19/8/2026?", "OK"),
    # The other side of the limit: delta below zero.
    ("Which checkpoints stayed under the temperature limit in the first run, and by how much?", "OK"),
    # Out of the date range: no run started that day, so the router abstains.
    ("Which checkpoints went over the temperature limit on 10 September 2026?", "ABSTAINED"),
    # Several steps at once: a day with no year (several runs), only the checkpoints
    # over the limit, then the largest delta in each run, ties kept.
    ("On Aug 31st, which checkpoint ran hottest above the temperature limit in each run, and by how much?", "OK"),
]

@pytest.mark.parametrize("question, expected", CASES)
def test_d1(ask, question, expected):
    assert ask(question, "D1") == expected
