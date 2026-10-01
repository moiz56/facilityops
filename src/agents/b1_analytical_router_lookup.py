"""B-1 Analytical, lookup router: the router for questions the retrieval
router sends to lookup (values as recorded, nothing worked out).

The model names what the question is about (runs, zones, checkpoints, sensor
fields and lookup tables) without answering it. Code checks every name, turns
run positions and dates into run_ids the same way the derivation router does
(resolve_runs, check_places), and hands the result on. Each table in
database_lookup is one route.

The reply is held to reply_schema by the provider and checked again by
parse_reply; a bad one is sent back with the reason, up to max_attempts calls.
A run, date, zone or checkpoint the records do not hold is not a bad reply:
the question is about something that is not there, so the route abstains.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, fields
from typing import Sequence

from common.schema import Record
from agents.b1_analytical_router_derived import (
    PLACES, RouteError, check_places, layout, resolve_runs, run_days,
)
from agents.database_lookup import TABLES
from agents.provider import Provider
from agents.schema import B1Config
from agents.utils import SENSOR_BLOCKS, fill_prompt

log = logging.getLogger(__name__)

# What each table answers, in the prompt. Keyed by database_lookup.TABLES.
DESCRIPTIONS = {
    "zones": (
        "the zones each run's route passed through, in route order. For \"which zones did the route "
        "cover\", \"which zone comes first\""
    ),
    "checkpoints": (
        "each run's checkpoints: the zone each is in, its place in the route order, whether the robot "
        "got there (status COMPLETED or MISSED) and its verdict (result_status PASS, FAIL or WARN; "
        "empty when the checkpoint was missed, which status shows). For \"was <checkpoint> completed\", "
        "\"what was <checkpoint>'s "
        "result\", \"which zone is <checkpoint> in\", \"which checkpoints are in <zone>\", \"what "
        "order does the route take\""
    ),
    "checkpoint_readings": (
        "every sensor reading taken at each checkpoint, one per field, exactly as recorded; or, for a "
        "reading that could not be trusted (a faulty or offline sensor, or a missed checkpoint), that "
        "it was left out and why. For \"the temperature at <checkpoint>\", \"the accelerometer values "
        "at <checkpoint>\", \"why is there no PM2.5 reading at <checkpoint>\""
    ),
    "evidence": (
        "the image files each checkpoint captured, as recorded: its evidence images, annotated images, "
        "and images of findings made there. For \"which images were taken at <checkpoint>\", \"the "
        "thermal images at <checkpoint>\""
    ),
}
assert set(DESCRIPTIONS) == set(TABLES), "every lookup table needs a description, and only those"

# Written by hand. build_prompt fills these placeholders:
#   {tables}    one line per lookup table, from DESCRIPTIONS
#   {fields}    the sensor fields, by block
#   {layout}    every zone in the records and the checkpoints in it
#   {days}      every day a run started on
#   {places}    how to name the runs, zones and checkpoints (the derivation router's PLACES)
#   {question}  the user's question
#   {feedback}  empty on the first call; why the previous reply was rejected after that
PROMPT = """You are the second step in looking up what facility inspections recorded.
An earlier step decided this question asks for values as they were recorded:
a reading, a status or verdict, a zone, or an image.

HOW THIS WORKS
An inspection robot drives a route of checkpoints around a facility. Each time
it does that is a run. At every checkpoint it records whether the stop was
completed and whether it passed, takes sensor readings and captures images.
Several runs are loaded, oldest first.

This question asks for values as they were recorded, not for anything worked
out from them. You say which runs, zones, checkpoints, readings and tables it
is about; code then fetches exactly those rows and writes the answer. So you
only need to point at the right things.

HOW THE DATA IS ORGANISED
Everything sits in one hierarchy, each level inside the one above:

  run                     one drive of the route, e.g. the latest run
  └─ zone                 an area of the route
     └─ checkpoint        one stop in that zone
        ├─ status         whether the robot got there: COMPLETED or MISSED
        ├─ result_status  the verdict: PASS, FAIL or WARN
        ├─ readings       by sensor block, then field, e.g.
        │                 environment → temperature_c; each one either its
        │                 value, or left out with the reason why
        └─ evidence       the images captured there: evidence images,
                          annotated images, images of findings

- Runs are loaded oldest first. Each run has its own zones and checkpoints: a
  checkpoint's status, readings and images belong to one run, so the same
  checkpoint has different values in different runs.
- A zone can hold several checkpoints, or just one; then the two often share
  a name. A checkpoint is in exactly one zone.
- In these tables a reading or image is always at a checkpoint. A question
  about a zone covers the checkpoints in it; a question about a run covers
  every zone.
- Each table below holds one level of this, or one branch of a checkpoint.
- In the examples below, <checkpoint> and <zone> stand for any checkpoint or
  zone name. The real names are listed further down.

WHAT CAN BE LOOKED UP
{tables}

Nothing here is worked out: there are no counts, averages, highest or lowest
values, rankings or comparisons. Rows are listed as recorded.

READINGS
The sensor fields, by block:
{fields}
Questions use everyday words for readings:
- vibration or shaking is accelerometer
- temperature, heat, how hot or cold, humidity and pressure are environment
- particulate, particles, dust, air quality, PM2.5 or PM10 is particulate
A reading that could not be trusted is still asked about the usual way: the
answer says it was left out and why. That is not a reason to abstain.

Zones in the records, and the checkpoints in each (zone: checkpoints):
{layout}

Days the runs started on:
{days}

HOW TO REPLY
Reply with JSON only, one of:
  {"intent": "answer", "runs": ..., "zones": [...], "checkpoints": [...], "fields": [...], "tables": [...]}
  {"intent": "abstain", "runs": "all", "zones": [], "checkpoints": [], "fields": [], "tables": []}

{places}
fields: the readings the question asks for, as written in READINGS. A field
  ("temperature" is temperature_c, "PM2.5" is pm2_5), or a block for all of
  its fields ("the accelerometer values" is accelerometer). [] when it asks
  for every reading, or for none.
tables: the names, from WHAT CAN BE LOOKED UP, of the tables the question needs. At
  least one; more only when it asks for more than one kind of thing ("the
  status and the temperature at <checkpoint>" is checkpoints and
  checkpoint_readings).

WHEN TO ABSTAIN
- No table above holds what the question asks for.
- It asks for something worked out from the values: how many, an average,
  the highest or lowest, a ranking, a comparison or a change.
- It asks why something happened, what will happen, what should be done, or
  for a judgement (better, worse, safe, normal).

{feedback}QUESTION
{question}
"""


@dataclass(frozen=True)
class LookupRoute:
    """What a lookup question is about, checked against the records.

    abstain:     True when the model abstained, or named a run, date, zone or
                 checkpoint the records do not hold
    reason:      why, when abstain is True
    run_ids:     the runs named, oldest first
    zones:       the zones named; [] means no zone filter
    checkpoints: the checkpoint_ids named; [] means no checkpoint filter
    fields:      the sensor blocks and fields named; [] means every field
    tables:      the lookup tables the question needs
    attempts:    model calls made
    """

    abstain: bool
    reason: str | None
    run_ids: list[str]
    zones: list[str]
    checkpoints: list[str]
    fields: list[str]
    tables: list[str]
    attempts: int


def route_lookup(
    question: str, records: Sequence[Record], provider: Provider, config: B1Config,
    calls: list[dict] | None = None,
) -> LookupRoute:
    """Route one lookup question over the records, oldest first. Raises ProviderError, or RouteError when no reply is valid after max_attempts.

    calls, if given, gets {attempt, reply, rejected} for every model call, rejected
    being why the reply was sent back, or None.
    """
    calls = [] if calls is None else calls
    feedback = ""
    for attempt in range(1, config.max_attempts + 1):
        prompt = build_prompt(question, feedback, records)
        reply = provider.complete(prompt, temperature=config.temperature, schema=reply_schema())
        try:
            route = parse_reply(reply, records, attempt)
        except RouteError as error:
            log.warning("B-1 lookup router attempt %d rejected: %s", attempt, error)
            calls.append({"attempt": attempt, "reply": reply, "rejected": str(error)})
            feedback = f"YOUR PREVIOUS REPLY WAS REJECTED: {error}\nFix that and reply again.\n\n"
            continue
        log.info("B-1 lookup router attempt %d: %s", attempt, route)
        calls.append({"attempt": attempt, "reply": reply, "rejected": None})
        return route
    raise RouteError(f"no valid reply after {config.max_attempts} attempts; last: {feedback.strip()}")


def sensor_names() -> list[str]:
    """Every block, then every field, a reply's fields may name."""
    return [*SENSOR_BLOCKS, *(f.name for block_type in SENSOR_BLOCKS.values() for f in fields(block_type))]


def reply_schema() -> dict:
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
            "fields": {"type": "array", "items": {"type": "string", "enum": sensor_names()}},
            "tables": {"type": "array", "items": {"type": "string", "enum": list(TABLES)}},
        },
        "required": ["intent", "runs", "zones", "checkpoints", "fields", "tables"],
        "additionalProperties": False,
    }


def build_prompt(question: str, feedback: str, records: Sequence[Record]) -> str:
    values = {
        "tables": "\n".join(f"- {name}: {DESCRIPTIONS[name]}" for name in TABLES),
        "fields": "\n".join(
            f"- {block}: {', '.join(f.name for f in fields(block_type))}"
            for block, block_type in SENSOR_BLOCKS.items()
        ),
        "layout": layout(records) or "(none)",
        "days": ", ".join(run_days(records)) or "(none)",
        "places": PLACES,
        "question": question,
        "feedback": feedback,
    }
    return fill_prompt(PROMPT, values)


def parse_reply(reply: str, records: Sequence[Record], attempt: int) -> LookupRoute:
    """The model's JSON reply, checked, as a LookupRoute. Raises RouteError."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.strip())
    try:
        plan = json.loads(text)
    except json.JSONDecodeError:
        raise RouteError("the reply was not valid JSON") from None
    if not isinstance(plan, dict):
        raise RouteError("the reply must be one JSON object")
    if plan.get("intent") == "abstain":
        return LookupRoute(True, "the model abstained", [], [], [], [], [], attempt)
    if plan.get("intent") != "answer":
        raise RouteError('intent must be "answer" or "abstain"')
    if set(plan) != {"intent", "runs", "zones", "checkpoints", "fields", "tables"}:
        raise RouteError("an answer has exactly the keys intent, runs, zones, checkpoints, fields and tables")

    run_ids, reason = resolve_runs(plan["runs"], records)
    if reason is None:
        reason = check_places(plan["zones"], plan["checkpoints"], records)
    if reason:
        return LookupRoute(True, reason, [], [], [], [], [], attempt)

    names = plan["fields"]
    allowed = sensor_names()
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise RouteError("fields must be a list of sensor blocks and fields")
    bad = [n for n in names if n not in allowed]
    if bad:
        raise RouteError(f"unknown field {bad[0]}; use one of: {', '.join(allowed)}")

    tables = plan["tables"]
    if not isinstance(tables, list) or not all(isinstance(t, str) for t in tables):
        raise RouteError("tables must be a list of table names")
    bad = [t for t in tables if t not in TABLES]
    if bad:
        raise RouteError(f"unknown table {bad[0]}; use one of: {', '.join(TABLES)}")
    if not tables:
        raise RouteError("an answer names at least one table; abstain if none fits")

    return LookupRoute(
        False, None, run_ids, list(dict.fromkeys(plan["zones"])), list(dict.fromkeys(plan["checkpoints"])),
        list(dict.fromkeys(names)), list(dict.fromkeys(tables)), attempt,
    )
