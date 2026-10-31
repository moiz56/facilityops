"""Ask the question sets' canonical questions, and keep every answer to read.

    PYTHONPATH=src python -m query.run_milestone3
    PYTHONPATH=src python -m query.run_milestone3 --use both --tenant tenant_racks
    PYTHONPATH=src python -m query.run_milestone3 --ids Q01 Q17

Asks each canonical question once, no paraphrases, through each --use in
turn (router, model and both by default; see query.run_question_set). Each
answer is printed and written to output/milestone3/answers_<use>_<time>.txt:
the question, whether it passed, the intent, the routing confidence and the
answer. The model and both runs also write one answer PDF per question to
output/milestone3/model and output/milestone3/both. The router run calls no
model, so it has a route but no answer and no PDF.

The PDFs are written whatever config/agents.yaml says about pdf, and
config/agent_path.yaml's b1_answers folder is left alone.
"""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone

from semantic_router import load_router

from common import PROJECT_ROOT
from common.paths import load_config
from query import agreed_route
from query.agreed_route import NEXT_ROUTER_FILES, confidence_bars
from query.ask import ask, load_settings
from query.run_question_set import by_router, load_questions, outcome, passed
from query.scope import QueryContext
from router.first_step_router import ROUTER_FILE

OUT_DIR = PROJECT_ROOT / "output" / "milestone3"
USES = ("router", "model", "both")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def settings_writing_pdfs_to(folder):
    """load_settings, with the answer PDF on and written to folder."""
    def load(root):
        settings = load_settings(root)
        return replace(settings, b1=replace(settings.b1, pdf=True), b1_answers=folder)
    return load


def ask_both(question: str, tenant: str) -> tuple[str, dict | None]:
    """query.agreed_route's result and envelope. The envelope is None when it
    stopped or failed, as nothing was answered then."""
    printed = io.StringIO()
    with redirect_stdout(printed), redirect_stderr(printed):
        code = agreed_route.main(["--tenant", tenant, "--ask", question, "--asked-by", "question_set"])
    lines = printed.getvalue().splitlines()
    if code != 0:
        return f"ERROR {lines[-1] if lines else ''}", None
    if any(line.startswith("stopped:") for line in lines):
        return "STOPPED", None
    start = lines.index("{")
    answer_envelope = json.loads("\n".join(lines[start:lines.index("}", start) + 1]))
    if any(line.startswith("unroutable:") for line in lines):
        return "UNROUTABLE", answer_envelope
    return outcome(answer_envelope), answer_envelope


def written(n: int, total: int, question: dict, result: str, ok: bool, answer_envelope: dict | None) -> str:
    """One question's block, as printed and as written to the answers file."""
    expected = "abstain" if question["must_abstain"] else "answer"
    lines = [
        f"[{n}/{total}] {question['id']}  expected {expected}  {'pass' if ok else 'FAIL'}  {result}",
        f"question: {question['text']}",
    ]
    if answer_envelope is not None:
        output = answer_envelope.get("output") or {}
        lines += [
            f"status:   {answer_envelope['status']}",
            f"intent:   {output.get('intent')}",
            f"routing:  {output.get('routing_confidence')}"
            + (f"  second step: {output['second_step_confidence']}" if "second_step_confidence" in output else ""),
            f"records:  {output.get('records_in_scope')} in scope, "
            f"{len(output.get('records_consulted') or [])} consulted, {len(output.get('citations') or [])} citations",
            "answer:",
            *(f"  {line}" for line in (output.get("answer") or "(none)").splitlines()),
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask the canonical questions and keep every answer.")
    parser.add_argument("--use", nargs="+", choices=USES, default=list(USES), help="which runs, in this order")
    parser.add_argument("--tenant", default="tenant_facility", help="the tenant asking, as named in config/tenants.yaml")
    parser.add_argument("--ids", nargs="+", default=[], help="only these questions, e.g. Q01 Q24")
    args = parser.parse_args()

    asked = load_questions(args.ids, canonical_only=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for use in args.use:
        path = OUT_DIR / f"answers_{use}_{stamp}.txt"
        with path.open("w", encoding="utf-8") as answers:
            def out(text: str) -> None:
                print(text)
                answers.write(text + "\n")
                answers.flush()

            out(f"=== --use {use}, tenant {args.tenant if use != 'router' else '(none)'}, {now()} ===\n")
            if use == "router":
                routers = {"first_step": load_router(ROUTER_FILE)}
                routers.update({kind: load_router(p) for kind, p in NEXT_ROUTER_FILES.items()})
                bars = confidence_bars(load_config(PROJECT_ROOT / "config" / "intents.yaml").get("routing"))
            load = settings_writing_pdfs_to(OUT_DIR / use)
            if use == "model":
                settings = load(PROJECT_ROOT)
            if use == "both":
                agreed_route.load_settings = load   # agreed_route loads its own settings

            passes = 0
            for n, question in enumerate(asked, 1):
                answer_envelope = None
                try:
                    if use == "router":
                        result = by_router(question["text"], routers, bars)
                    elif use == "model":
                        context = QueryContext(args.tenant, (), None, "question_set", now())
                        answer_envelope = ask(question["text"], context, settings)
                        result = outcome(answer_envelope)
                    else:
                        result, answer_envelope = ask_both(question["text"], args.tenant)
                except Exception as error:   # one question's failure is that question's ERROR, not the end of the run
                    result = f"ERROR {error}"
                ok = passed(question["must_abstain"], result)
                passes += ok
                out(written(n, len(asked), question, result, ok, answer_envelope))

            out(f"{passes} of {len(asked)} passed")
            if use != "router":
                out(f"answer PDFs are in {OUT_DIR / use}")
        print(f"answers written to {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
