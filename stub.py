"""The B-1 question set as a test suite: every question in config/b1_question_set.yaml.

Each question is answered end to end, as `python -m agents.main --ask` does
(retrieval router, router, SQL writer, prose writer, verification), and its
answer PDF and trace are written to tests/output/b1/question_set/ before the
check, so a failing case can still be read.

A question with must_abstain: true passes when the answer is "the records do
not contain this" (ABSTAINED). Every other question passes when it is
answered: OK, or DEGRADED_TEMPLATE_ONLY (the facts verified and shown, only
the model's lead-in sentence missing). It calls the real model, so it needs
the API key in .env.

    PYTHONPATH=src pytest stub.py -v                       # the 20 canonical questions (TB-04)
    PYTHONPATH=src pytest stub.py -v -k Q07                # one question
    B1_PARAPHRASES=1 PYTHONPATH=src pytest stub.py -v      # and every paraphrase

The paraphrases are development fixtures, not part of TB-04: they check that
routing does not depend on the canonical wording.
"""

import os

import pytest

from common import PROJECT_ROOT
from common.paths import load_config
from tests.b1_derivations.conftest import ask  # noqa: F401  (the shared fixture: answers, writes PDF and trace)

QUESTION_SET = PROJECT_ROOT / "config" / "b1_question_set.yaml"
ANSWERED = ("OK", "DEGRADED_TEMPLATE_ONLY")


def cases() -> list:
    """(id, question, must_abstain) for every canonical question, and its paraphrases when asked for."""
    found = []
    for q in load_config(QUESTION_SET)["questions"]:
        found.append(pytest.param(q["question"], q["must_abstain"], id=q["id"]))
        if os.environ.get("B1_PARAPHRASES"):
            found += [
                pytest.param(p, q["must_abstain"], id=f"{q['id']}p{n}")
                for n, p in enumerate(q.get("paraphrases") or [], 1)
            ]
    return found


@pytest.mark.parametrize("question, must_abstain", cases())
def test_question_set(ask, question, must_abstain):
    status = ask(question, "question_set")
    if must_abstain:
        assert status == "ABSTAINED", f"must abstain, got {status}"
    else:
        assert status in ANSWERED, f"must be answered, got {status}"
