"""Ask the record when the semantic router backs the model's first step.

The tenant's records are loaded first, once: scope, eligibility, the
extended record and both databases. Then the model's retrieval router picks
the first step: lookup, derivation, catalogue or abstain. Its pick is the
one taken. The semantic router (src/router, its saved first_step_router.yaml)
then gives its confidence for that same route, whether or not it is the
semantic router's own best route.

At or above that route's minimum the question is
answered from the next router on, logged and printed as `query.main --ask`
does. Below it the question is unroutable: the answer is that the records do
not contain it, followed by what the records do hold about the checkpoint or
zone it names, if one is recognised. Nothing is guessed.

A route's minimum is its own value under routing.first_step or
routing.second_step in config/intents.yaml, or routing.min_confidence when
it has none.

catalogue ("what can I ask") has no semantic route to check, so it goes
ahead: code writes that answer from the intent catalogue, not the records.

That confidence goes in the answer as routing_confidence.

The second step works the same way. For lookup and derivation the model's
next router picks the tables, or the derivations, the question needs. The
second-step semantic router (src/router, second_step_lookup.yaml or
second_step_derivation.yaml) gives its confidence for each pick: a
derivation is checked by its type, so group_mean_temperature_c is checked as
group_mean. Every pick must be at or above its own minimum, or the
question is unroutable, as above. When the model's next router abstains
there is nothing to check, and answer_question abstains as it always does.

Those confidences go in the answer as second_step_confidence, one per pick,
or null when there was no second step.

routing.second_step_check in config/intents.yaml turns that second check
off: the model's next router still picks, but its picks go ahead unchecked,
the second-step router files are not needed, and nothing about the check is
printed or put in the answer: no second_step_confidence key. The first-step
check is not affected.

    PYTHONPATH=src python -m query.agreed_route --tenant tenant_racks --ask "which zone is a3_back in"

The steps are query.ask's, in this file so the records are loaded before
routing and only once. Each model router is called once: their replies are
handed back to answer_question in place of new calls, so the steps that were
checked are the steps the answer takes.
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
from agents.b1_analytical_router_derived import RECORDS, RouteError, route_question
from agents.b1_analytical_router_lookup import route_lookup
from agents.b1_answer_pdf import write_answer_pdf
from agents.b1_retrieval_router import RetrievalError, classify_question
from agents.database_derivation import open_database
from agents.database_lookup import TABLES, open_lookup_database
from agents.derivation import compute_eligibility, extend_record
from agents.envelope import envelope
from agents.provider import Provider, ProviderError
from agents.routes import PROMPTS
from agents.utils import eligibility_by_run, print_b1_answer, print_b1_trace, print_runs
from common import PROJECT_ROOT
from common.loader import RecordParseError
from common.paths import ConfigError, load_config
from common.schema import Record
from query.ask import Settings, answer_intent, load_settings, log_answer
from query.scope import QueryContext, ScopeError, resolve
from router.first_step_router import ROUTER_FILE, normalise
from router.second_step_router import ROUTER_FILES as NEXT_ROUTER_FILES

# The routes the saved semantic router must have, one per first step it can check.
STEPS = ("lookup", "derivation", "abstain")

# The routes each saved second-step router must have: every derivation type
# with a SQL writer prompt (records among them), and every lookup table.
NEXT_STEPS = {"derivation": tuple(PROMPTS), "lookup": TABLES}

# What a checkpoint can carry, named as the unroutable answer names it.
HELD = ("inspection results", "sensor readings", "evidence captures", "findings", "operator notes")


class Replay:
    """The provider, except that its first calls return replies already given.

    answer_question's first model call is the retrieval router's and its
    second, for lookup and derivation, is the next router's, so they get the
    replies whose confidence was checked; every later call goes to the model.
    """

    def __init__(self, provider: Provider, replies: list[str]):
        self.provider = provider
        self.replies = list(replies)
        self.model = provider.model

    def complete(self, prompt: str, temperature: float = 0.0, schema: dict | None = None) -> str:
        if self.replies:
            return self.replies.pop(0)
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


def check_bar(name: str, value) -> float:
    """value, when it is a confidence above 0 and at most 1, or ConfigError."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
        raise ConfigError(f"intents.yaml: {name} must be a number above 0 and at most 1, got {value!r}")
    return value


def confidence_bars(routing) -> dict[str, dict[str, float]]:
    """Each route's minimum confidence, from intents.yaml's routing section.

    Keyed by step, then route: first_step (lookup, derivation, abstain), then
    derivation and lookup for the second-step routers. A route not listed under
    routing.first_step or routing.second_step gets routing.min_confidence.
    A route or router that does not exist is a ConfigError, so a typo is not
    quietly given the default.
    """
    if not isinstance(routing, dict):
        raise ConfigError("intents.yaml: 'routing' must be a mapping")
    default = check_bar("routing.min_confidence", routing.get("min_confidence"))
    second = routing.get("second_step") or {}
    if not isinstance(second, dict):
        raise ConfigError("intents.yaml: routing.second_step must be a mapping")
    unknown = sorted(set(second) - set(NEXT_STEPS))
    if unknown:
        raise ConfigError(f"intents.yaml: routing.second_step has no {', '.join(unknown)} router; "
                          f"it has {', '.join(NEXT_STEPS)}")

    own = {"first_step": routing.get("first_step") or {}, **{step: second.get(step) or {} for step in NEXT_STEPS}}
    bars = {}
    for step, routes in {"first_step": STEPS, **NEXT_STEPS}.items():
        name = "routing.first_step" if step == "first_step" else f"routing.second_step.{step}"
        if not isinstance(own[step], dict):
            raise ConfigError(f"intents.yaml: {name} must be a mapping")
        unknown = sorted(set(own[step]) - set(routes))
        if unknown:
            raise ConfigError(f"intents.yaml: {name} has no {', '.join(unknown)} route; it has {', '.join(routes)}")
        bars[step] = {
            route: check_bar(f"{name}.{route}", own[step][route]) if route in own[step] else default
            for route in routes
        }
    return bars


def unroutable(
    question: str, records: list[Record], context: QueryContext, settings: Settings, route: str,
    routing_confidence: float | None, second_step_confidence: dict[str, float] | None,
    routed_to: list[str] | None = None, second_step_check: bool = True,
) -> int:
    """Answer that the records do not contain the question, then log and print it.
    second_step_confidence is left out of the answer when the second-step check is off."""
    held = held_about(records, question)
    answer = "The records do not contain this." + (f" {held}" if held else "")
    output = {
        "answer": answer, "citations": [], "records_consulted": [r.run_id for r in records],
        "intent": None, "routing_confidence": routing_confidence,
    }
    if second_step_check:
        output["second_step_confidence"] = second_step_confidence
    output["records_in_scope"] = len(records)
    answer_envelope = envelope(
        "B-1", settings.provider.model, settings.b1.prompt_version, output["records_consulted"],
        "OK", output, None,
    )
    log_answer(question, context, answer_envelope, settings, route, routed_to)
    print(json.dumps(answer_envelope, indent=2))
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ask a question of one tenant's records when the semantic router backs the model's first step.")
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
        return fail(f"{ROUTER_FILE} not found: run python src/router/first_step_router.py first to calibrate")

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
        routing = load_config(PROJECT_ROOT / "config" / "intents.yaml").get("routing")
        bars = confidence_bars(routing)
        second_step_check = routing.get("second_step_check")
        if not isinstance(second_step_check, bool):
            raise ConfigError("intents.yaml: routing.second_step_check must be true or false")
        semantic = load_router(ROUTER_FILE)
        missing = [step for step in STEPS if step not in {r.name for r in semantic.routes}]
        if missing:
            return fail(f"{ROUTER_FILE.name} has no {', '.join(missing)} route: run python src/router/first_step_router.py to refit")
        next_semantic = {}
        if second_step_check:
            refit = "run PYTHONPATH=src python -m router.second_step_router"
            for path in NEXT_ROUTER_FILES.values():
                if not path.exists():
                    return fail(f"{path} not found: {refit} first to calibrate")
            next_semantic = {kind: load_router(path) for kind, path in NEXT_ROUTER_FILES.items()}
            for kind, router in next_semantic.items():
                missing = [step for step in NEXT_STEPS[kind] if step not in {r.name for r in router.routes}]
                if missing:
                    return fail(f"{NEXT_ROUTER_FILES[kind].name} has no {', '.join(missing)} route: {refit} to refit")

        # 1. Load: the tenant's records, once.
        records = resolve(context, settings.tenants, settings.root)
        if not records:
            output = {"answer": ABSTAIN, "citations": [], "records_consulted": [], "intent": None, "records_in_scope": 0}
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

        # 2. Route. The model picks the first step; the semantic router's
        # confidence for that route decides whether it goes ahead.
        calls: list[dict] = []
        try:
            retrieval = classify_question(question, settings.provider, settings.b1, settings.derivation, calls)
        except RetrievalError as error:
            print(f"model router:    no valid reply ({error})")
            print("stopped: no first step")
            return 0
        print(f"model router:    {retrieval.kind}  ({retrieval.reason})")

        routed = normalise(question)
        scores = {c.name: c.confidence for c in semantic(routed, limit=None, threshold=float("-inf"))}
        best = max(scores, key=scores.get)
        print(f"semantic router: best {best} {scores[best]:.2f}; "
              + ", ".join(f"{name} {value:.2f}" for name, value in scores.items()))
        routing_confidence = scores.get(retrieval.kind)
        if retrieval.kind == "catalogue":
            print("catalogue has no semantic route to check: going ahead\n")
        elif routing_confidence < bars["first_step"][retrieval.kind]:
            print(f"unroutable: {retrieval.kind} has confidence {routing_confidence:.2f}, "
                  f"below its minimum {bars['first_step'][retrieval.kind]}")
            return unroutable(
                question, records, context, settings, retrieval.kind, routing_confidence, None,
                second_step_check=second_step_check,
            )
        else:
            print(f"going ahead: {retrieval.kind} has confidence {routing_confidence:.2f} "
                  f"(minimum {bars['first_step'][retrieval.kind]})\n")

        # 3. Second step, for lookup and derivation. The model picks the
        # tables or derivations; the second-step semantic router's confidence
        # for every pick decides whether it goes ahead.
        replies = [calls[-1]["reply"]]
        second_step_confidence = None
        if retrieval.kind in NEXT_STEPS:
            next_calls: list[dict] = []
            try:
                if retrieval.kind == "derivation":
                    route = route_question(
                        question, extended.records, settings.provider, settings.b1, settings.derivation, next_calls,
                    )
                else:
                    route = route_lookup(question, extended.records, settings.provider, settings.b1, next_calls)
            except RouteError as error:
                print(f"model {retrieval.kind} router: no valid reply ({error})")
                print("stopped: no second step")
                return 0
            replies.append(next_calls[-1]["reply"])

            if route.abstain:
                print(f"model {retrieval.kind} router: abstain ({route.reason})"
                      + (": nothing to check, going ahead\n" if second_step_check else "\n"))
            else:
                # A derivation is checked by its type: group_mean_temperature_c as group_mean.
                if retrieval.kind == "derivation":
                    routed_to = route.derivations
                    picks = list(dict.fromkeys(
                        RECORDS if name == RECORDS else settings.derivation.derivations[name]["type"]
                        for name in routed_to
                    ))
                else:
                    routed_to = route.tables
                    picks = list(routed_to)
                print(f"model {retrieval.kind} router: {', '.join(routed_to)}"
                      + ("" if second_step_check else "\n"))

            if not route.abstain and second_step_check:
                scores = {
                    c.name: c.confidence
                    for c in next_semantic[retrieval.kind](routed, limit=None, threshold=float("-inf"))
                }
                print(f"semantic {retrieval.kind} router: "
                      + ", ".join(f"{name} {value:.2f}" for name, value in scores.items()))
                second_step_confidence = {pick: scores[pick] for pick in picks}
                minimum = bars[retrieval.kind]
                weak = [pick for pick, value in second_step_confidence.items() if value < minimum[pick]]
                if weak:
                    print("unroutable: " + ", ".join(
                        f"{pick} has confidence {second_step_confidence[pick]:.2f}, below its minimum {minimum[pick]}"
                        for pick in weak
                    ))
                    return unroutable(
                        question, records, context, settings, retrieval.kind,
                        routing_confidence, second_step_confidence, routed_to,
                    )
                print("going ahead: " + ", ".join(
                    f"{pick} has confidence {value:.2f} (minimum {minimum[pick]})"
                    for pick, value in second_step_confidence.items()
                ) + "\n")

        # 4. Answer, with the model's checked steps replayed.
        provider = Replay(settings.provider, replies)
        result = answer_question(
            question, extended, conn, provider, settings.b1,
            settings.derivation, settings.verification, lookup_conn, settings.catalogue,
        )
        trace = result.trace or {}
        chosen = trace.get("router", {}).get("route")
        routed_to = None
        if chosen and not chosen["abstain"]:
            routed_to = chosen["derivations"] if "derivations" in chosen else chosen["tables"]
        output = result.output
        if output is not None:
            output = {
                **output, "intent": answer_intent(output["answer"], retrieval.kind, routed_to, trace, settings),
                "routing_confidence": routing_confidence,
            }
            if second_step_check:
                output["second_step_confidence"] = second_step_confidence
            output["records_in_scope"] = len(records)
        run_ids = (output or {}).get("records_consulted") or [r.run_id for r in records]
        answer_envelope = envelope(
            "B-1", settings.provider.model, settings.b1.prompt_version, run_ids,
            result.status, output, result.verification,
        )

        # 5. Log, print and write the PDF, as query.ask does.
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
