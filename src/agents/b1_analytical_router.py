"""B-1 Analytical, router: the first of B-1's three model calls.

The model names what the question is about (runs, zones, checkpoints and
derivations) without answering it. Code checks every name, turns run positions
and dates into run_ids, and hands the result to the SQL writer.

The reply is held to reply_schema by the provider and checked again by
parse_reply; a bad one is sent back with the reason, up to max_attempts calls.
A run, date, zone or checkpoint the records do not hold is not a bad reply:
the question is about something that is not there, so the route abstains.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

from common.schema import Record
from agents.provider import Provider
from agents.routes import PROMPTS
from agents.schema import B1Config, DerivationConfig
from agents.utils import fill_prompt

log = logging.getLogger(__name__)

# The route for what the runs recorded, named alongside the derivation
# instances. Not a derivations.yaml instance: keep it in step with
# routes.records.TYPE, and never name an instance this.
RECORDS = "records"

# The derivation types the router offers: every type with a SQL writer prompt.
ROUTED_TYPES = tuple(kind for kind in PROMPTS if kind != RECORDS)

# Written by hand. build_prompt fills these placeholders:
#   {derivations}  one line per routed instance, what it works out, from derivations.yaml
#   {layout}       every zone in the records and the checkpoints in it, one line
#                  each, so zones and checkpoints are not taken for each other
#   {days}         every day a run started on, so a date given without its
#                  month or year can be completed
#   {question}     the user's question
#   {feedback}     empty on the first call; why the previous reply was rejected after that
# RECORDS is left out for now. To put it back, uncomment the RECORDS lines
# below and restore this section in PROMPT, after the date range paragraph:
# THE RECORDS THEMSELVES
# "records" is not a figure: it is what each run wrote down, as written. Name it
# when the question asks for a detail a run recorded:
# - the run itself: its facility, status, final result, start and end time,
#   duration, and the counts the robot reported for itself
# - what happened at a checkpoint: whether it was completed or missed and why,
#   its result, what was observed, the notes
# - the findings (what, where, how severe, whether they need review, the
#   recommended action), the sensor alerts, and the run's event log
# - a single sensor reading at a checkpoint or at a time, or why a reading is
#   missing
# It works nothing out: it has no counts, averages, maxima or comparisons of its
# own. So when the question asks for a figure, name the figure above that holds
# it, never records. When it asks how many, an average, the highest or a
# comparison and no figure above holds it, abstain; records will not work it
# out. A question can need both, e.g. a count and the notes of the checkpoints
# it counts: name both.
PROMPT = """You are the first step in answering a question about facility inspections.

HOW THIS WORKS
An inspection robot drives a route of checkpoints around a facility. Each time
it does that is a run. At every checkpoint it records whether the stop was
completed and whether it passed, and takes sensor readings. Along the way it
also logs findings and sensor alerts. Several runs are loaded, oldest first.

The route is laid out in two levels: zones, and checkpoints inside them.
- A zone is an area of the route, e.g. a row of racks (rowA_back).
- A checkpoint is one stop in a zone, e.g. a single rack (a3_back). A zone
  can hold several checkpoints, or just one; then the two often share a name
  (checkpoint_1 is both a zone and its only checkpoint).
- Telemetry samples are logged every couple of seconds while the robot
  drives, each tagged with the zone it was in, not with a checkpoint.
So a figure worked out per zone covers every reading in that zone. A question
about a checkpoint gets the figure of the zone it is in, when the figure is
per zone.

Some figures have already been worked out for every run, listed below. You
say which runs, zones, checkpoints and which of these the question is about;
the next step then fetches exactly those and writes the answer. So you only need to point at the right things.

WHAT CAN BE FETCHED
{derivations}

In plain terms. Each figure is worked out once for every run, oldest first,
unless it says otherwise. A figure, or one part of it (one checkpoint, one
zone), can be marked not computable, with the reason: the readings it needed
were missed or came from a sensor flagged faulty. The answer reports that
itself.

- A threshold comparison takes the one reading and the one limit its line
  above names, and checks every checkpoint in a run against them. For each
  checkpoint it holds the reading, whether it is over the limit, and how far
  over or under the limit it is. For the run it holds how many checkpoints
  were over and how many were not. Use it for "which checkpoints went over",
  "how many exceeded" and "by how much", and also for "which stayed under"
  and "how far below". Any other reading or limit is not in it.
- A count takes one condition, a field of a checkpoint, finding or alert
  being one value, as its line above says. Its config, under that line, is
  its setting as written: scope is the items it counts, and the condition's
  field and equals are the field and the exact value an item must have. It
  counts that value only: a question about another value of the field
  ("passed" when equals is FAIL) or another field is not this count. For each run it holds how many
  items meet it, the ids of those items, how many items it could be checked
  on, and the items left out of the count with the reason each was left out
  (e.g. a missed checkpoint). Of the items it checked, it keeps only the ones
  that meet the condition, never the others.
- A share takes a condition the same way. For each run it holds how many
  items meet it, out of how many, as a percentage, and the ids of the ones
  that meet it. Use it for "what percentage", "what share" or "how many out
  of".
- A mean averages one sensor reading over its source: samples are the
  readings logged every couple of seconds while the robot drives, checkpoints
  the one reading taken at each stop. For each run it gives one mean per
  group (per zone, or one for the whole run, as its line says) and how many
  readings went into it. Per zone, it also lists the checkpoints in each zone,
  so a question about the mean at a checkpoint is answered with its zone's
  mean; name the checkpoint. It holds no single reading, highest or lowest. A
  question can ask about one mean or several; name each one it needs.
- A maximum gives the highest value of one sensor reading in each run (per
  group, as its line says), the checkpoint or sample it came from, and when
  it was taken. Per zone, it also lists the checkpoints in each zone, so a
  question about the peak at a checkpoint is answered with its zone's peak
  and the checkpoint that reached it; name the checkpoint. Use it for "highest", "peak", "maximum" or "where was it
  highest". It does not hold the lowest reading, or any below the highest.
- A ranking lists the highest readings of one sensor field in each run,
  highest first, as many as its line says, each with its rank and the
  checkpoint or sample it came from. Use it for "the top 5", "the highest
  readings", "which checkpoints had the highest" or "where did a3_back rank".
  It ranks the checkpoints of the whole run and holds no zones: the top
  readings within one zone are not in it. It never holds the lowest. A
  maximum and a ranking can both give the single highest reading;
  name the one whose reading and source (samples or checkpoints) the question
  matches.
- A difference compares two runs only: the latest and the one before it. It
  holds the checkpoint ids only the earlier run's record lists, only the
  latest's lists, and both list, and how many of each. Its runs are [-2, -1];
  to compare any other runs' checkpoints, abstain. It says nothing about
  readings or results, and never whether a change is better or worse.
- A date range is one figure for all runs together: the earliest and latest
  run start and which runs they were, the whole days between them, how many
  runs are loaded, and on how many of those days a run was taken and on how
  many none was. It also lists every day a run was taken on, with its weekday
  and the runs that day. Use it for "when", "which days", "how many runs on
  21 August", "which day had the most runs" or "were runs taken on a Monday".
  Its runs are "all", unless the question names runs or days: "when did the
  third run start" is [3], "which runs were taken on 21/8/2026" is
  ["2026-08-21"]. It covers only the runs loaded here, not when anything
  began: the age or lifetime of the route, site or equipment is not in it.

Questions use everyday words for readings. Match them to the sensor block in
the names above:
- vibration or shaking is the accelerometer (the ADXL345 sensor)
- temperature, heat, how hot or cold is environment (the BME680 sensor);
  humidity and pressure are environment too
- particulate, particles, dust, air quality, PM2.5 or PM10 is particulate (the
  SPS30 sensor)
So "average vibration per zone" is the mean of accelerometer.vibration_rms_g.
Use a share for "what percentage" or "what share", and a count for "how many"
or "which". Readings that could not be trusted were left out of all of these
and the answer will say so; that is not a reason to abstain.

Zones in the records, and the checkpoints in each (zone: checkpoints):
{layout}

Days the runs started on:
{days}

HOW TO REPLY
Reply with JSON only, one of:
  {"intent": "answer", "runs": ..., "zones": [...], "checkpoints": [...], "derivations": [...]}
  {"intent": "abstain", "runs": "all", "zones": [], "checkpoints": [], "derivations": []}

runs: which runs the question is about.
- "all" if it does not name any.
- Otherwise a list of positions, dates or both. Counting from the oldest, 1 is
  the first run and 2 the second. Counting back from the newest, -1 is the
  latest and -2 the one before it. "the first and third runs" is [1, 3], "the
  latest run" is [-1], "the last two runs" is [-2, -1], "the first and the
  latest" is [1, -1].
- A date is written "YYYY-MM-DD" and names every run that started that day:
  "the runs on 21/8/2026" is ["2026-08-21"]. Dates written with slashes are
  day first, so 3/4/2026 is 3 April. A span of days is written "first/last":
  "the runs from 1 to 10 August 2026" is ["2026-08-01/2026-08-10"], "the runs
  in August 2026" is ["2026-08-01/2026-08-31"]. If the question leaves out the
  month or year, take it from the days listed above.
zones: the zones the question names, written as before a colon in the list
  above. "row A back" is rowA_back. [] if it names none.
checkpoints: the checkpoints the question names, written as after a colon in
  the list above. "checkpoint 3" or "Checkpoint3" is checkpoint_3. [] if it
  names none.
  Never put a zone in checkpoints or a checkpoint in zones. A name that is
  both (checkpoint_1: checkpoint_1) goes in checkpoints when the question
  means the stop ("at checkpoint 1") and in zones when it means the area
  ("in the checkpoint_1 zone"). If a name matches nothing in the list, copy
  it as written into the one the question means.
derivations: the names, from the list above, of the figures the question
  needs. At least one; more only if the question asks about more
  than one thing ("how many passed and how many failed").

WHEN TO ABSTAIN
Abstain if no figure above answers the question:
-the model should abstain on how/why questions like "Why did the temperature reduce/peak" or
 "Who is the manufacturer of the robot". Basically questions which the data will not contain.

{feedback}QUESTION
{question}
"""


class RouteError(ValueError):
    """The model's reply broke a rule. The message goes back to the model."""


@dataclass(frozen=True)
class Route:
    """What the question is about, checked against the records and derivations.yaml.

    abstain:     True when the model abstained, or named a run, date, zone or
                 checkpoint the records do not hold
    reason:      why, when abstain is True
    run_ids:     the runs named, oldest first
    checkpoints: the checkpoint_ids named; [] means no checkpoint filter
    derivations: the derivation instance names the question needs
    attempts:    model calls made
    zones:       the zones named; [] means no zone filter
    """

    abstain: bool
    reason: str | None
    run_ids: list[str]
    checkpoints: list[str]
    derivations: list[str]
    attempts: int
    zones: list[str] = field(default_factory=list)


def route_question(
    question: str, records: Sequence[Record], provider: Provider, config: B1Config, derivation: DerivationConfig,
    calls: list[dict] | None = None,
) -> Route:
    """Route one question over the records, oldest first. Raises ProviderError, or RouteError when no reply is valid after max_attempts.

    calls, if given, gets {attempt, reply, rejected} for every model call, rejected
    being why the reply was sent back, or None.
    """
    calls = [] if calls is None else calls
    feedback = ""
    for attempt in range(1, config.max_attempts + 1):
        prompt = build_prompt(question, feedback, records, derivation)
        reply = provider.complete(prompt, temperature=config.temperature, schema=reply_schema(derivation))
        try:
            route = parse_reply(reply, records, derivation, attempt)
        except RouteError as error:
            log.warning("B-1 router attempt %d rejected: %s", attempt, error)
            calls.append({"attempt": attempt, "reply": reply, "rejected": str(error)})
            feedback = f"YOUR PREVIOUS REPLY WAS REJECTED: {error}\nFix that and reply again.\n\n"
            continue
        log.info("B-1 router attempt %d: %s", attempt, route)
        calls.append({"attempt": attempt, "reply": reply, "rejected": None})
        return route
    raise RouteError(f"no valid reply after {config.max_attempts} attempts; last: {feedback.strip()}")


def offered(derivation: DerivationConfig) -> dict[str, dict]:
    """The derivation instances the router may name, by instance name."""
    return {name: entry for name, entry in derivation.derivations.items() if entry["type"] in ROUTED_TYPES}


def reply_schema(derivation: DerivationConfig) -> dict:
    """The JSON shape the provider holds the reply to. An abstain fills every key too; only intent is read."""
    return {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": ["answer", "abstain"]},
            "runs": {"anyOf": [
                {"type": "string", "enum": ["all"]},
                {"type": "array", "items": {"anyOf": [{"type": "integer"}, {"type": "string"}]}},
            ]},
            "zones": {"type": "array", "items": {"type": "string"}},
            "checkpoints": {"type": "array", "items": {"type": "string"}},
            # "derivations": {"type": "array", "items": {"type": "string", "enum": [*offered(derivation), RECORDS]}},
            "derivations": {"type": "array", "items": {"type": "string", "enum": [*offered(derivation)]}},
        },
        "required": ["intent", "runs", "zones", "checkpoints", "derivations"],
        "additionalProperties": False,
    }


def known_checkpoints(records: Sequence[Record]) -> list[str]:
    """Every checkpoint_id in the records, in the order first seen, oldest run first."""
    return list(dict.fromkeys(c.checkpoint_id for record in records for c in record.checkpoints))


def known_zones(records: Sequence[Record]) -> list[str]:
    """Every zone in the records, from checkpoints and samples, in the order first seen."""
    zones = [c.zone for r in records for c in r.checkpoints] + [s.zone for r in records for s in r.sensor_samples]
    return list(dict.fromkeys(z for z in zones if z))


def layout(records: Sequence[Record]) -> str:
    """Each zone and its checkpoints, one line each, from the checkpoints' own zone.
    A zone only samples name has no checkpoints."""
    zones: dict = {zone: {} for zone in known_zones(records)}
    for record in records:
        for c in record.checkpoints:
            zones.setdefault(c.zone or "(no zone)", {})[c.checkpoint_id] = None
    return "\n".join(f"- {zone}: {', '.join(ids) or '(no checkpoints)'}" for zone, ids in zones.items())


def run_days(records: Sequence[Record]) -> list[str]:
    """Every day a run started on, YYYY-MM-DD in the run's own UTC offset, oldest run first."""
    return list(dict.fromkeys(r.start_time.date().isoformat() for r in records if r.start_time))


def describe(name: str, derivation: DerivationConfig) -> str:
    """One instance in words, from its derivations.yaml entry; or the records route."""
    if name == RECORDS:
        return (f"- {RECORDS}: the runs' own records, as written. The run's details, each checkpoint visit, "
                f"findings, sensor alerts, the event log, single sensor readings that can be trusted, and why "
                f"others were left out")
    entry = derivation.derivations[name]
    if entry["type"] == "run_set_difference":
        return (f"- {name}: difference. Which checkpoints only the run before the latest had, only the latest "
                f"run had, and both had, and how many of each")
    if entry["type"] == "run_date_range":
        return (f"- {name}: date range. Across all runs: the earliest and latest start time and which runs "
                f"they were, the days between them, how many runs, and how many days had a run and how many "
                f"did not; each day a run was taken on, its weekday, and its runs; and when each run started")
    if entry["type"] == "rank_top_n":
        return (f"- {name}: ranking. In each run, the {entry['n']} highest {entry['field_path']} readings "
                f"over its {entry['source']}, highest first, and which reading each was")
    if entry["type"] == "group_mean":
        zone = ", and the checkpoints in each zone" if entry["group_by"] == "zone" else ""
        return f"- {name}: mean. In each run, the mean of {entry['field_path']} over its {entry['source']}, per {entry['group_by']}{zone}"
    if entry["type"] == "group_max":
        zone = ", and the checkpoints in each zone" if entry["group_by"] == "zone" else ""
        return (f"- {name}: maximum. In each run, the highest {entry['field_path']} over its {entry['source']}, "
                f"per {entry['group_by']}, and which reading it was and when{zone}")
    if entry["type"] in ("condition_count", "proportion"):
        condition = derivation.conditions[entry["condition"]]
        items = entry["scope"].replace("_", " ")
        meets = f"{condition['field']} {condition['equals']}"
        if entry["type"] == "proportion":
            return f"- {name}: share. In each run, what share of {items} have {meets}, out of how many, and which ones"
        # Its derivations.yaml entry and named condition, as set: the exact items, field and value it counts.
        config = {key: value for key, value in entry.items() if key != "type"}
        return (f"- {name}: count. In each run, which {items} have {meets}, how many, out of how many {items}, "
                f"and which {items} were left out of the count and why\n"
                f"  config: {json.dumps(config)}; condition {entry['condition']}: {json.dumps(condition)}")
    compare = f"{entry['field_path']} {entry['direction']} {entry['threshold']}"
    if entry["scope"] == "checkpoints":
        return (f"- {name}: threshold comparison. In each run, which checkpoints (id and name) have {compare}, "
                "how many do and do not, and by how much")
    return f"- {name}: threshold comparison. In each run, whether {entry['scope']} has {compare}, and by how much"


def build_prompt(question: str, feedback: str, records: Sequence[Record], derivation: DerivationConfig) -> str:
    # lines = [describe(name, derivation) for name in [*offered(derivation), RECORDS]]
    lines = [describe(name, derivation) for name in [*offered(derivation)]]
    values = {
        "derivations": "\n".join(lines) or "(none)",
        "layout": layout(records) or "(none)",
        "days": ", ".join(run_days(records)) or "(none)",
        "question": question,
        "feedback": feedback,
    }
    return fill_prompt(PROMPT, values)


def parse_reply(reply: str, records: Sequence[Record], derivation: DerivationConfig, attempt: int) -> Route:
    """The model's JSON reply, checked, as a Route. Raises RouteError."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.strip())
    try:
        plan = json.loads(text)
    except json.JSONDecodeError:
        raise RouteError("the reply was not valid JSON") from None
    if not isinstance(plan, dict):
        raise RouteError("the reply must be one JSON object")
    if plan.get("intent") == "abstain":
        return Route(True, "the model abstained", [], [], [], attempt)
    if plan.get("intent") != "answer":
        raise RouteError('intent must be "answer" or "abstain"')
    if set(plan) != {"intent", "runs", "zones", "checkpoints", "derivations"}:
        raise RouteError("an answer has exactly the keys intent, runs, zones, checkpoints and derivations")

    runs = plan["runs"]
    if runs == "all":
        indices = list(range(len(records)))
    else:
        if not isinstance(runs, list) or not runs or not all((type(p) is int and p != 0) or isinstance(p, str) for p in runs):
            raise RouteError('runs must be "all" or a list of non-zero whole numbers and dates')
        # Two entries can name the same run.
        chosen = set()
        for p in runs:
            if isinstance(p, int):
                if not -len(records) <= p <= len(records):
                    return Route(True, f"no run at position {p}; {len(records)} recorded", [], [], [], attempt)
                # 1 is the oldest, -1 the most recent.
                chosen.add(p - 1 if p > 0 else len(records) + p)
                continue
            # A day, or a span first/last, matched on the day the run started in its own offset.
            first, _, last = p.partition("/")
            try:
                first, last = date.fromisoformat(first), date.fromisoformat(last or first)
            except ValueError:
                raise RouteError(f'{p} is not a date "YYYY-MM-DD" or a span "YYYY-MM-DD/YYYY-MM-DD"') from None
            if first > last:
                raise RouteError(f"{p} ends before it starts")
            matched = [i for i, r in enumerate(records) if r.start_time and first <= r.start_time.date() <= last]
            if not matched:
                return Route(True, f"no run started on {p}", [], [], [], attempt)
            chosen.update(matched)
        indices = sorted(chosen)
    run_ids = [records[i].run_id for i in indices]

    checkpoints = plan["checkpoints"]
    if not isinstance(checkpoints, list) or not all(isinstance(c, str) and c for c in checkpoints):
        raise RouteError("checkpoints must be a list of checkpoint ids")
    # Checked against every run: a checkpoint absent from the runs named is still a fair question.
    known = set(known_checkpoints(records))
    unknown = [c for c in checkpoints if c not in known]
    if unknown:
        return Route(True, f"{unknown[0]} is not in the records", [], [], [], attempt)

    zones = plan["zones"]
    if not isinstance(zones, list) or not all(isinstance(z, str) and z for z in zones):
        raise RouteError("zones must be a list of zone names")
    known = set(known_zones(records))
    unknown = [z for z in zones if z not in known]
    if unknown:
        return Route(True, f"{unknown[0]} is not a zone in the records", [], [], [], attempt)

    names = plan["derivations"]
    # allowed = [*offered(derivation), RECORDS]
    allowed = [*offered(derivation)]
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise RouteError("derivations must be a list of derivation names")
    bad = [n for n in names if n not in allowed]
    if bad:
        raise RouteError(f"unknown derivation {bad[0]}; use one of: {', '.join(allowed) or 'none'}")
    if not names:
        raise RouteError("an answer names at least one derivation; abstain if none fits")

    return Route(
        False, None, run_ids, list(dict.fromkeys(checkpoints)), list(dict.fromkeys(names)), attempt,
        list(dict.fromkeys(zones)),
    )
