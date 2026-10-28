"""Answer every question in a question set and write each answer as a text file.

Each question is answered the way `python -m agents.main --ask` answers it,
but instead of a PDF, output/b1_answers_txt/<id>.txt holds the question, the
status and the answer. It calls the real model, so it needs the API key in .env.

    PYTHONPATH=src python ask_questions.py                                 # the extension set
    PYTHONPATH=src python ask_questions.py config/b1_question_set.yaml     # another set
"""

import sys
from pathlib import Path

from agents.b1_analytical import ABSTAIN, answer_question
from agents.b1_catalogue import check_catalogue
from agents.data_agent import load_corpus
from agents.database_derivation import open_database
from agents.database_lookup import open_lookup_database
from agents.derivation import compute_eligibility, extend_record
from agents.main import (
    AGENT_PATH_CONFIG, AGENTS_CONFIG, CATALOGUE_CONFIG, DERIVATIONS_CONFIG, ENV_FILE, REPORT_CONFIG,
)
from agents.provider import make_provider
from agents.utils import (
    b1_config, derivation_config, eligibility_by_run, hash_configs, load_env, provider_config, verification_config,
)
from common import PROJECT_ROOT
from common.paths import load_config, setting

QUESTION_SET = PROJECT_ROOT / "config" / "b1_question_set_extension.yaml"
OUTPUT_DIR = PROJECT_ROOT / "output" / "b1_answers_txt"

question_set = Path(sys.argv[1]) if len(sys.argv) > 1 else QUESTION_SET

load_env(ENV_FILE)
paths = load_config(AGENT_PATH_CONFIG)
report = load_config(REPORT_CONFIG)
derivations = load_config(DERIVATIONS_CONFIG)
agents = load_config(AGENTS_CONFIG)
config = derivation_config(report, derivations, hash_configs(report, derivations))
verification = verification_config(agents, report, derivations)
b1 = b1_config(agents)
catalogue = check_catalogue(load_config(CATALOGUE_CONFIG))

records = load_corpus(PROJECT_ROOT / setting(paths, "agent", "data_dir"), setting(paths, "agent", "record_patterns"))
eligibility = compute_eligibility(records, config)
extended = extend_record(records, config, eligibility)
conn, _ = open_database(extended, config, PROJECT_ROOT / setting(paths, "agent", "database"))
lookup_conn, _ = open_lookup_database(
    eligibility_by_run(records, eligibility, config), config, PROJECT_ROOT / setting(paths, "agent", "lookup_database"),
)
provider = make_provider(provider_config(agents))

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
for q in load_config(question_set)["questions"]:
    result = answer_question(
        q["question"], extended, conn, provider, b1, config, verification, lookup_conn, catalogue,
    )
    answer = (result.output or {}).get("answer") or "(no answer shown)"
    status = "ABSTAINED" if answer == ABSTAIN else result.status

    lines = [f"{q['id']}  {q['question']}", "", f"status: {status}"]
    if result.reason:
        lines.append(f"reason: {result.reason}")
    lines += ["", answer]
    (OUTPUT_DIR / f"{q['id']}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{q['id']}  {status}")

conn.close()
lookup_conn.close()
