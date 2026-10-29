"""Answer every question in a question set as one tenant, and write each answer as a text file.

Each question goes through query.ask, the way `python -m query.main --ask`
answers it: scoped to the tenant's records and written to its query log. But
instead of a PDF, output/b1_answers_txt/<tenant>/<id>.txt holds the question,
the status, the records in scope and the answer. It calls the real model, so
it needs the API key in .env.

    PYTHONPATH=src python ask_questions.py                                          # tenant_facility, the extension set
    PYTHONPATH=src python ask_questions.py --tenant tenant_racks                    # another tenant
    PYTHONPATH=src python ask_questions.py config/b1_question_set.yaml             # another set
"""

import argparse
from datetime import datetime, timezone
from pathlib import Path

from agents.b1_analytical import ABSTAIN
from common import PROJECT_ROOT
from common.paths import load_config
from query.ask import ask, load_settings
from query.scope import QueryContext

QUESTION_SET = PROJECT_ROOT / "config" / "b1_question_set_extension.yaml"
OUTPUT_DIR = PROJECT_ROOT / "output" / "b1_answers_txt"

parser = argparse.ArgumentParser(description="Answer a question set as one tenant.")
parser.add_argument("question_set", nargs="?", type=Path, default=QUESTION_SET, help="the question set")
parser.add_argument("--tenant", default="tenant_facility", help="the tenant asking, as named in config/tenants.yaml")
args = parser.parse_args()

settings = load_settings(PROJECT_ROOT)
output_dir = OUTPUT_DIR / args.tenant
output_dir.mkdir(parents=True, exist_ok=True)

for q in load_config(args.question_set)["questions"]:
    context = QueryContext(
        tenant_id=args.tenant,
        facility_ids=(),
        run_window=None,
        asked_by="ask_questions",
        asked_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    answer_envelope = ask(q["question"], context, settings)
    output = answer_envelope["output"] or {}
    answer = output.get("answer") or "(no answer shown)"
    status = "ABSTAINED" if answer == ABSTAIN else answer_envelope["status"]

    lines = [
        f"{q['id']}  {q['question']}", "",
        f"status: {status}",
        f"records in scope: {output.get('records_in_scope')}", "",
        answer,
    ]
    (output_dir / f"{q['id']}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{q['id']}  {status}")
