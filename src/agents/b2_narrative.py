"""B-2 Narrative: the prose inside the report for one run (section 8.3).

B-2 is called for one run. Before any template is filled, run_values_for works
out the values its prose states that no derivation gives (counts, alerts,
offline devices, evidence, formatted times), and add_run_values puts them in
the extended record under derived.run_values.<run_id>. From there a slot cites
and the verifier checks them like any other field.

executive_summary: code writes every paragraph from a macro in
templates/executive_summary.j2, every value going in as a slot. The model only
sets the paragraph order (section 1.1: "salience and ordering"); it sees what
each paragraph is about, never its text. The filled text is verified before it
is returned.

section_intros: one or two sentences per zone, then one line per zone mean,
from templates/section_intro.j2. item_notes: one note per checkpoint, from
templates/item_note.j2. coverage: one statement per run, from
templates/coverage_statement.j2; it holds what the summary only points to (the
record's contradictions, missing sensor data, evidence). None of the three makes
a model call: deterministic.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from common.paths import ConfigError, parse_filename, resolve_checkpoint_images, units
from common.schema import Checkpoint, Record
from agents.derivation import compute_eligibility, extend_record
from agents.envelope import envelope
from agents.provider import Provider, ProviderError, make_provider
from agents.schema import B2Config, DerivationConfig, Eligibility, ExtendedRecord, Template, VerificationConfig
from agents.slots import CAP_WORDS, ORDINAL, WORDS, fill_slots, resolve
from agents.utils import (
    b2_config, derivation_config, for_run, format_date, hash_configs, provider_config, verification_config,
)
from agents.verification import verify_numeric

log = logging.getLogger(__name__)

AGENT = "B-2"
TEMPLATES = Path(__file__).parent / "templates"
MISSED = "MISSED"
CLOCK = "%H:%M"     # start time as the summary prints it, e.g. 14:41

# How the verdict paragraph words run_status; any other status is quoted.
RUN_STATUS = {"COMPLETED": "completed", "ABORTED": "was aborted", "RUNNING": "was still running"}

# Declared count -> its computed key in run_values, what it counts, and where
# the computed count is held. warned_checkpoints has its own sentence.
COUNT_PAIRS = (
    ("total_required_checkpoints", "required", "required checkpoints", "checkpoint list"),
    ("total_completed_checkpoints", "completed", "completed checkpoints", "checkpoint list"),
    ("passed_checkpoints", "passed", "passed checkpoints", "checkpoint list"),
    ("failed_checkpoints", "failed", "failed checkpoints", "checkpoint list"),
    ("missed_checkpoints", "missed", "missed checkpoints", "checkpoint list"),
    ("finding_count", "findings", "findings", "findings list"),
)

# What each paragraph is about. The model sees this, never the paragraph.
PARAGRAPHS = {
    "verdict": "when the run took place, how many required checkpoints it reached, and its overall result",
    "empty": "the run recorded no checkpoints",
    "results": "how many checkpoints passed and failed, and what was logged against each failed one",
    "contradiction": "a pointer: the record's declared counts disagree with its own data (detail in the coverage statement)",
    "gap": "a pointer: sensor data that was not recorded on this run (detail in the coverage statement)",
    "measurement": "the zone with the highest mean reading of a sensor field",
}

PROMPT = """You set the paragraph order of an inspection run's executive summary.
A facility manager reads it first thing. Start with the run's verdict, then
put what needs their attention (failures, contradictions in the record,
missing data) before routine facts (evidence captured, measurements). Code
writes every paragraph; you only order them. You see what each paragraph is
about, never its content.

THE PARAGRAPHS
{paragraphs}
{feedback}
REPLY
JSON only, no other text, with every paragraph id exactly once:
{"order": ["<paragraph id>", ...]}
"""


class Writer:
    """Writes sentences from one template file, registering each value as a slot."""

    def __init__(self, extended: ExtendedRecord, template_file: str):
        env = Environment(loader=FileSystemLoader(TEMPLATES), undefined=StrictUndefined, autoescape=False)
        self.macros = env.get_template(template_file).module
        self.extended = extended
        self.slots: dict[str, str] = {}
        self.also: dict[str, str] = {}      # slot_id -> a second field the same figure is cited to

    def slot(self, path: str, words: bool = False, capital: bool = False, ordinal: bool = False) -> str | None:
        """A placeholder for the value at path, or None when there is none.

        words prints a count as a word (seven); capital starts it with a capital
        (Seven); ordinal prints it as an ordinal word (second).
        """
        value, _, _ = resolve(path, self.extended)
        if value is None or value == "" or value == [] or value == ():
            return None
        slot_id = f"s{len(self.slots)}"
        self.slots[slot_id] = path + (ORDINAL if ordinal else CAP_WORDS if capital else WORDS if words else "")
        return "{{ " + slot_id + " }}"

    def count(self, computed: str, declared: str, words: bool = False, capital: bool = False) -> str | None:
        """A computed count beside the count the record declared, as the report's cover prints them.

        Agreeing: one figure, cited to both fields. Disagreeing: the computed
        figure, then "(the record declares N)", each cited to its own field.
        Not declared: the computed figure alone.
        """
        figure = self.slot(computed, words=words, capital=capital)
        claimed, _, _ = resolve(declared, self.extended)
        if figure is None or claimed is None:
            return figure
        if claimed == resolve(computed, self.extended)[0]:
            self.also[f"s{len(self.slots) - 1}"] = declared
            return figure
        return f"{figure} (the record declares {self.slot(declared)})"

    def say(self, macro: str, **args) -> str:
        return str(getattr(self.macros, macro)(**args)).strip()


def prepare(
    record: Record, report: dict, derivations: dict, agents: dict, evidence_root: Path | None = None,
) -> tuple:
    """B-2's settings and this run's extended record, derived from the run alone.

    Takes the parsed report.yaml, b2_derivations.yaml (B-2's own derivation
    set; B-1 reads derivations.yaml) and agents.yaml. evidence_root, if given,
    is where evidence paths resolve, so the run values count the images that
    resolved. Returns (extended record with the run's values,
    DerivationConfig, VerificationConfig, B2Config).
    """
    config = derivation_config(report, derivations, hash_configs(report, derivations))
    verification = verification_config(agents, report, derivations)
    b2 = b2_config(agents)
    eligibility = compute_eligibility([record], config)
    extended = extend_record([record], config, eligibility)
    extended = add_run_values(extended, 0, config, eligibility, b2, units(report), evidence_root)
    return extended, config, verification, b2


def run_summary(record: Record, report: dict, derivations: dict, agents: dict) -> tuple[ExtendedRecord, dict | None]:
    """B-2's executive summary for one run: (extended record, envelope).

    The agents CLI (--summary) and the report engine call it the same way. The
    envelope is None when B-2 is disabled in agents.yaml. With prose on and no
    API key in the environment, code's paragraph order is kept
    (DEGRADED_TEMPLATE_ONLY).
    """
    extended, config, verification, b2 = prepare(record, report, derivations, agents)
    if not b2.enabled:
        return extended, None

    provider = None
    if b2.prose:
        try:
            provider = make_provider(provider_config(agents))
        except ConfigError as error:
            log.warning("B-2 has no provider: %s", error)
    return extended, executive_summary(extended, 0, provider, b2, config, verification)


def run_section_intros(record: Record, report: dict, derivations: dict, agents: dict) -> tuple[ExtendedRecord, dict | None]:
    """B-2's section introductions for one run: (extended record, zone -> envelope).

    None in place of the envelopes when B-2 is disabled in agents.yaml.
    """
    extended, config, verification, b2 = prepare(record, report, derivations, agents)
    if not b2.enabled:
        return extended, None
    return extended, section_intros(extended, 0, b2, config, verification)


def run_coverage(
    record: Record, report: dict, derivations: dict, agents: dict, evidence_root: Path | None = None,
) -> tuple[ExtendedRecord, dict | None]:
    """B-2's coverage statement for one run: (extended record, envelope).

    evidence_root is where evidence paths resolve (common.paths, as the report
    resolves them); without it the statement gives images referenced only.
    None in place of the envelope when B-2 is disabled in agents.yaml.
    """
    extended, config, verification, b2 = prepare(record, report, derivations, agents, evidence_root)
    if not b2.enabled:
        return extended, None
    return extended, coverage(extended, 0, b2, config, verification)


def run_item_notes(record: Record, report: dict, derivations: dict, agents: dict) -> tuple[ExtendedRecord, list | None]:
    """B-2's item notes for one run: (extended record, one envelope per checkpoint, record order).

    None in place of the envelopes when B-2 is disabled in agents.yaml.
    """
    extended, config, verification, b2 = prepare(record, report, derivations, agents)
    if not b2.enabled:
        return extended, None
    return extended, item_notes(extended, 0, b2, config, verification)


def executive_summary(
    extended: ExtendedRecord, run_index: int, provider: Provider | None, config: B2Config,
    derivation: DerivationConfig, verification: VerificationConfig,
) -> dict:
    """The executive summary of records[run_index], in its envelope.

    With prose on, the model orders the paragraphs; with no provider, or no
    valid order after max_attempts, code's order is kept and the status is
    DEGRADED_TEMPLATE_ONLY. With prose off there is no model call: OK,
    method deterministic.
    """
    extended = with_run_values(extended, run_index, derivation)
    record = extended.records[run_index]
    w = Writer(extended, "executive_summary.j2")
    paragraphs = summary_paragraphs(w, record, run_index, extended, derivation)
    order = list(paragraphs)
    model, method, attempts, status = None, "deterministic", 0, "OK"

    if config.prose:
        if provider is None:
            log.warning("B-2 executive summary uses code's order: no provider")
            status = "DEGRADED_TEMPLATE_ONLY"
        else:
            chosen, calls, reason = order_paragraphs(order, provider, config)
            model, attempts = provider.model, calls - 1
            if chosen is None:
                log.warning("B-2 executive summary uses code's order: %s", reason)
                status = "DEGRADED_TEMPLATE_ONLY"
            else:
                order, method = chosen, "template_slot_fill"

    text = "\n\n".join(paragraphs[key] for key in order)
    return seal("executive_summary", w, text, record, extended, config, derivation, verification,
                model, method, attempts, status, word_count=True)


def seal(
    section: str, w: Writer, text: str, record: Record, extended: ExtendedRecord, config: B2Config,
    derivation: DerivationConfig, verification: VerificationConfig, model: str | None = None,
    method: str = "deterministic", attempts: int = 0, status: str = "OK", word_count: bool = False,
) -> dict:
    """Fill, verify and wrap one section. Text that fails verification is never returned.

    A figure Writer.count cited to two fields gets both citations, on one span.
    word_count adds the number of words (the executive summary's, asked for).
    """
    filled = fill_slots(Template(section, text, w.slots), extended, derivation)
    result = verify_numeric(filled.text, extended, verification, filled.slots)
    derived = [m.group(1) for s in filled.slots if (m := re.match(r"derived\.values\.(\w+)", s.source_field))]
    result = replace(result, method=method, regeneration_attempts=attempts,
                     derived_values_used=list(dict.fromkeys(derived)))
    if not result.passed:
        log.warning("B-2 %s failed verification: %s", section, result.failures)
        return envelope(AGENT, model, config.prompt_version, [record.run_id], "REFUSED_UNVERIFIABLE", None, result)

    citations = []
    for slot in filled.slots:
        citations.append({"claim_span": list(slot.span), "source_field": slot.source_field})
        if slot.slot_id in w.also:
            citations.append({"claim_span": list(slot.span), "source_field": w.also[slot.slot_id]})
    output = {"text": filled.text, "citations": citations, "section": section}
    if word_count:
        output["word_count"] = sum(1 for word in filled.text.split() if word != "-")   # bullet marks are not words
    return envelope(AGENT, model, config.prompt_version, [record.run_id], status, output, result)


def summary_paragraphs(
    w: Writer, record: Record, i: int, extended: ExtendedRecord, config: DerivationConfig,
) -> dict[str, str]:
    """paragraph id -> its text with slot placeholders, in code's order (the worked example's)."""
    r = f"records[{i}]"
    v = f"derived.run_values.{record.run_id}"
    values = extended.derived.run_values[record.run_id]
    computed = values["computed"]
    paragraphs = {}

    run_status = RUN_STATUS.get(record.run_status) or f"ended with run status {w.slot(f'{r}.run_status')}"
    paragraphs["verdict"] = w.say(
        "verdict", facility=w.slot(f"{r}.facility_name"), date=w.slot(f"{v}.formatted.run_date"),
        start=w.slot(f"{v}.formatted.start_time"), duration=w.slot(f"{v}.formatted.duration"),
        run_status_phrase=run_status, final_status=w.slot(f"{r}.final_status"),
    )
    if not record.checkpoints:
        paragraphs["empty"] = w.say("empty")

    if record.checkpoints:
        c = f"{v}.computed"
        lines = [w.say(
            "results", passed=w.count(f"{c}.passed", f"{r}.passed_checkpoints", capital=True),
            passed_n=computed["passed"], failed=w.count(f"{c}.failed", f"{r}.failed_checkpoints", words=True),
            warned=w.slot(f"{c}.warn_result", words=True), warned_n=computed["warn_result"],
        )]
        groups = failed_groups(w, record, i)
        if groups:
            lines += [w.say("failed_lead"), *groups]
        paragraphs["results"] = "\n".join(lines)

    # The contradiction and the missing sensor data are set out in the coverage
    # statement; the summary says they exist and points there.
    if disagreeing(record, computed):
        paragraphs["contradiction"] = w.say("contradiction_pointer")
    if values["offline"]:
        paragraphs["gap"] = " ".join(w.say("gap_pointer", label=block.capitalize()) for block in values["offline"])

    found = highest_mean(record, extended, config)
    if found:
        path, zone, n, field_path, source = found
        g = f"{path}.groups.{zone}"
        paragraphs["measurement"] = w.say(
            "measurement", field=field_path.rsplit(".", 1)[-1], zone=w.slot(g), value=w.slot(f"{g}.mean"),
            n=w.slot(f"{g}.n"), n_n=n, source="sample" if source == "samples" else "checkpoint reading",
        )
    return paragraphs


def disagreeing(record: Record, computed: dict) -> bool:
    """Whether any count the record declares disagrees with what its arrays hold."""
    pairs = [("warned_checkpoints", "warned"), *((declared, key) for declared, key, _, _ in COUNT_PAIRS)]
    return any(getattr(record, d) is not None and getattr(record, d) != computed[k] for d, k in pairs)


def count_conflicts(w: Writer, record: Record, i: int, values: dict) -> list[str]:
    """One bullet per declared count that disagrees with its computed count, warned first."""
    r = f"records[{i}]"
    v = f"derived.run_values.{record.run_id}"
    computed = values["computed"]
    conflicts = []
    if record.warned_checkpoints is not None and record.warned_checkpoints != computed["warned"]:
        conflicts.append(w.say(
            "warned_conflict", declared=w.slot(f"{r}.warned_checkpoints"),
            warned=w.slot(f"{v}.computed.warned", capital=True), required=w.slot(f"{v}.computed.required"),
            alerts=w.slot(f"{v}.alerts.total"), alerts_n=values["alerts"]["total"],
            critical=w.slot(f"{v}.alerts.critical"),
        ))
    for declared, key, label, held_in in COUNT_PAIRS:
        if getattr(record, declared) is not None and getattr(record, declared) != computed[key]:
            conflicts.append(w.say(
                "count_conflict", declared=w.slot(f"{r}.{declared}"), label=label,
                computed=w.slot(f"{v}.computed.{key}"), held_in=held_in,
            ))
    return conflicts


# Coverage statement

def coverage(
    extended: ExtendedRecord, run_index: int, config: B2Config,
    derivation: DerivationConfig, verification: VerificationConfig,
) -> dict:
    """The coverage statement of records[run_index], in its envelope. No model call.

    Paragraphs, each only when it has something to say: completion (and every
    missed checkpoint with its recorded reason), evidence (the reached
    checkpoints that captured none), images and thermal coverage, sensor
    availability (devices off, readings excluded), and the record's own
    contradictions, every declared count beside its computed one.
    """
    extended = with_run_values(extended, run_index, derivation)
    record = extended.records[run_index]
    r = f"records[{run_index}]"
    v = f"derived.run_values.{record.run_id}"
    values = extended.derived.run_values[record.run_id]
    computed = values["computed"]
    w = Writer(extended, "coverage_statement.j2")

    def name(j: int) -> str | None:
        return w.slot(f"{r}.checkpoints[{j}].checkpoint_name")

    paragraphs = []

    # Completion
    lines = [w.say("route", required=w.slot(f"{v}.computed.required"), required_n=computed["required"])]
    if not record.checkpoints:
        lines.append(w.say("empty"))
    elif computed["completed"] == computed["required"] and not computed["missed"]:
        lines.append(w.say("all_completed", completed=w.slot(f"{v}.computed.completed")))
    else:
        lines.append(w.say(
            "completion", completed=w.slot(f"{v}.computed.completed", capital=True),
            required=w.slot(f"{v}.computed.required"), missed=w.slot(f"{v}.computed.missed", words=True),
            missed_n=computed["missed"],
        ))
        lines += [w.say("missed_item", name=name(j), reason=w.slot(f"{r}.checkpoints[{j}].missed_reason"))
                  for j, cp in enumerate(record.checkpoints) if cp.status == MISSED]
    paragraphs.append("\n".join(lines))

    # Evidence, over the checkpoints reached
    reached = [j for j, cp in enumerate(record.checkpoints) if cp.status != MISSED]
    if reached:
        lacking = [j for j in reached if not has_evidence(record.checkpoints[j])]
        if not lacking:
            paragraphs.append(w.say("all_evidence", reached=w.slot(f"{v}.evidence.reached")))
        else:
            lines = [w.say("some_evidence", with_evidence=w.slot(f"{v}.evidence.with_evidence", capital=True),
                           reached=w.slot(f"{v}.evidence.reached"))]
            lines += [w.say("no_evidence_item", bullet=len(lacking) > 1, name=name(j),
                            observed=w.slot(f"{r}.checkpoints[{j}].observed"))
                      for j in lacking]
            paragraphs.append(("\n" if len(lacking) > 1 else " ").join(lines))

    # Images and thermal, over the checkpoints that captured evidence
    captured = [j for j in reached if has_evidence(record.checkpoints[j])]
    if captured:
        lines = [w.say("images", with_evidence=w.slot(f"{v}.evidence.with_evidence"), with_evidence_n=len(captured),
                       images=w.slot(f"{v}.evidence.images_referenced"),
                       resolved=w.slot(f"{v}.evidence.images_resolved"))]
        entries = values["checkpoints"]
        full = [j for j in captured if entries[j]["directions"] and entries[j]["thermal_pairs"] == entries[j]["directions"]]
        none = [j for j in captured if not entries[j]["thermal_pairs"]]
        partial = [j for j in captured if j not in full and j not in none]
        if len(full) == len(captured):
            lines[0] += " " + w.say("thermal_complete")
        else:
            lines.append(w.say("thermal_lead", word="absent" if len(none) == len(captured) else "uneven"))
            if full:
                lines.append(w.say("thermal_full", names=join_words([name(j) for j in full])))
            if partial:
                lines.append(w.say("thermal_partial", items=join_words([
                    w.say("thermal_some", name=name(j), pairs=w.slot(f"{v}.checkpoints[{j}].thermal_pairs"),
                          directions=w.slot(f"{v}.checkpoints[{j}].directions"))
                    for j in partial
                ])))
            if none:
                lines.append(w.say("thermal_none", names=join_words([name(j) for j in none])))
        paragraphs.append("\n".join(lines))

    # Sensor availability, at the checkpoints reached
    if reached:
        lines = [
            w.say("offline", label=block.capitalize(), device=w.slot(f"{v}.offline.{block}.device"),
                  block=block, flag=w.slot(f"{v}.offline.{block}.flag"), off=w.slot(f"{v}.offline.{block}.off"))
            for block in values["offline"]
        ]
        sensors = {block: s for block, s in values["sensors"].items() if block not in values["offline"]}
        everywhere = [block for block, s in sensors.items() if s["available"] == len(reached)]
        if everywhere:
            lines.append(w.say(
                "available_everywhere", labels=join_words(everywhere).capitalize(),
                n=w.slot(f"{v}.sensors.{everywhere[0]}.available"),
            ))
        for block, s in sensors.items():
            if block in everywhere:
                continue
            lines.append(w.say("available_some", label=block.capitalize(), n=w.slot(f"{v}.sensors.{block}.available"),
                               reached=w.slot(f"{v}.evidence.reached")))
            lines += [w.say("excluded_item", reason=w.slot(f"{v}.sensors.{block}.excluded[{k}].reason"),
                            count=w.slot(f"{v}.sensors.{block}.excluded[{k}].count"))
                      for k in range(len(s["excluded"]))]
        if lines:
            paragraphs.append("\n".join(lines))

    # The record's own contradictions
    conflicts = count_conflicts(w, record, run_index, values)
    if conflicts:
        paragraphs.append("\n".join([w.say("conflict_lead"), *conflicts, w.say("unreconciled")]))

    return seal("coverage", w, "\n\n".join(paragraphs), record, extended, config, derivation, verification)


def failed_groups(w: Writer, record: Record, i: int) -> list[str]:
    """One bullet per group of FAIL checkpoints that recorded the same things.

    Checkpoints group by whether they captured evidence and by the findings
    logged against them, each finding as its feature and severity. A group's
    findings are cited to its first checkpoint's findings; the others carry the
    same values. Groups and their checkpoints keep route order.
    """
    r = f"records[{i}]"
    groups: dict[tuple, dict] = {}
    for j, cp in enumerate(record.checkpoints):
        if cp.result_status != "FAIL":
            continue
        first: dict[tuple, int] = {}
        for k, f in enumerate(record.findings):
            if f.checkpoint_id == cp.checkpoint_id:
                first.setdefault((f.feature, f.severity), k)
        group = groups.setdefault((has_evidence(cp), tuple(first)), {"findings": list(first.values()), "checkpoints": []})
        group["checkpoints"].append(j)

    return [
        w.say(
            "failed_group", has_evidence=has, names=join_words([w.slot(f"{r}.checkpoints[{j}].checkpoint_name") for j in g["checkpoints"]]),
            findings=join_words([
                w.say("finding", feature=w.slot(f"{r}.findings[{k}].feature") or "not recorded",
                      severity=w.slot(f"{r}.findings[{k}].severity") or "not recorded")
                for k in g["findings"]
            ]) if g["findings"] else None,
        )
        for (has, _), g in groups.items()
    ]


def join_words(items: list[str | None]) -> str:
    """a; a and b; a, b and c. Missing items are left out."""
    items = [item for item in items if item is not None]
    if not items:
        return "not recorded"
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


# Section introductions

def section_intros(
    extended: ExtendedRecord, run_index: int, config: B2Config,
    derivation: DerivationConfig, verification: VerificationConfig,
) -> dict[str, dict]:
    """zone -> its section introduction in an envelope, zones in route order.

    One sentence for the zone (its checkpoints, the samples it recorded), then
    one line per samples-based group_mean in b2_derivations.yaml: the mean,
    how many samples were usable and how many excluded, and the zone's rank
    among the zones where that mean could be computed. A zone with no samples
    gets the first sentence only. No model call.
    """
    extended = with_run_values(extended, run_index, derivation)
    record = extended.records[run_index]
    v = f"derived.run_values.{record.run_id}"
    values = extended.derived.run_values[record.run_id]
    intros = {}

    for zone, z in values["zones"].items():
        w = Writer(extended, "section_intro.j2")
        zp = f"{v}.zones.{zone}"
        lines = [w.say(
            "intro", checkpoints=w.slot(f"{zp}.checkpoints"), checkpoints_n=z["checkpoints"],
            samples=w.slot(f"{zp}.samples"), samples_n=z["samples"],
        )]
        for name, m in z["metrics"].items() if z["samples"] else ():
            field = derivation.derivations[name]["field_path"].rsplit(".", 1)[-1]
            mp = f"{zp}.metrics.{name}"
            if "rank" not in m:
                lines.append(w.say("no_mean", field=field, excluded=w.slot(f"{mp}.excluded"), excluded_n=m["excluded"]))
                continue
            k = next(k for k, out in enumerate(extended.derived.values[name]) if out.get("run_id") == record.run_id)
            total = values["zones_with_mean"][name]
            lines.append(w.say(
                "mean", field=field, mean=w.slot(f"derived.values.{name}[{k}].groups.{zone}.mean"),
                usable=w.slot(f"{mp}.usable"), usable_n=m["usable"],
                excluded=w.slot(f"{mp}.excluded"), excluded_n=m["excluded"],
                rank=w.slot(f"{mp}.rank", ordinal=True), rank_n=m["rank"], ranked=total > 1,
                joint=sum(1 for other in values["zones"].values() if other["metrics"][name].get("rank") == m["rank"]) > 1,
                total=w.slot(f"{v}.zones_with_mean.{name}"),
            ))
        intros[zone] = seal("section_intro", w, "\n".join(lines), record, extended, config, derivation, verification)
    return intros


# Item notes

def item_notes(
    extended: ExtendedRecord, run_index: int, config: B2Config,
    derivation: DerivationConfig, verification: VerificationConfig,
) -> list[dict]:
    """One note per checkpoint, in record order, each in its envelope.

    The variant, first that applies: missed (not reached, with its recorded
    reason); no evidence (with the findings logged against it); sensor warning
    (each warning, with the reading it is about when that reading is eligible to
    print); normal (what was observed and photographed), followed by "No
    findings recorded for this checkpoint." when there are none. No model call.
    """
    extended = with_run_values(extended, run_index, derivation)
    record = extended.records[run_index]
    r = f"records[{run_index}]"
    v = f"derived.run_values.{record.run_id}"
    entries = extended.derived.run_values[record.run_id]["checkpoints"]
    notes = []

    for j, cp in enumerate(record.checkpoints):
        w = Writer(extended, "item_note.j2")
        c, e = f"{r}.checkpoints[{j}]", f"{v}.checkpoints[{j}]"
        name, result = w.slot(f"{c}.checkpoint_name"), w.slot(f"{c}.result_status")

        if cp.status == MISSED:
            text = w.say("missed", name=name, reason=w.slot(f"{c}.missed_reason"))
        elif not has_evidence(cp):
            first: dict[tuple, int] = {}
            for k, f in enumerate(record.findings):
                if f.checkpoint_id == cp.checkpoint_id:
                    first.setdefault((f.feature, f.severity), k)
            text = w.say(
                "no_evidence", name=name, result=result,
                findings=w.slot(f"{e}.findings", words=True), findings_n=entries[j]["findings"],
                items=join_words([
                    w.say("finding", feature=w.slot(f"{r}.findings[{k}].feature") or "not recorded",
                          severity=w.slot(f"{r}.findings[{k}].severity") or "not recorded")
                    for k in first.values()
                ]),
            )
        elif entries[j]["warnings"]:
            readings = []
            for k, warning in enumerate(entries[j]["warnings"]):
                value = w.slot(f"{c}.sensor.{warning['field']}") if warning["eligible"] else None
                readings.append(w.say(
                    "reading", metric=w.slot(f"{e}.warnings[{k}].metric"), value=value,
                    unit=w.slot(f"{e}.warnings[{k}].unit"),
                    label=w.slot(f"{c}.sensor.warnings[{k}].label") or w.slot(f"{c}.sensor.warnings[{k}].code"),
                ))
            text = w.say("warning", name=name, result=result, readings=join_words(readings))
        else:
            observed = "a normal scene" if cp.observed == "normal_scene" else w.slot(f"{c}.observed")
            text = w.say(
                "normal", name=name, result=result, observed=observed,
                rgb=w.slot(f"{e}.directions"), rgb_n=entries[j]["directions"],
                thermal=w.slot(f"{e}.thermal_pairs"), thermal_n=entries[j]["thermal_pairs"],
            )
            if not entries[j]["findings"]:
                text += " " + w.say("nothing")
        notes.append(seal("item_note", w, text, record, extended, config, derivation, verification))
    return notes


def highest_mean(record: Record, extended: ExtendedRecord, config: DerivationConfig) -> tuple | None:
    """(output path, zone, n, field_path, source) of the highest zone mean in this run.

    From the first group_mean instance in b2_derivations.yaml whose output for this
    run has an OK zone. None when there is none.
    """
    for name, entry in config.derivations.items():
        if entry.get("type") != "group_mean":
            continue
        for k, out in enumerate(extended.derived.values.get(name, [])):
            if out.get("run_id") != record.run_id or out.get("status") != "OK":
                continue
            groups = [(zone, g) for zone, g in out["groups"].items() if zone is not None and g["status"] == "OK"]
            if groups:
                zone, g = max(groups, key=lambda item: item[1]["mean"])
                return f"derived.values.{name}[{k}]", zone, g["n"], out["field_path"], entry.get("source")
    return None


def order_paragraphs(ids: list[str], provider: Provider, config: B2Config) -> tuple[list[str] | None, int, str | None]:
    """The model's reading order: (order or None, calls made, why not)."""
    feedback = ""
    for attempt in range(1, config.max_attempts + 1):
        lines = "\n".join(f"- {key}: {PARAGRAPHS[key]}" for key in ids)
        values = {"paragraphs": lines, "feedback": feedback}
        prompt = re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), PROMPT)
        schema = {
            "type": "object",
            "properties": {"order": {"type": "array", "items": {"type": "string", "enum": ids}}},
            "required": ["order"],
            "additionalProperties": False,
        }
        try:
            reply = provider.complete(prompt, temperature=config.temperature, schema=schema)
        except ProviderError as error:
            return None, attempt, str(error)
        try:
            return parse_order(reply, ids), attempt, None
        except ValueError as error:
            log.warning("B-2 attempt %d rejected: %s", attempt, error)
            feedback = f"\nYOUR PREVIOUS REPLY WAS REJECTED: {error}\nFix that and reply again.\n"
    return None, config.max_attempts, f"no valid order after {config.max_attempts} attempts; last: {feedback.strip()}"


def parse_order(reply: str, ids: list[str]) -> list[str]:
    """The ids from {"order": [...]}. Raises ValueError unless every id is there exactly once."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.strip())
    try:
        order = json.loads(text).get("order")
    except (json.JSONDecodeError, AttributeError):
        raise ValueError('the reply must be one JSON object with "order"') from None
    if not isinstance(order, list) or sorted(order, key=str) != sorted(ids):
        raise ValueError(f'"order" must list each of {", ".join(ids)} exactly once')
    return order


# Run values

def add_run_values(
    extended: ExtendedRecord, run_index: int, config: DerivationConfig, eligibility: Eligibility,
    b2: B2Config | None = None, field_units: dict[str, str] | None = None, evidence_root: Path | None = None,
) -> ExtendedRecord:
    """The extended record with run_values for records[run_index] added.

    b2 and field_units name the measurement a sensor warning is about (item
    notes); without them a warning is named by its label alone. evidence_root
    adds evidence.images_resolved: the referenced images that resolve to a
    readable file there (common.paths, the report's own resolver).
    """
    record = extended.records[run_index]
    entry = run_values_for(record, config)
    entry["zones"], entry["zones_with_mean"] = zones_for(record, config, extended.derived.values, eligibility)
    entry["sensors"] = sensors_for(record, config, eligibility)
    if evidence_root is not None:
        entry["evidence"]["images_resolved"] = sum(
            1 for cp in record.checkpoints for image in resolve_checkpoint_images(cp, evidence_root) if image.readable
        )
    entry["checkpoints"] = checkpoints_for(
        record, eligibility, b2.warning_fields if b2 else {}, b2.metric_names if b2 else {}, field_units or {},
    )
    run_values = {**extended.derived.run_values, record.run_id: entry}
    return replace(extended, derived=replace(extended.derived, run_values=run_values))


def with_run_values(extended: ExtendedRecord, run_index: int, config: DerivationConfig) -> ExtendedRecord:
    """extended as given if records[run_index] already has its run values, else with them added."""
    if extended.records[run_index].run_id in extended.derived.run_values:
        return extended
    return add_run_values(extended, run_index, config, compute_eligibility(extended.records, config))


def zones_for(record: Record, config: DerivationConfig, values: dict, eligibility: Eligibility) -> tuple[dict, dict]:
    """Per zone of the route (route order), and per samples-based group_mean instance.

    zones: zone -> {checkpoints, samples, metrics: {instance: {usable, excluded, rank?}}}
      checkpoints  distinct checkpoint ids in the zone
      samples      telemetry samples the zone recorded
      usable       of those, the samples eligibility kept for the instance's field
      excluded     the samples it left out; usable + excluded = samples
      rank         the zone's position by mean, highest first, among the zones
                   whose mean is OK; tied means share a rank. Absent with no mean.
    zones_with_mean: instance -> how many zones have an OK mean.
    """
    ids: dict[str, set] = {}
    for cp in record.checkpoints:
        ids.setdefault(cp.zone, set()).add(cp.checkpoint_id)
    recorded = Counter(sample.zone for sample in record.sensor_samples)
    zones = {zone: {"checkpoints": len(cids), "samples": recorded.get(zone, 0), "metrics": {}} for zone, cids in ids.items()}

    with_mean = {}
    for name, entry in config.derivations.items():
        if entry.get("type") != "group_mean" or entry.get("source") != "samples":
            continue
        used, dropped = for_run(*eligibility[entry["field_path"]], record.run_id, "sample")
        usable = Counter(value.zone for value in used)
        excluded = Counter()
        for exclusion in dropped:
            excluded[exclusion.zone] += exclusion.count
        out = next((o for o in values.get(name, []) if o.get("run_id") == record.run_id), {})
        means = {zone: g["mean"] for zone, g in (out.get("groups") or {}).items() if zone in zones and g.get("status") == "OK"}
        with_mean[name] = len(means)
        for zone, z in zones.items():
            metric = {"usable": usable.get(zone, 0), "excluded": excluded.get(zone, 0)}
            if zone in means:
                metric["rank"] = 1 + sum(1 for other in means.values() if other > means[zone])
            z["metrics"][name] = metric
    return zones, with_mean


def run_values_for(record: Record, config: DerivationConfig) -> dict:
    """Every value B-2 states about one run that no derivation gives."""
    checkpoints = record.checkpoints
    alerts = Counter({"critical": 0, "warning": 0})
    alerts.update(a.severity for a in record.sensor_alerts)

    reached = [cp for cp in checkpoints if cp.status != MISSED]
    no_evidence = [cp.checkpoint_id for cp in checkpoints if not has_evidence(cp)]
    thermal = {"full": [], "partial": [], "none": []}
    for cp in checkpoints:
        if has_evidence(cp):
            thermal[thermal_coverage(cp)].append(cp.checkpoint_id)

    return {
        "computed": {
            "required": len(checkpoints),
            "completed": sum(1 for cp in checkpoints if cp.status == config.completed_status),
            "passed": sum(1 for cp in checkpoints if cp.result_status == "PASS"),
            "failed": sum(1 for cp in checkpoints if cp.result_status == "FAIL"),
            "warn_result": sum(1 for cp in checkpoints if cp.result_status == "WARN"),
            "missed": sum(1 for cp in checkpoints if cp.status == MISSED),
            "warned": sum(1 for cp in checkpoints if was_warned(cp)),
            "findings": len(record.findings),
        },
        "alerts": {"total": len(record.sensor_alerts), **dict(sorted(alerts.items()))},
        "offline": offline_blocks(record, config),
        "findings_by_checkpoint": dict(Counter(f.checkpoint_id for f in record.findings)),
        "evidence": {
            "images_referenced": sum(len(cp.evidence_images) for cp in checkpoints),
            "reached": len(reached),                                        # checkpoints not MISSED
            "with_evidence": sum(1 for cp in reached if has_evidence(cp)),   # of those, how many captured evidence
            "no_evidence_ids": no_evidence,
            **{f"thermal_{kind}": len(ids) for kind, ids in thermal.items()},
            **{f"thermal_{kind}_ids": ids for kind, ids in thermal.items()},
        },
        "formatted": {
            "run_date": format_date(record.start_time, config) if record.start_time else None,
            "start_time": record.start_time.strftime(CLOCK) if record.start_time else None,
            "duration": duration_words(record.duration),
        },
    }


def was_warned(checkpoint: Checkpoint) -> bool:
    """A WARN verdict, or any sensor warning raised at the checkpoint."""
    return checkpoint.result_status == "WARN" or bool(checkpoint.sensor and checkpoint.sensor.warnings)


def has_evidence(checkpoint: Checkpoint) -> bool:
    """It lists evidence images and was not recorded as observed: no_evidence."""
    return bool(checkpoint.evidence_images) and checkpoint.observed != "no_evidence"


def sensors_for(record: Record, config: DerivationConfig, eligibility: Eligibility) -> dict:
    """Per sensor block, at the checkpoints reached: how many readings were usable, and why the rest were not.

    block -> {available, excluded: [{reason, count}]}, for the blocks of the
    group_mean fields in b2_derivations.yaml (one field read per block).
    Readings at MISSED checkpoints are left out: the completion paragraph
    already says those checkpoints were never reached.
    """
    missed = {cp.checkpoint_id for cp in record.checkpoints if cp.status == MISSED}
    sensors = {}
    for entry in config.derivations.values():
        field = entry.get("field_path", "")
        block = field.split(".", 1)[0]
        if entry.get("type") != "group_mean" or block in sensors or field not in eligibility:
            continue
        used, dropped = for_run(*eligibility[field], record.run_id, "checkpoint")
        reasons = Counter()
        for exclusion in dropped:
            if exclusion.scope not in missed:
                reasons[exclusion.reason] += exclusion.count
        sensors[block] = {
            "available": sum(1 for value in used if value.source_id not in missed),
            "excluded": [{"reason": reason, "count": count} for reason, count in reasons.items()],
        }
    return sensors


def checkpoints_for(
    record: Record, eligibility: Eligibility, warning_fields: dict[str, str],
    metric_names: dict[str, str], field_units: dict[str, str],
) -> list[dict]:
    """One entry per checkpoint, in record order (a checkpoint visited twice has two).

    directions     views with an RGB image ("directions photographed")
    thermal_pairs  views with both an RGB and a thermal image
    findings       run-level findings naming this checkpoint
    warnings       one per sensor warning, in the record's order: the field it
                   is about (warning_fields, by code; None if unlisted), how a
                   sentence names it and its unit, and whether this
                   checkpoint's reading of it is eligible to print (section 3.2)
    """
    eligible_at = {}
    for field in set(warning_fields.values()):
        if field in eligibility:
            used, _ = for_run(*eligibility[field], record.run_id, "checkpoint")
            eligible_at[field] = {value.source_id for value in used}
    findings = Counter(f.checkpoint_id for f in record.findings)

    entries = []
    for cp in record.checkpoints:
        rgb, thermal = views(cp)
        warnings = []
        for warning in (cp.sensor.warnings if cp.sensor else ()):
            field = warning_fields.get(warning.code)
            name = field.rsplit(".", 1)[-1] if field else None
            warnings.append({
                "field": field,
                "metric": metric_names.get(name, name) if name else None,
                "unit": field_units.get(name) if name else None,
                "eligible": bool(field) and cp.checkpoint_id in eligible_at.get(field, ()),
            })
        entries.append({
            "directions": len(rgb), "thermal_pairs": len(rgb & thermal),
            "findings": findings.get(cp.checkpoint_id, 0), "warnings": warnings,
        })
    return entries


def views(checkpoint: Checkpoint) -> tuple[set, set]:
    """The view labels with an RGB image, and those with a thermal one (from the filenames)."""
    rgb, thermal = set(), set()
    for uri in checkpoint.evidence_images:
        view, is_thermal = parse_filename(uri, checkpoint.checkpoint_id)
        (thermal if is_thermal else rgb).add((view or "").casefold())
    return rgb, thermal


def thermal_coverage(checkpoint: Checkpoint) -> str:
    """full: every view with an RGB image has a thermal one; none: no thermal at all."""
    rgb, thermal = views(checkpoint)
    if not thermal:
        return "none"
    return "full" if rgb <= thermal else "partial"


def offline_blocks(record: Record, config: DerivationConfig) -> dict:
    """Blocks whose device flag was false wherever it could be read.

    block -> {flag, device, off, readable}: how many checkpoints said false, out
    of how many reported the flag. device is the flag without _ok (sps30_ok -> SPS30). A checkpoint whose whole reading is unusable says
    nothing about its devices; a block any checkpoint reported working is left out.
    """
    off, readable, working = Counter(), Counter(), set()
    for cp in record.checkpoints:
        sensor = cp.sensor
        if (sensor is None or sensor.raw is None or sensor.ok is False
                or sensor.status != config.connected_status or sensor.sensor_hub_reachable is False):
            continue
        for flag, block in config.subsystem_flags.items():
            value = getattr(sensor.raw, flag, None)
            if value is None:
                continue
            readable[block] += 1
            if value is False:
                off[block] += 1
            else:
                working.add(block)
    return {
        block: {"flag": flag, "device": flag.removesuffix("_ok").upper(), "off": off[block], "readable": readable[block]}
        for flag, block in config.subsystem_flags.items()
        if off[block] and block not in working
    }


def duration_words(duration: str | None) -> str | None:
    """00:10:54 -> "10 minutes and 54 seconds". Anything not HH:MM:SS is kept as recorded."""
    if duration is None:
        return None
    parts = duration.split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return duration
    words = [
        f"{n} {unit}{'' if n == 1 else 's'}"
        for n, unit in zip(map(int, parts), ("hour", "minute", "second")) if n
    ]
    if not words:
        return "0 seconds"
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]
