"""B-3 Action plan: prioritised, grouped actions from a run's findings (section 8.4).

B-3 writes no action. Every finding already carries a recommended_action, and
B-3 carries it verbatim. What it adds:
  category   from action_mapping.yaml, by the finding's feature
  priority   severity rank, then category rank, then route order; ties stay ties
  grouping   one action per feature and action text, never across features
  unmapped   a feature with no configured category is stated, never guessed
Deterministic by default (prose: false): no model call. With prose on, the
model adds one opening sentence and nothing else. The plan is also rendered as
text from b3_action.j2, so every value is a cited slot, and that text is
verified before anything is returned.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace

from agents.b2_narrative import Writer
from agents.envelope import envelope
from agents.provider import Provider, ProviderError
from agents.schema import B3Config, DerivationConfig, ExtendedRecord, Template, VerificationConfig
from agents.slots import fill_slots
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


def plan(
    extended: ExtendedRecord, run_index: int, provider: Provider | None, config: B3Config,
    derivation: DerivationConfig, verification: VerificationConfig,
) -> dict:
    """B-3's action plan for one run, in its envelope. provider is None when prose is off."""
    record = extended.records[run_index]
    r = f"records[{run_index}]"
    w = Writer(extended, "b3_action.j2")
    route = {}
    for cp in reversed(record.checkpoints):     # reversed, so a repeated id keeps its first position
        route[cp.checkpoint_id] = LAST if cp.sequence_number is None else cp.sequence_number

    mapped, unmapped = [], []
    for (feature, text), positions in group(record, config).items():
        checkpoints = at_checkpoints(record, positions, route)
        entry = {
            "positions": positions,
            "checkpoints": checkpoints,
            "first_stop": min((route.get(record.findings[k].checkpoint_id, LAST) for k in checkpoints.values()),
                              default=LAST),
        }
        category = config.features.get(feature)
        if category is not None and text is not None:
            mapped.append({**entry, "feature": feature, "text": text, "category": category})
        else:
            unmapped.append({**entry, "feature": feature, "no_text": category is not None})

    # Priority: a dense rank of (severity, category, route position), so equal keys share one.
    for g in mapped:
        worst = min(g["positions"], key=lambda k: severity_rank(record.findings[k].severity, config))
        g["worst"] = worst
        g["key"] = (severity_rank(record.findings[worst].severity, config),
                    config.category_rank[g["category"]], g["first_stop"])
    ranks = {key: n + 1 for n, key in enumerate(sorted({g["key"] for g in mapped}))}
    mapped.sort(key=lambda g: (g["key"], g["feature"], g["text"]))
    unmapped.sort(key=lambda g: (g["first_stop"], str(g["feature"])))

    actions, missing, lines = [], [], []
    for g in mapped:
        ids = [record.findings[k].finding_id for k in g["positions"]]
        actions.append({
            "action": g["text"], "referencing_findings": ids, "priority": ranks[g["key"]],
            "category": g["category"], "finding_count": len(ids), "checkpoints": list(g["checkpoints"]),
        })
        lines.append(w.say(
            "action", category=g["category"].replace("_", " "),
            severity=w.slot(f"{r}.findings[{g['worst']}].severity"),
            text=w.slot(f"{r}.findings[{g['positions'][0]}].recommended_action"),
            checkpoints=[w.slot(f"{r}.findings[{k}].checkpoint_id") for k in g["checkpoints"].values()],
            findings=[w.slot(f"{r}.findings[{k}].finding_id") for k in g["positions"]],
        ))
    for g in unmapped:
        feature = g["feature"] or "not recorded"
        statement = (f"No recommended action is recorded for finding type {feature}." if g["no_text"]
                     else f"No action is configured for finding type {feature}.")
        missing.append({
            "feature": g["feature"], "finding_count": len(g["positions"]),
            "checkpoints": list(g["checkpoints"]), "statement": statement,
        })
        lines.append(w.say(
            "no_recorded_action" if g["no_text"] else "unmapped",
            feature=w.slot(f"{r}.findings[{g['positions'][0]}].feature"),
            checkpoints=[w.slot(f"{r}.findings[{k}].checkpoint_id") for k in g["checkpoints"].values()],
        ))

    if not record.findings:
        lines = [w.say("no_findings", run=w.slot(f"{r}.run_id"))]
    elif actions:
        lines.insert(0, w.say("heading"))

    how = {"model": None, "method": "deterministic", "attempts": 0, "status": "OK"}
    if config.prose and provider is not None and actions:
        categories = sorted({a["category"].replace("_", " ") for a in actions})
        sentence, attempts, reason = opening(categories, provider, config)
        if sentence is None:
            log.warning("B-3 plan keeps no opening sentence: %s", reason)
            how.update(model=provider.model, status="DEGRADED_TEMPLATE_ONLY")
        else:
            lines.insert(0, sentence)
            how.update(model=provider.model, method="template_slot_fill", attempts=attempts - 1)

    filled = fill_slots(Template("b3_action", "\n".join(lines), w.slots), extended, derivation)
    result = verify_numeric(filled.text, extended, verification)
    result = replace(result, method=how["method"], regeneration_attempts=how["attempts"], derived_values_used=[])
    if not result.passed:
        log.warning("B-3 plan failed verification: %s", result.failures)
        return envelope(AGENT, how["model"], config.prompt_version, [record.run_id],
                        "REFUSED_UNVERIFIABLE", None, result)

    output = {
        "actions": actions,
        "unmapped": missing,
        "text": filled.text,
        "citations": [{"claim_span": list(s.span), "source_field": s.source_field} for s in filled.slots],
    }
    return envelope(AGENT, how["model"], config.prompt_version, [record.run_id], how["status"], output, result)


def group(record, config: B3Config) -> dict[tuple, list[int]]:
    """(feature, action text) -> finding positions, in record order.

    A mapped feature groups by its action text too, so two different texts are
    never merged. An unmapped feature groups by feature alone (text None). A
    finding id listed twice counts once.
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


def at_checkpoints(record, positions: list[int], route: dict) -> dict[str, int]:
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
        tokens = extract_tokens(sentence, word_numbers=True)
        if not sentence or len(sentence) > MAX_SENTENCE_CHARS:
            problem = f"the reply must be one sentence under {MAX_SENTENCE_CHARS} characters"
        elif found:
            problem = f'the sentence contains "{found.group()}"'
        elif tokens:
            problem = "the sentence contains a number or a name"
        else:
            return sentence, attempt, None
        log.warning("B-3 attempt %d rejected: %s", attempt, problem)
        feedback = f"\nYOUR PREVIOUS REPLY WAS REJECTED: {problem}. Reply again.\n"
    return None, config.max_attempts, f"no usable sentence after {config.max_attempts} attempts; last: {feedback.strip()}"
