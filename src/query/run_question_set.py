"""Ask the question sets' questions through the router, the model, or both.

    PYTHONPATH=src python -m query.run_question_set --use router
    PYTHONPATH=src python -m query.run_question_set --use model --tenant tenant_racks
    PYTHONPATH=src python -m query.run_question_set --use both --tenant tenant_racks --ids Q01 Q24 --canonical-only

  router  the saved semantic routers only (src/router), no model, no tenant
  model   the model only, through query.ask, with no semantic check
  both    the model, checked by the semantic routers, through query.agreed_route

Questions come from config/b1_question_set.yaml and
config/b1_question_set_extension.yaml: each question, then its paraphrases.
A question passes when it must abstain and is refused (abstained or
unroutable), or when it is answerable and is answered.
"""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone

import yaml
from semantic_router import load_router

from agents.b1_analytical import ABSTAIN
from common import PROJECT_ROOT
from common.paths import load_config
from query import agreed_route
from query.agreed_route import NEXT_ROUTER_FILES, confidence_bars
from query.ask import ask, load_settings
from query.scope import QueryContext
from router.first_step_router import ROUTER_FILE, normalise

QUESTION_SETS = ("config/b1_question_set.yaml", "config/b1_question_set_extension.yaml")


def load_questions(ids: list[str], canonical_only: bool) -> list[dict]:
    """Every question to ask: {id, must_abstain, text}, in file order."""
    asked = []
    for path in QUESTION_SETS:
        for entry in yaml.safe_load((PROJECT_ROOT / path).read_text(encoding="utf-8"))["questions"]:
            qid = str(entry["id"]).split()[0]   # some ids carry a note, e.g. "Q21 (ALREADY IMPLEMENTED)"
            if ids and qid not in ids:
                continue
            texts = [entry["question"]] + ([] if canonical_only else entry.get("paraphrases") or [])
            asked += [{"id": qid, "must_abstain": bool(entry.get("must_abstain")), "text": t.strip()} for t in texts]
    return asked


def by_router(question: str, routers: dict, bars: dict) -> str:
    """The semantic routers' route, e.g. derivation > group_mean 0.76, or unroutable."""
    routed = normalise(question)
    choice = routers["first_step"](routed, threshold=float("-inf"))
    if choice.confidence < bars["first_step"][choice.name]:
        return f"unroutable ({choice.name} {choice.confidence:.2f})"
    result = f"{choice.name} {choice.confidence:.2f}"
    if choice.name in NEXT_ROUTER_FILES:
        pick = routers[choice.name](routed, threshold=float("-inf"))
        if pick.confidence < bars[choice.name][pick.name]:
            return f"unroutable ({result} > {pick.name} {pick.confidence:.2f})"
        result += f" > {pick.name} {pick.confidence:.2f}"
    return result


def by_model(question: str, tenant: str, settings) -> str:
    """query.ask's answer: ANSWERED, ABSTAINED, or the envelope's status."""
    context = QueryContext(tenant, (), None, "question_set", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    answer_envelope = ask(question, context, settings)
    return outcome(answer_envelope)


def by_both(question: str, tenant: str) -> str:
    """query.agreed_route's answer: ANSWERED, ABSTAINED, UNROUTABLE, STOPPED, or the envelope's status."""
    printed = io.StringIO()
    with redirect_stdout(printed), redirect_stderr(printed):
        code = agreed_route.main(["--tenant", tenant, "--ask", question, "--asked-by", "question_set"])
    text = printed.getvalue()
    if code != 0:
        return f"ERROR {text.strip().splitlines()[-1]}"
    lines = text.splitlines()
    if any(line.startswith("unroutable:") for line in lines):
        return "UNROUTABLE"
    if any(line.startswith("stopped:") for line in lines):
        return "STOPPED"
    start = lines.index("{")
    return outcome(json.loads("\n".join(lines[start:lines.index("}", start) + 1])))


def outcome(answer_envelope: dict) -> str:
    answer = (answer_envelope.get("output") or {}).get("answer")
    if answer == ABSTAIN:
        return "ABSTAINED"
    return "ANSWERED" if answer_envelope["status"] == "OK" else answer_envelope["status"]


def passed(must_abstain: bool, result: str) -> bool:
    refused = result.startswith(("unroutable", "abstain", "ABSTAINED", "UNROUTABLE"))
    if must_abstain:
        return refused
    return not refused and not result.startswith(("ERROR", "STOPPED", "REFUSED", "PROVIDER"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask the question sets through the router, the model, or both.")
    parser.add_argument("--use", choices=("router", "model", "both"), required=True)
    parser.add_argument("--tenant", help="the tenant asking, as named in config/tenants.yaml (model and both)")
    parser.add_argument("--ids", nargs="+", default=[], help="only these questions, e.g. Q01 Q24")
    parser.add_argument("--canonical-only", action="store_true", help="each question as written, no paraphrases")
    args = parser.parse_args()
    if args.use != "router" and not args.tenant:
        parser.error(f"--use {args.use} needs --tenant")

    asked = load_questions(args.ids, args.canonical_only)
    if args.use == "router":
        routers = {"first_step": load_router(ROUTER_FILE)}
        routers.update({kind: load_router(path) for kind, path in NEXT_ROUTER_FILES.items()})
        bars = confidence_bars(load_config(PROJECT_ROOT / "config" / "intents.yaml").get("routing"))
    if args.use == "model":
        settings = load_settings(PROJECT_ROOT)

    passes = 0
    for n, question in enumerate(asked, 1):
        try:
            if args.use == "router":
                result = by_router(question["text"], routers, bars)
            elif args.use == "model":
                result = by_model(question["text"], args.tenant, settings)
            else:
                result = by_both(question["text"], args.tenant)
        except Exception as error:   # one question's failure is that question's ERROR, not the end of the run
            result = f"ERROR {error}"
        ok = passed(question["must_abstain"], result)
        passes += ok
        expected = "abstain" if question["must_abstain"] else "answer"
        print(f"{n:>3}/{len(asked)}  {question['id']:<4} expected {expected:<7}  {'pass' if ok else 'FAIL'}  "
              f"{result:<50}  {question['text']}")

    print(f"\n{passes} of {len(asked)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
