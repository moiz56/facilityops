"""B-3 Action plan: prioritised, grouped actions from one run's findings (section 8.4).

B-3 writes no action. Every finding already carries a recommended_action, and
B-3 carries it verbatim. What it adds:
  category   from action_mapping.yaml, by the finding's feature
  priority   severity rank, then category rank, then route order; equal keys share one
  grouping   one action per feature and action text, never across features
  unmapped   a feature with no configured category is stated, never guessed
Called for one run, like B-2, and derived from that run alone. Deterministic by
default (prose: false): no model call. With prose on, the model adds one
opening sentence and nothing else. The plan is also rendered as text from
templates/b3_action.j2, with every value a cited slot; the plan's own numbers
(priority, category, finding counts) go into the run's extended record under
derived.run_values.<run_id>.plan so they can be cited, and the text is verified
before anything is returned.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace

from common.paths import ConfigError
from common.schema import Record
from agents.b2_narrative import Writer, join_words
from agents.derivation import extend_record
from agents.envelope import envelope
from agents.provider import Provider, ProviderError, make_provider
from agents.schema import B3Config, DerivationConfig, ExtendedRecord, Template, VerificationConfig
from agents.slots import fill_slots
from agents.utils import b3_config, derivation_config, hash_configs, provider_config, verification_config
from agents.verification import extract_tokens, verify_numeric

log = logging.getLogger(__name__)

AGENT = "B-3"
LAST = float("inf")     # route position of a checkpoint the run does not list

PROMPT = """Write one plain, factual sentence that opens a facility's action plan.
The plan covers these kinds of action: {categories}.
No digits, no number words, no names of checkpoints, zones or findings, no
superlatives, and no judgement of whether anything got better or worse.
Reply with the sentence only.
{feedback}"""

# Words the opening sentence may not use (section 8.1). Numbers and names are
# caught by the verifier's own extractor.
FORBIDDEN = re.compile(r"\b(improv\w*|wors\w*|better|degrad\w*|deteriorat\w*|ris(?:e|es|en|ing))\b", re.IGNORECASE)
MAX_SENTENCE_CHARS = 300


def run_plan(
    record: Record, report: dict, derivations: dict, agents: dict, mapping: dict,
) -> tuple[ExtendedRecord, dict | None]:
    """B-3's action plan for one run: (extended record with the plan's values, envelope).

    Takes the parsed report.yaml, b2_derivations.yaml (the per-run derivation
    settings B-2 also uses), agents.yaml and action_mapping.yaml. The envelope
    is None when B-3 is disabled in agents.yaml.
    """
    config = derivation_config(report, derivations, hash_configs(report, derivations, mapping))
    verification = verification_config(agents, report, derivations)
    b3 = b3_config(agents, mapping)
    extended = extend_record([record], config)
    if not b3.enabled:
        return extended, None

    provider = None
    if b3.prose:
        try:
            provider = make_provider(provider_config(agents))
        except ConfigError as error:
            log.warning("B-3 has no provider: %s", error)
    return plan(extended, 0, provider, b3, config, verification)


def plan(
    extended: ExtendedRecord, run_index: int, provider: Provider | None, config: B3Config,
    derivation: DerivationConfig, verification: VerificationConfig,
) -> tuple[ExtendedRecord, dict]:
    """B-3's action plan for records[run_index], in its envelope.

    Returns (the extended record with the plan's values added, envelope).
    provider is None when prose is off, or when no provider could be made.
    """
    record = extended.records[run_index]
    route = {}
    for cp in reversed(record.checkpoints):     # reversed, so a repeated id keeps its first position
        route[cp.checkpoint_id] = LAST if cp.sequence_number is None else cp.sequence_number

    mapped, unmapped = [], []
    for (feature, text), positions in group(record, config).items():
        checkpoints = at_checkpoints(record, positions, route)
        entry = {
            "feature": feature, "positions": positions, "checkpoints": checkpoints,
            "first_stop": min((route.get(cid, LAST) for cid in checkpoints), default=LAST),
        }
        category = config.features.get(feature)
        if category is not None and text is not None:
            mapped.append({**entry, "text": text, "category": category})
        else:
            unmapped.append({**entry, "no_text": category is not None})

    # Priority: a dense rank of (severity, category, first route position), so equal keys share one.
    for g in mapped:
        g["worst"] = min(g["positions"], key=lambda k: severity_rank(record.findings[k].severity, config))
        g["key"] = (severity_rank(record.findings[g["worst"]].severity, config),
                    config.category_rank[g["category"]], g["first_stop"])
    ranks = {key: n + 1 for n, key in enumerate(sorted({g["key"] for g in mapped}))}
    mapped.sort(key=lambda g: (g["key"], g["feature"], g["text"]))
    unmapped.sort(key=lambda g: (g["first_stop"], str(g["feature"])))

    actions = [{
        "action": g["text"],
        "referencing_findings": [record.findings[k].finding_id for k in g["positions"]],
        "priority": ranks[g["key"]],
        "category": g["category"],
        "finding_count": len(g["positions"]),
        "checkpoints": list(g["checkpoints"]),
    } for g in mapped]
    missing = [{
        "feature": g["feature"],
        "finding_count": len(g["positions"]),
        "checkpoints": list(g["checkpoints"]),
        "statement": (f"No recommended action is recorded for finding type {g['feature']}." if g["no_text"]
                      else f"No action is configured for finding type {g['feature']}."),
    } for g in unmapped]

    # The plan's own numbers, where a slot can cite them.
    values = {
        "actions": [{"priority": a["priority"], "category": a["category"], "finding_count": a["finding_count"]}
                    for a in actions],
        "unmapped": [{"finding_count": u["finding_count"]} for u in missing],
    }
    run_values = {**extended.derived.run_values,
                  record.run_id: {**extended.derived.run_values.get(record.run_id, {}), "plan": values}}
    extended = replace(extended, derived=replace(extended.derived, run_values=run_values))

    w = Writer(extended, "b3_action.j2")
    r = f"records[{run_index}]"
    p = f"derived.run_values.{record.run_id}.plan"

    def at(g: dict) -> str:
        return join_words([w.slot(f"{r}.findings[{k}].checkpoint_id") for k in g["checkpoints"].values()])

    lines = []
    if not record.findings:
        lines.append(w.say("no_findings", run=w.slot(f"{r}.run_id")))
    if mapped:
        lines.append(w.say("heading"))
        lines += [w.say(
            "action", priority=w.slot(f"{p}.actions[{k}].priority"), category=w.slot(f"{p}.actions[{k}].category"),
            severity=w.slot(f"{r}.findings[{g['worst']}].severity"),
            text=w.slot(f"{r}.findings[{g['positions'][0]}].recommended_action"),
            count=w.slot(f"{p}.actions[{k}].finding_count", capital=True), count_n=len(g["positions"]),
            checkpoints=at(g),
        ) for k, g in enumerate(mapped)]
    if unmapped:
        lines.append(w.say("unmapped_heading"))
        lines += [w.say(
            "no_recorded_action" if g["no_text"] else "unmapped",
            feature=w.slot(f"{r}.findings[{g['positions'][0]}].feature") or "not recorded",
            count=w.slot(f"{p}.unmapped[{k}].finding_count", capital=True), count_n=len(g["positions"]),
            checkpoints=at(g),
        ) for k, g in enumerate(unmapped)]

    model, method, attempts, status = None, "deterministic", 0, "OK"
    if config.prose and actions:
        if provider is None:
            log.warning("B-3 plan keeps no opening sentence: no provider")
            status = "DEGRADED_TEMPLATE_ONLY"
        else:
            sentence, calls, reason = opening(sorted({a["category"].replace("_", " ") for a in actions}), provider, config)
            model, attempts = provider.model, calls - 1
            if sentence is None:
                log.warning("B-3 plan keeps no opening sentence: %s", reason)
                status = "DEGRADED_TEMPLATE_ONLY"
            else:
                lines.insert(0, sentence)
                method = "template_slot_fill"

    filled = fill_slots(Template("b3_action", "\n".join(lines), w.slots), extended, derivation)
    result = verify_numeric(filled.text, extended, verification, filled.slots)
    result = replace(result, method=method, regeneration_attempts=attempts, derived_values_used=[])
    if not result.passed:
        log.warning("B-3 plan failed verification: %s", result.failures)
        return extended, envelope(AGENT, model, config.prompt_version, [record.run_id],
                                  "REFUSED_UNVERIFIABLE", None, result)

    output = {
        "actions": actions,
        "unmapped": missing,
        "text": filled.text,
        "citations": [{"claim_span": list(s.span), "source_field": s.source_field} for s in filled.slots],
    }
    return extended, envelope(AGENT, model, config.prompt_version, [record.run_id], status, output, result)


def group(record: Record, config: B3Config) -> dict[tuple, list[int]]:
    """(feature, action text) -> finding positions, in record order.

    A mapped feature groups by its action text too, so two different texts are
    never merged into one action. An unmapped feature groups by feature alone
    (text None). A finding id listed twice counts once.
    """
    seen, groups = set(), {}
    for k, f in enumerate(record.findings):
        if f.finding_id in seen:
            continue
        seen.add(f.finding_id)
        text = f.recommended_action or None
        key = (f.feature, text if f.feature in config.features else None)
        groups.setdefault(key, []).append(k)
    return groups


def at_checkpoints(record: Record, positions: list[int], route: dict) -> dict[str, int]:
    """checkpoint_id -> the first of these findings at it, in route order."""
    first = {}
    for k in positions:
        first.setdefault(record.findings[k].checkpoint_id, k)
    return dict(sorted(first.items(), key=lambda item: (route.get(item[0], LAST), item[1])))


def severity_rank(severity: str | None, config: B3Config) -> int:
    """fail 1, warning 2, info 3; a severity the mapping does not know ranks after all of them."""
    return config.severity_rank.get(severity, len(config.severity_rank) + 1)


def opening(categories: list[str], provider: Provider, config: B3Config) -> tuple[str | None, int, str | None]:
    """The model's opening sentence: (sentence or None, calls made, why not)."""
    feedback = ""
    for attempt in range(1, config.max_attempts + 1):
        values = {"categories": ", ".join(categories), "feedback": feedback}
        prompt = re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), PROMPT)
        try:
            reply = provider.complete(prompt, temperature=config.temperature)
        except ProviderError as error:
            return None, attempt, str(error)
        sentence = " ".join(reply.split())
        found = FORBIDDEN.search(sentence)
        if not sentence or len(sentence) > MAX_SENTENCE_CHARS:
            problem = f"the reply must be one sentence under {MAX_SENTENCE_CHARS} characters"
        elif found:
            problem = f'the sentence contains "{found.group()}"'
        elif extract_tokens(sentence, word_numbers=True):
            problem = "the sentence contains a number or a name"
        else:
            return sentence, attempt, None
        log.warning("B-3 attempt %d rejected: %s", attempt, problem)
        feedback = f"\nYOUR PREVIOUS REPLY WAS REJECTED: {problem}. Reply again.\n"
    return None, config.max_attempts, f"no usable sentence after {config.max_attempts} attempts; last: {feedback.strip()}"
