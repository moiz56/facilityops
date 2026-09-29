"""B-2 Narrative: the prose inside the report for one run (section 8.3).

Four sections, each written from its template in templates/:
  executive_summary  one fact per sentence; the model only sets their order
  coverage           declared counts beside computed ones, then what was missed
  section_intro      one per zone
  item_note          one per checkpoint
Code writes every sentence from a template macro, and every value goes in as a
slot, so fill_slots records its span and the citations are exact. The model
never sees a record value: it is told what each fact is about, and returns the
order a facility manager should read them in. Every section is verified before
it is returned.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from agents.envelope import envelope
from agents.provider import Provider, ProviderError
from agents.schema import B2Config, DerivationConfig, ExtendedRecord, Template, VerificationConfig
from agents.slots import fill_slots, resolve
from agents.verification import verify_numeric

log = logging.getLogger(__name__)

AGENT = "B-2"
TEMPLATES = Path(__file__).parent / "templates"

# What each executive-summary fact is about. The model sees this, never the fact.
KINDS = {
    "run_outcome": "when the run happened, its run status and its final verdict",
    "coverage": "how many of the required checkpoints were completed",
    "failed": "the checkpoints whose verdict was FAIL",
    "warned": "the checkpoints whose verdict was WARN",
    "missed": "the checkpoints the robot did not reach",
    "count_mismatch": "the robot's declared counts disagree with its own lists",
    "findings": "the findings recorded on the run",
    "no_findings": "no findings were recorded on the run",
    "review": "findings the detector abstained on, which need human review",
    "alerts": "how many critical and warning sensor alerts were logged",
    "excluded": "sensor blocks whose values were left out, and why",
    "stale": "readings that were stale when taken",
    "top_reading": "the highest checkpoint reading of a sensor field",
}

# The coverage rows (report engine section 3.3): label, declared field, and
# what the computed count counts. None counts every item: the population.
ROWS = (
    ("Required checkpoints", "total_required_checkpoints", "checkpoints", "status", None),
    ("Completed checkpoints", "total_completed_checkpoints", "checkpoints", "status", "COMPLETED"),
    ("Passed checkpoints", "passed_checkpoints", "checkpoints", "result_status", "PASS"),
    ("Failed checkpoints", "failed_checkpoints", "checkpoints", "result_status", "FAIL"),
    ("Missed checkpoints", "missed_checkpoints", "checkpoints", "status", "MISSED"),
    ("Warned checkpoints", "warned_checkpoints", "checkpoints", "result_status", "WARN"),
    ("Findings", "finding_count", "findings", "status", None),
)

PROMPT = """You set the reading order of an inspection run's executive summary. A
facility manager reads it first thing: put first what needs their attention,
problems before routine facts. Code writes every sentence; you only order them.
You see what each fact is about, never its content.

THE FACTS
{facts}
{feedback}
REPLY
JSON only, no other text, with every fact id exactly once:
{"order": ["<fact id>", ...]}
"""


class Writer:
    """Writes sentences from one template file, registering each value as a slot."""

    def __init__(self, extended: ExtendedRecord, template_file: str):
        env = Environment(loader=FileSystemLoader(TEMPLATES), undefined=StrictUndefined, autoescape=False)
        self.macros = env.get_template(template_file).module
        self.extended = extended
        self.slots: dict[str, str] = {}

    def slot(self, path: str) -> str:
        """A placeholder for the value at path, or "not recorded" when there is none."""
        value, _, _ = resolve(path, self.extended)
        if value is None or value == "" or value == [] or value == ():
            return "not recorded"
        slot_id = f"s{len(self.slots)}"
        self.slots[slot_id] = path
        return "{{ " + slot_id + " }}"

    def say(self, macro: str, **args) -> str:
        return str(getattr(self.macros, macro)(**args)).strip()


def narrate(
    extended: ExtendedRecord, run_index: int, provider: Provider | None, config: B2Config,
    derivation: DerivationConfig, verification: VerificationConfig,
) -> dict:
    """Every B-2 section for one run, each in its envelope.

    Returns {"executive_summary": envelope, "coverage": envelope,
    "section_intro": {zone: envelope}, "item_note": [envelope per checkpoint]}.
    provider is None when prose is off: the summary keeps code's order.
    """
    record = extended.records[run_index]

    def seal(section: str, writer: Writer, text: str, **how) -> dict:
        return sealed(section, writer, text, record.run_id, extended, config, derivation, verification, **how)

    return {
        "executive_summary": executive_summary(record, run_index, extended, provider, config, derivation, verification),
        "coverage": seal("coverage", *coverage(record, run_index, extended, derivation)),
        "section_intro": {
            zone: seal("section_intro", w, text)
            for zone, (w, text) in section_intros(record, run_index, extended, derivation).items()
        },
        "item_note": [
            seal("item_note", *item_note(record, run_index, j, extended)) for j in range(len(record.checkpoints))
        ],
    }


def sealed(
    section: str, writer: Writer, text: str, run_id: str, extended: ExtendedRecord, config: B2Config,
    derivation: DerivationConfig, verification: VerificationConfig,
    model: str | None = None, method: str = "deterministic", attempts: int = 0, status: str = "OK",
) -> dict:
    """Fill, verify and wrap one section. Text that fails verification is never returned."""
    filled = fill_slots(Template(section, text, writer.slots), extended, derivation)
    result = verify_numeric(filled.text, extended, verification)
    paths = [slot.source_field for slot in filled.slots]
    # The slots say which derivation each value came from.
    derived = [m.group(1) for p in paths if (m := re.match(r"derived\.values\.(\w+)", p))]
    result = replace(result, method=method, regeneration_attempts=attempts,
                     derived_values_used=list(dict.fromkeys(derived)))
    if not result.passed:
        log.warning("B-2 %s failed verification: %s", section, result.failures)
        return envelope(AGENT, model, config.prompt_version, [run_id], "REFUSED_UNVERIFIABLE", None, result)

    output = {
        "text": filled.text,
        "citations": [{"claim_span": list(s.span), "source_field": s.source_field} for s in filled.slots],
        "section": section,
    }
    return envelope(AGENT, model, config.prompt_version, [run_id], status, output, result)


# Executive summary

def executive_summary(
    record, i: int, extended: ExtendedRecord, provider: Provider | None, config: B2Config,
    derivation: DerivationConfig, verification: VerificationConfig,
) -> dict:
    """The facts in the model's order, or code's order when prose is off or the model fails."""
    w = Writer(extended, "executive_summary.j2")
    facts = summary_facts(w, record, i, extended, derivation)
    order, how = list(range(len(facts))), {}

    if config.prose and provider is not None:
        chosen, attempts, reason = order_facts(facts, provider, config)
        if chosen is None:
            log.warning("B-2 executive summary uses code's order: %s", reason)
            how = {"model": provider.model, "status": "DEGRADED_TEMPLATE_ONLY"}
        else:
            order = chosen
            how = {"model": provider.model, "method": "template_slot_fill", "attempts": attempts - 1}

    text = " ".join(facts[n][1] for n in order)
    return sealed("executive_summary", w, text, record.run_id, extended, config, derivation, verification, **how)


def summary_facts(w: Writer, record, i: int, extended: ExtendedRecord, config: DerivationConfig) -> list[tuple]:
    """(kind, sentence, nothing_to_report) per fact, in code's default order."""
    r = f"records[{i}]"
    run_id = record.run_id
    facts = [("run_outcome", w.say(
        "run_outcome", run=w.slot(f"{r}.run_id"), facility=w.slot(f"{r}.facility_name"),
        start=w.slot(f"{r}.start_time"), end=w.slot(f"{r}.end_time"),
        run_status=w.slot(f"{r}.run_status"), final_status=w.slot(f"{r}.final_status"),
    ), False)]

    found = count(extended, config, run_id, "checkpoints", "status", "COMPLETED")
    if found:
        path, _ = found
        facts.append(("coverage", w.say(
            "coverage", completed=w.slot(f"{path}.count"), required=w.slot(f"{path}.population"),
        ), False))

    for kind, label, field, equals in (
        ("failed", "Checkpoints with result FAIL", "result_status", "FAIL"),
        ("warned", "Checkpoints with result WARN", "result_status", "WARN"),
        ("missed", "Checkpoints missed", "status", "MISSED"),
    ):
        found = count(extended, config, run_id, "checkpoints", field, equals)
        if found:
            path, out = found
            ids = [w.slot(f"{path}.matching_ids[{p}]") for p in range(len(out["matching_ids"]))]
            facts.append((kind, w.say("tally", label=label, n=w.slot(f"{path}.count"), ids=ids), out["count"] == 0))

    mismatched = [label.lower() for label, declared, _, computed in reconcile(record, extended, config)
                  if computed is not None and getattr(record, declared) != computed[1]]
    if mismatched:
        facts.append(("count_mismatch", w.say("count_mismatch", labels=mismatched), False))

    findings = [finding(w, r, k) for k in range(len(record.findings))]
    if findings:
        facts.append(("findings", w.say("findings", items=findings), False))
    else:
        facts.append(("no_findings", w.say("no_findings", run=w.slot(f"{r}.run_id")), True))
    review = [finding(w, r, k) for k, f in enumerate(record.findings) if f.status == "abstained"]
    if review:
        facts.append(("review", w.say("review", items=review), False))

    critical = count(extended, config, run_id, "sensor_alerts", "severity", "critical")
    warning = count(extended, config, run_id, "sensor_alerts", "severity", "warning")
    if critical and warning:
        facts.append(("alerts", w.say(
            "alerts", critical=w.slot(f"{critical[0]}.count"), warning=w.slot(f"{warning[0]}.count"),
        ), critical[1]["count"] == 0 and warning[1]["count"] == 0))

    excluded = [
        w.say("exclusion", block=w.slot(f"derived.excluded.{run_id}[{k}].block"),
              scope=None if e["scope"] == "__run__" else w.slot(f"derived.excluded.{run_id}[{k}].scope"),
              reason=w.slot(f"derived.excluded.{run_id}[{k}].reason"))
        for k, e in enumerate(extended.derived.excluded[run_id])
    ]
    if excluded:
        facts.append(("excluded", w.say("excluded", items=excluded), False))
    stale = [
        w.say("stale_item", block=w.slot(f"derived.stale[{k}].block"), scope=w.slot(f"derived.stale[{k}].scope"))
        for k, e in enumerate(extended.derived.stale) if e["run_id"] == run_id
    ]
    if stale:
        facts.append(("stale", w.say("stale", items=stale), False))

    for name, entry in config.derivations.items():
        found = derived_for(extended, name, run_id) if entry.get("type") == "rank_top_n" else None
        if found and found[1].get("status") == "OK" and found[1].get("ranking"):
            path, _ = found
            facts.append(("top_reading", w.say(
                "top_reading", field=w.slot(f"{path}.field_path"),
                value=w.slot(f"{path}.ranking[0].value"), checkpoint=w.slot(f"{path}.ranking[0].id"),
            ), False))
    return facts


def finding(w: Writer, r: str, k: int) -> str:
    f = f"{r}.findings[{k}]"
    return w.say("finding", feature=w.slot(f"{f}.feature"), checkpoint=w.slot(f"{f}.checkpoint_id"),
                 severity=w.slot(f"{f}.severity"), status=w.slot(f"{f}.status"))


def order_facts(facts: list[tuple], provider: Provider, config: B2Config) -> tuple[list[int] | None, int, str | None]:
    """The model's reading order, as indexes into facts: (order or None, calls made, why not)."""
    feedback = ""
    for attempt in range(1, config.max_attempts + 1):
        try:
            reply = provider.complete(build_prompt(facts, feedback), temperature=config.temperature,
                                      schema=order_schema(len(facts)))
        except ProviderError as error:
            return None, attempt, str(error)
        try:
            return parse_order(reply, len(facts)), attempt, None
        except ValueError as error:
            log.warning("B-2 attempt %d rejected: %s", attempt, error)
            feedback = f"\nYOUR PREVIOUS REPLY WAS REJECTED: {error}\nFix that and reply again.\n"
    return None, config.max_attempts, f"no valid order after {config.max_attempts} attempts; last: {feedback.strip()}"


def build_prompt(facts: list[tuple], feedback: str) -> str:
    lines = [f"- f{n}: {KINDS[kind]}{' (nothing to report)' if empty else ''}"
             for n, (kind, _, empty) in enumerate(facts)]
    values = {"facts": "\n".join(lines), "feedback": feedback}
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), PROMPT)


def order_schema(n: int) -> dict:
    """The JSON shape the provider holds the reply to: {"order": [fact ids]}."""
    ids = [f"f{k}" for k in range(n)]
    return {
        "type": "object",
        "properties": {"order": {"type": "array", "items": {"type": "string", "enum": ids}}},
        "required": ["order"],
        "additionalProperties": False,
    }


def parse_order(reply: str, n: int) -> list[int]:
    """Fact indexes from {"order": ["f2", "f0", ...]}. Raises ValueError unless every id is there once."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.strip())
    try:
        order = json.loads(text).get("order")
    except (json.JSONDecodeError, AttributeError):
        raise ValueError('the reply must be one JSON object with "order"') from None
    ids = [f"f{k}" for k in range(n)]
    if not isinstance(order, list) or sorted(order, key=str) != sorted(ids):
        raise ValueError(f'"order" must list each of {", ".join(ids)} exactly once')
    return [ids.index(fact) for fact in order]


# Coverage

def coverage(record, i: int, extended: ExtendedRecord, config: DerivationConfig) -> tuple[Writer, str]:
    """Declared beside computed for each row, then missed checkpoints and those with no evidence."""
    w = Writer(extended, "coverage_statement.j2")
    r = f"records[{i}]"
    lines = []
    if not record.checkpoints:
        lines.append(w.say("no_checkpoints", run=w.slot(f"{r}.run_id")))

    for label, declared, scope, computed in reconcile(record, extended, config):
        stated = w.slot(f"{r}.{declared}")
        if computed is None:
            lines.append(w.say("row_uncomputed", label=label, declared=stated))
        elif getattr(record, declared) == computed[1]:
            lines.append(w.say("row_agree", label=label, declared=stated, computed=w.slot(computed[0])))
        else:
            source = "the findings list" if scope == "findings" else "the checkpoint list"
            lines.append(w.say("row_disagree", label=label, declared=stated, computed=w.slot(computed[0]),
                               source=source))

    missed = [
        w.say("missed_item", checkpoint=w.slot(f"{r}.checkpoints[{j}].checkpoint_id"),
              reason=w.slot(f"{r}.checkpoints[{j}].missed_reason"))
        for j, cp in enumerate(record.checkpoints) if cp.status == "MISSED"
    ]
    if missed:
        lines.append(w.say("missed", items=missed))
    bare = [
        w.say("no_evidence_item", checkpoint=w.slot(f"{r}.checkpoints[{j}].checkpoint_id"),
              observed=w.slot(f"{r}.checkpoints[{j}].observed"))
        for j, cp in enumerate(record.checkpoints) if cp.status == "COMPLETED" and not cp.evidence_images
    ]
    if bare:
        lines.append(w.say("no_evidence", items=bare))
    return w, "\n".join(lines)


def reconcile(record, extended: ExtendedRecord, config: DerivationConfig) -> list[tuple]:
    """(label, declared field, scope, (computed path, computed value) or None) per coverage row."""
    rows = []
    for label, declared, scope, field, equals in ROWS:
        found = count(extended, config, record.run_id, scope, field, equals)
        key = "population" if equals is None else "count"
        rows.append((label, declared, scope, (f"{found[0]}.{key}", found[1][key]) if found else None))
    return rows


# Section intros and item notes

def section_intros(record, i: int, extended: ExtendedRecord, config: DerivationConfig) -> dict[str, tuple]:
    """zone -> (writer, text): which checkpoints it covers, then its telemetry means."""
    r = f"records[{i}]"
    zones: dict[str, list[int]] = {}
    for j, cp in enumerate(record.checkpoints):
        zones.setdefault(cp.zone, []).append(j)

    intros = {}
    for zone, positions in zones.items():
        w = Writer(extended, "section_intro.j2")
        sentences = [w.say("intro", zone=w.slot(f"{r}.checkpoints[{positions[0]}].zone"),
                           checkpoints=[w.slot(f"{r}.checkpoints[{j}].checkpoint_id") for j in positions])]
        means = []
        for name, entry in config.derivations.items():
            if entry.get("type") != "group_mean" or entry.get("group_by") != "zone":
                continue
            found = derived_for(extended, name, record.run_id)
            group = found[1].get("groups", {}).get(zone) if found else None
            if group is None:
                continue
            path = found[0]
            field = w.slot(f"{path}.field_path")
            if group.get("status") == "OK":
                means.append(w.say("mean", field=field, value=w.slot(f"{path}.groups.{zone}.mean")))
            else:
                means.append(w.say("not_computable", field=field))
        if means:
            sentences.append(w.say("means", items=means))
        intros[zone] = (w, " ".join(sentences))
    return intros


def item_note(record, i: int, j: int, extended: ExtendedRecord) -> tuple[Writer, str]:
    """One sentence on one checkpoint: why it was missed, or its result and findings."""
    w = Writer(extended, "item_note.j2")
    r = f"records[{i}]"
    c = f"{r}.checkpoints[{j}]"
    cp = record.checkpoints[j]
    if cp.status == "MISSED":
        return w, w.say("missed", checkpoint=w.slot(f"{c}.checkpoint_id"), status=w.slot(f"{c}.status"),
                        reason=w.slot(f"{c}.missed_reason"))
    findings = [
        w.say("finding", feature=w.slot(f"{r}.findings[{k}].feature"),
              severity=w.slot(f"{r}.findings[{k}].severity"), status=w.slot(f"{r}.findings[{k}].status"))
        for k, f in enumerate(record.findings) if f.checkpoint_id == cp.checkpoint_id
    ]
    return w, w.say("visited", checkpoint=w.slot(f"{c}.checkpoint_id"), status=w.slot(f"{c}.status"),
                    result=w.slot(f"{c}.result_status"), observed=w.slot(f"{c}.observed"), findings=findings)


# Finding derived values

def derived_for(extended: ExtendedRecord, name: str, run_id: str) -> tuple[str, dict] | None:
    """(path, output) of a per-run derivation for this run, or None."""
    output = extended.derived.values.get(name)
    if isinstance(output, list):
        for k, out in enumerate(output):
            if out.get("run_id") == run_id:
                return f"derived.values.{name}[{k}]", out
    return None


def count(
    extended: ExtendedRecord, config: DerivationConfig, run_id: str, scope: str, field: str, equals: str | None,
) -> tuple[str, dict] | None:
    """The configured condition_count over scope whose condition tests field (== equals, if given), for this run.

    Found by what it counts, not by its name, so renaming an instance changes nothing.
    """
    for name, entry in config.derivations.items():
        condition = config.conditions.get(entry.get("condition"), {})
        if (entry.get("type") == "condition_count" and entry.get("scope") == scope
                and condition.get("field") == field and (equals is None or condition.get("equals") == equals)):
            return derived_for(extended, name, run_id)
    return None
