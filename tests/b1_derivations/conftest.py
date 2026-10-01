"""Shared setup for the B-1 derivation tests (D1_test.py to D8_test.py).

Builds what `python -m agents.main --ask` builds, once per session: the
configs, the extended record over the agent corpus, the database and the
provider. The tests call the real model, so they need the API key in .env.

Each answer's PDF is written under tests/output/b1/<derivation>/, with a
.json of the same name beside it (the envelope and the trace: the router's
reply, each SQL attempt and why it was rejected, the prose calls). Both are
written before the assert, so a failing case can still be read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.b1_analytical import ABSTAIN, answer_question
from agents.b1_answer_pdf import write_answer_pdf
from agents.data_agent import load_corpus
from agents.database_derivation import open_database
from agents.database_lookup import open_lookup_database
from agents.derivation import compute_eligibility, extend_record
from agents.envelope import envelope
from agents.main import AGENT_PATH_CONFIG, AGENTS_CONFIG, DERIVATIONS_CONFIG, ENV_FILE, REPORT_CONFIG
from agents.provider import make_provider
from agents.utils import (
    b1_config, derivation_config, eligibility_by_run, hash_configs, load_env, provider_config, verification_config,
)
from common import PROJECT_ROOT
from common.paths import load_config, setting

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output" / "b1"


def pdf_status(result) -> str:
    """The status the answer PDF shows, by b1_answer_pdf's rule."""
    if (result.output or {}).get("answer") == ABSTAIN:
        return "ABSTAINED"
    return result.status


@pytest.fixture(scope="session")
def ask():
    """ask(question, derivation) answers question, writes its PDF and returns the PDF's status."""
    load_env(ENV_FILE)
    paths = load_config(AGENT_PATH_CONFIG)
    report = load_config(REPORT_CONFIG)
    derivations = load_config(DERIVATIONS_CONFIG)
    agents = load_config(AGENTS_CONFIG)
    config = derivation_config(report, derivations, hash_configs(report, derivations))
    verification = verification_config(agents, report, derivations)
    b1 = b1_config(agents)

    records = load_corpus(
        PROJECT_ROOT / setting(paths, "agent", "data_dir"), setting(paths, "agent", "record_patterns"),
    )
    eligibility = compute_eligibility(records, config)
    extended = extend_record(records, config, eligibility)
    conn, _ = open_database(extended, config, PROJECT_ROOT / setting(paths, "agent", "database"))
    lookup_conn, _ = open_lookup_database(
        eligibility_by_run(records, eligibility, config), config, PROJECT_ROOT / setting(paths, "agent", "lookup_database"),
    )
    provider = make_provider(provider_config(agents))

    def answer(question: str, derivation: str) -> str:
        result = answer_question(question, extended, conn, provider, b1, config, verification, lookup_conn)
        run_ids = (result.output or {}).get("records_consulted") or [r.run_id for r in extended.records]
        answer_envelope = envelope(
            "B-1", provider.model, b1.prompt_version, run_ids,
            result.status, result.output, result.verification,
        )
        pdf = write_answer_pdf(
            question, result, answer_envelope, extended, config, report, OUTPUT_DIR / derivation,
            PROJECT_ROOT / setting(paths, "agent", "evidence_root"),
        )
        # Beside the PDF: the envelope and how the answer was found (router, SQL attempts, prose).
        pdf.with_suffix(".json").write_text(json.dumps({
            "question": question,
            "pdf_status": pdf_status(result),
            "reason": result.reason,
            "attempts": result.attempts,
            "envelope": answer_envelope,
            "trace": result.trace,
        }, indent=2, default=str) + "\n", encoding="utf-8")
        return pdf_status(result)

    yield answer
    conn.close()
    lookup_conn.close()
