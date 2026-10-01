"""B-1 questions answered by D2 (condition_count).

Each case is a question and the status its answer PDF should show: OK,
ABSTAINED, DEGRADED_TEMPLATE_ONLY, REFUSED_UNVERIFIABLE or PROVIDER_UNAVAILABLE.
The PDFs are written to tests/output/b1/D2/.
"""

import pytest

CASES = [
    # The count and what it is out of.
    ("How many checkpoints failed in the latest run, out of how many?", "OK"),
    # The failed checkpoints themselves, over every run.
    ("Which checkpoints failed in each run?", "OK"),
    # The failed checkpoints on a specific day.
    ("Which checkpoints have a result status fail for runs on 31st aug?", "OK"),
    # excluded_items: the checkpoints left out and why.
    ("Which checkpoints were left out of the failure count in the latest run, and why?", "OK"),
    # The run with the most, ties kept.
    ("Which run had the most failed checkpoints?", "OK"),
    # Only matching items are stored, so the non-matching ones cannot be read.
    ("Which checkpoints did not fail in the latest run?", "ABSTAINED"),
]

@pytest.mark.parametrize("question, expected", CASES)
def test_d2(ask, question, expected):
    assert ask(question, "D2") == expected
