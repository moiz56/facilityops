"""Ask the record only when two routers agree on the first step.

The tenant's records are loaded first, once: scope, eligibility, the
extended record and both databases. Then the semantic router (src/router,
its saved calibrated_router.yaml) scores the question.

Below routing.min_confidence (config/intents.yaml) the question is
unroutable: the answer is that the records do not contain it, followed by
what the records do hold about the checkpoint or zone it names, if one is
recognised. Nothing is guessed and the model is not called.

Otherwise the question also goes to the model's retrieval router. Each says
lookup, derivation or abstain. When they agree, the question is answered
from the next router on, logged and printed as `query.main --ask` does.
When they disagree, it stops there and nothing is answered.

The semantic router's confidence goes in the answer as routing_confidence.

    PYTHONPATH=src python -m query.agreed_route --tenant tenant_racks --ask "which zone is a3_back in"

The steps are query.ask's, in this file so the records are loaded before
routing and only once. The model's retrieval router is called once: its
reply is handed back to answer_question in place of a second call, so the
step it agreed on is the step the answer takes.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone

from semantic_router import load_router

from agents.b1_analytical import ABSTAIN, answer_question
from agents.b1_answer_pdf import write_answer_pdf
from agents.b1_retrieval_router import RetrievalError, classify_question
from agents.database_derivation import open_database
from agents.database_lookup import open_lookup_database
from agents.derivation import compute_eligibility, extend_record
from agents.envelope import envelope
from agents.provider import Provider, ProviderError
from agents.utils import eligibility_by_run, print_b1_answer, print_b1_trace, print_runs
from common import PROJECT_ROOT
from common.loader import RecordParseError
from common.paths import ConfigError, load_config, setting
from common.schema import Record
from query.ask import load_settings, log_answer
from query.scope import QueryContext, ScopeError, resolve
from router.try_router import ROUTER_FILE, normalise

# The first steps the two routers can agree on.
STEPS = ("lookup", "derivation", "abstain")

# What a checkpoint can carry, named as the unroutable answer names it.
HELD = ("inspection results", "sensor readings", "evidence captures", "findings", "operator notes")


class Replay:
    """The provider, except that its first call returns a reply already given.

    answer_question's first model call is the retrieval router's, so it gets
    the reply the agreement was checked on; every later call goes to the model.
    """

    def __init__(self, provider: Provider, reply: str):
        self.provider = provider
        self.reply = reply
        self.model = provider.model

    def complete(self, prompt: str, temperature: float = 0.0, schema: dict | None = None) -> str:
        if self.reply is not None:
            reply, self.reply = self.reply, None
            return reply
        return self.provider.complete(prompt, temperature=temperature, schema=schema)


def held_about(records: list[Record], question: str) -> str | None:
    """What the records hold about the checkpoint or zone the question names,
    as a sentence, or None when it names neither.

    A name is recognised only when it is a checkpoint_id or zone in these
    records, matched as a whole word, case aside. A checkpoint is looked for
    before a zone, so a name that is both (home) is read as the checkpoint.
    """
    words = {word.lower() for word in re.findall(r"[A-Za-z0-9_]+", question)}
    checkpoints = [c for r in records for c in r.checkpoints]
    named = next((c.checkpoint_id for c in checkpoints if c.checkpoint_id.lower() in words), None)
    if named is not None:
        mine = [c for c in checkpoints if c.checkpoint_id == named]
        findings = [f for r in records for f in r.findings if f.checkpoint_id == named]
    else:
        named = next((c.zone for c in checkpoints if c.zone.lower() in words), None)
        if named is None:
            return None
        mine = [c for c in checkpoints if c.zone == named]
        ids = {c.checkpoint_id for c in mine}
        findings = [f for r in records for f in r.findings if f.zone == named or f.checkpoint_id in ids]

    has = {
        "inspection results": any(c.result_status for c in mine),
        "sensor readings": any(
            c.sensor and (c.sensor.accelerometer or c.sensor.environment or c.sensor.particulate) for c in mine
        ),
        "evidence captures": any(c.evidence_images or c.annotated_images for c in mine),
        "findings": bool(findings),
        "operator notes": any(c.notes for c in mine),
    }
    kinds = [kind for kind in HELD if has[kind]]
    if not kinds:
        return None
    listed = kinds[0] if len(kinds) == 1 else f"{', '.join(kinds[:-1])} and {kinds[-1]}"
    return f"For {named} the records hold {listed}."


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ask a question of one tenant's records when both routers agree.")
    parser.add_argument("--tenant", required=True, help="the tenant asking, as named in config/tenants.yaml")
    parser.add_argument("--ask", required=True, help="the question")
    parser.add_argument("--facility", action="append", default=[],
                        help="limit to this facility (repeatable); default every facility the tenant owns")
    parser.add_argument("--from", dest="start", help="first run date, YYYY-MM-DD (needs --to)")
    parser.add_argument("--to", dest="end", help="last run date, YYYY-MM-DD (needs --from)")
    parser.add_argument("--asked-by", default="cli", help="who is asking")
    return parser.parse_args(argv)


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if bool(args.start) != bool(args.end):
        return fail("--from and --to go together")
    question = args.ask.strip()
    if not question:
        return fail("the question is empty")
    if not ROUTER_FILE.exists():
        return fail(f"{ROUTER_FILE} not found: run python src/router/try_router.py first to calibrate")

    context = QueryContext(
        tenant_id=args.tenant,
        facility_ids=tuple(args.facility),
        run_window=(args.start, args.end) if args.start else None,
        asked_by=args.asked_by,
        asked_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    try:
        settings = load_settings(PROJECT_ROOT)
        if not settings.b1.enabled:
            return fail("B-1 is disabled in agents.yaml")
        if len(question) > settings.b1.max_question_chars:
            return fail(f"the question is longer than {settings.b1.max_question_chars} characters")
        min_confidence = setting(load_config(PROJECT_ROOT / "config" / "intents.yaml"), "routing", "min_confidence")
        if isinstance(min_confidence, bool) or not isinstance(min_confidence, (int, float)) or not 0 < min_confidence <= 1:
            return fail(f"intents.yaml: routing.min_confidence must be a number above 0 and at most 1, got {min_confidence!r}")
        semantic = load_router(ROUTER_FILE)
        missing = [step for step in STEPS if step not in {r.name for r in semantic.routes}]
        if missing:
            return fail(f"{ROUTER_FILE.name} has no {', '.join(missing)} route: run python src/router/try_router.py to refit")

        # 1. Load: the tenant's records, once.
        records = resolve(context, settings.tenants, settings.root)
        if not records:
            output = {"answer": ABSTAIN, "citations": [], "records_consulted": [], "records_in_scope": 0}
            answer_envelope = envelope("B-1", None, settings.b1.prompt_version, [], "OK", output, None)
            log_answer(question, context, answer_envelope, settings)
            print("no runs in scope")
            print(json.dumps(answer_envelope, indent=2))
            return 0
        print_runs(records)
        eligibility = compute_eligibility(records, settings.derivation)
        extended = extend_record(records, settings.derivation, eligibility)
        conn, reused = open_database(extended, settings.derivation, settings.database)
        lookup_conn, lookup_reused = open_lookup_database(
            eligibility_by_run(records, eligibility, settings.derivation),
            settings.derivation, settings.lookup_database,
        )
        print(f"database: {'reused' if reused else 'built and saved to'} {settings.database}")
        print(f"lookup database: {'reused' if lookup_reused else 'built and saved to'} {settings.lookup_database}\n")

        # 2. Route. The semantic router's best route, whatever its confidence:
        # min_confidence below decides, not the threshold saved with it.
        choice = semantic(normalise(question), threshold=float("-inf"))
        routing_confidence = choice.confidence
        confidence = f"{routing_confidence:.2f}" if routing_confidence is not None else "-"
        if routing_confidence is None or routing_confidence < min_confidence:
            print(f"semantic router: unroutable  confidence {confidence}  (min_confidence {min_confidence})")
            held = held_about(records, question)
            answer = "The records do not contain this." + (f" {held}" if held else "")
            output = {
                "answer": answer, "citations": [], "records_consulted": [r.run_id for r in records],
                "routing_confidence": routing_confidence, "records_in_scope": len(records),
            }
            answer_envelope = envelope(
                "B-1", None, settings.b1.prompt_version, output["records_consulted"], "OK", output, None,
            )
            log_answer(question, context, answer_envelope, settings)
            print(json.dumps(answer_envelope, indent=2))
            return 0
        semantic_step = choice.name
        print(f"semantic router: {semantic_step}  confidence {confidence}  (min_confidence {min_confidence})")
        calls: list[dict] = []
        try:
            retrieval = classify_question(question, settings.provider, settings.b1, settings.derivation, calls)
        except RetrievalError as error:
            print(f"model router:    no valid reply ({error})")
            print("stopped: no agreement")
            return 0
        print(f"model router:    {retrieval.kind}  ({retrieval.reason})")
        if retrieval.kind != semantic_step:
            print("stopped: the routers disagree")
            return 0
        print(f"agreed: {retrieval.kind}  confidence {confidence}\n")

        # 3. Answer: from the next router on, with the agreed first step.
        provider = Replay(settings.provider, calls[-1]["reply"])
        result = answer_question(
            question, extended, conn, provider, settings.b1,
            settings.derivation, settings.verification, lookup_conn, settings.catalogue,
        )
        output = result.output
        if output is not None:
            output = {**output, "routing_confidence": routing_confidence, "records_in_scope": len(records)}
        run_ids = (output or {}).get("records_consulted") or [r.run_id for r in records]
        answer_envelope = envelope(
            "B-1", settings.provider.model, settings.b1.prompt_version, run_ids,
            result.status, output, result.verification,
        )

        # 4. Log, print and write the PDF, as query.ask does.
        trace = result.trace or {}
        chosen = trace.get("router", {}).get("route")
        routed_to = None
        if chosen and not chosen["abstain"]:
            routed_to = chosen["derivations"] if "derivations" in chosen else chosen["tables"]
        log_answer(question, context, answer_envelope, settings, retrieval.kind, routed_to)

        print(json.dumps(answer_envelope, indent=2))
        print(f"\nattempts: {result.attempts}")
        if result.reason:
            print(f"reason: {result.reason}")
        if result.trace:
            print_b1_trace(result.trace)
        print_b1_answer(result, extended)
        if settings.b1.pdf:
            path = write_answer_pdf(
                question, result, answer_envelope, extended, settings.derivation, settings.report,
                settings.b1_answers, settings.evidence_root,
            )
            print(f"\nanswer written to {path}")
    except (ConfigError, ScopeError, ProviderError, RecordParseError, ValueError, sqlite3.Error, OSError) as error:
        return fail(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
