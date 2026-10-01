"""B-1 questions answered by D7 (run_set_difference).

Each case is a question and the status its answer PDF should show: OK,
ABSTAINED, DEGRADED_TEMPLATE_ONLY, REFUSED_UNVERIFIABLE or PROVIDER_UNAVAILABLE.
The PDFs are written to tests/output/b1/D7/.
"""

import pytest

CASES = [
    # --- The whole picture ---
    # Both runs and all three counts, plus what was dropped and added.
    ("Did the route change between the last two runs?", "OK"),
    ("Compare the checkpoints of the latest run with the run before it.", "OK"),
    ("Is the latest run's route the same as the previous one?", "OK"),

    # --- One membership, answered even when empty ---
    # only_in_b: may be empty, so the answer should say none, with the two runs.
    ("Which checkpoints are new in the latest run?", "OK"),
    # only_in_a in everyday words.
    ("Were any checkpoints dropped from the route since the previous run?", "OK"),
    # in_both: the count, not the whole route listed.
    ("How many checkpoints did the last two runs have in common?", "OK"),

    # --- Counts ---
    ("How many checkpoints were added and how many removed in the latest run?", "OK"),
    # Which runs were compared.
    ("Which two runs does the route comparison cover?", "OK"),

    # --- Checkpoints ---
    # A named checkpoint's membership.
    ("Was a3_back on the route in both of the last two runs?", "OK"),
    # Two checkpoints at once.
    ("Are c7_back and predock on both of the last two routes?", "OK"),
    # A name that is both a zone and a checkpoint, meant as the stop.
    ("Was ac_1 dropped from the route in the latest run?", "OK"),
    # A checkpoint on neither route: the records do not hold it, so abstain.
    ("Was checkpoint 99 added to the route in the latest run?", "ABSTAINED"),

    # --- Abstain ---
    # Only the last two runs are compared.
    ("Which checkpoints differ between the first and the third run?", "ABSTAINED"),
    ("How did the route change between the runs on 19 August and 21 August?", "ABSTAINED"),
    # Missed or skipped is a checkpoint's status, not a route change.
    ("Which checkpoints did the latest run skip?", "ABSTAINED"),
    # Never better or worse, never why.
    ("Is the latest route better than the previous one?", "ABSTAINED"),
    ("Why was the route changed?", "ABSTAINED"),
]

@pytest.mark.parametrize("question, expected", CASES)
def test_d7(ask, question, expected):
    assert ask(question, "D7") == expected
