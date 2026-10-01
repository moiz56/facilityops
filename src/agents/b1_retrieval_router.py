"""B-1 Analytical, retrieval router: the step before the router.

The model decides which kind of retrieval can answer the question, without
answering it:
  derivation  a figure worked out over the runs: goes to the router, then the
              derivation database
  lookup      values exactly as recorded, for named runs, zones or
              checkpoints: goes to the lookup database
  abstain     neither can answer it
Which runs, zones and checkpoints the question means is left to the step after.

The reply is held to REPLY_SCHEMA by the provider and checked again by
parse_reply; a bad one is sent back with the reason, up to max_attempts calls.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, fields

from agents.b1_analytical_router_derived import describe, offered
from agents.provider import Provider
from agents.schema import B1Config, DerivationConfig
from agents.utils import SENSOR_BLOCKS, fill_prompt

log = logging.getLogger(__name__)

RETRIEVALS = ("derivation", "lookup", "abstain")

REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "retrieval": {"type": "string", "enum": list(RETRIEVALS)},
        "reason": {"type": "string"},
    },
    "required": ["retrieval", "reason"],
    "additionalProperties": False,
}

# Written by hand. build_prompt fills these placeholders:
#   {derivations}  one line per derivation instance, as the router describes them
#   {fields}       the sensor fields, by block
#   {question}     the user's question
#   {feedback}     empty on the first call; why the previous reply was rejected after that
PROMPT = """You are the first step in answering a question about facility inspection
runs. You do not answer the question. You decide which kind of retrieval can
answer it from the records of the runs.

WHAT THE RECORDS HOLD
A robot drives a route of checkpoints. Checkpoints are stops, and each sits
inside a zone, an area of the site that holds one checkpoint or several. At
each checkpoint the robot records:
- whether it got there: status COMPLETED or MISSED
- the verdict: result_status PASS, FAIL or WARN
- sensor readings:
{fields}
- evidence images, some thermal, some annotated
A reading from a faulty or offline sensor, or from a checkpoint the robot
missed, is left out, and the reason it was left out is kept.

THE THREE KINDS
derivation: a figure worked out over the runs. These are held:
{derivations}
  Choose derivation whenever the question asks for a figure worked out from
  several values: how many, what share, an average, the highest or lowest, a
  ranking, above or below a limit, a difference between runs, when runs were
  taken. Choose it even when none of the above looks like a fit: the next
  step checks that, and says so when none holds it.

lookup: values exactly as recorded, for particular runs, zones or
checkpoints, with nothing combined or worked out:
  - a checkpoint's reading: "the temperature at <checkpoint> in the latest
    run", "the accelerometer values at <checkpoint>"
  - whether a reading was left out, and why: "why is there no PM2.5 at
    <checkpoint>"
  - a checkpoint's status or verdict: "was <checkpoint> completed", "what was
    <checkpoint>'s result"
  - which zone a checkpoint is in, which checkpoints are in a zone, the order
    of the route
  - the evidence a checkpoint captured: "which images were taken at
    <checkpoint>", "the thermal images at <checkpoint>"
  Choose lookup when the question asks for recorded values of the items it
  names, even when a derivation happens to carry that value too.

abstain: neither can answer it:
  - why something happened, what will happen, or what should be done
  - a judgement: better, worse, safe, normal, acceptable
  - anything the records do not hold: costs, maintenance, people, other sites

RULES
- Decide from what the question asks for, not from its wording.
- A figure worked out from several values is derivation; one recorded value
  of a named item is lookup. "The temperature at <checkpoint>" is lookup;
  "the highest temperature in <zone>" and "did <checkpoint> go over the
  limit" are derivation.
- A question that asks for a figure and a recorded value together is
  derivation.
- <checkpoint> and <zone> above stand for any checkpoint or zone name.
- Names of runs, dates, zones and checkpoints do not decide the kind. Whether
  they exist is checked in the next step: never abstain because a name is
  unfamiliar.

REPLY
JSON only:
  {"retrieval": "derivation" | "lookup" | "abstain", "reason": "<one short sentence>"}

{feedback}QUESTION
{question}
"""


class RetrievalError(ValueError):
    """The model's reply broke a rule. The message goes back to the model."""


@dataclass(frozen=True)
class Retrieval:
    """Which retrieval answers the question.

    kind:     derivation, lookup or abstain
    reason:   the model's one sentence on why
    attempts: model calls made
    """

    kind: str
    reason: str
    attempts: int


def classify_question(
    question: str, provider: Provider, config: B1Config, derivation: DerivationConfig,
    calls: list[dict] | None = None,
) -> Retrieval:
    """Pick the retrieval for one question. Raises ProviderError, or RetrievalError when no reply is valid after max_attempts.

    calls, if given, gets {attempt, reply, rejected} for every model call, rejected
    being why the reply was sent back, or None.
    """
    calls = [] if calls is None else calls
    feedback = ""
    for attempt in range(1, config.max_attempts + 1):
        prompt = build_prompt(question, feedback, derivation)
        reply = provider.complete(prompt, temperature=config.temperature, schema=REPLY_SCHEMA)
        try:
            retrieval = parse_reply(reply, attempt)
        except RetrievalError as error:
            log.warning("B-1 retrieval router attempt %d rejected: %s", attempt, error)
            calls.append({"attempt": attempt, "reply": reply, "rejected": str(error)})
            feedback = f"YOUR PREVIOUS REPLY WAS REJECTED: {error}\nFix that and reply again.\n\n"
            continue
        log.info("B-1 retrieval router attempt %d: %s", attempt, retrieval)
        calls.append({"attempt": attempt, "reply": reply, "rejected": None})
        return retrieval
    raise RetrievalError(f"no valid reply after {config.max_attempts} attempts; last: {feedback.strip()}")


def build_prompt(question: str, feedback: str, derivation: DerivationConfig) -> str:
    fields_by_block = "\n".join(
        f"    {block}: {', '.join(f.name for f in fields(block_type))}"
        for block, block_type in SENSOR_BLOCKS.items()
    )
    values = {
        "derivations": "\n".join(describe(name, derivation) for name in offered(derivation)) or "(none)",
        "fields": fields_by_block,
        "question": question,
        "feedback": feedback,
    }
    return fill_prompt(PROMPT, values)


def parse_reply(reply: str, attempt: int) -> Retrieval:
    """The model's JSON reply, checked, as a Retrieval. Raises RetrievalError."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.strip())
    try:
        plan = json.loads(text)
    except json.JSONDecodeError:
        raise RetrievalError("the reply was not valid JSON") from None
    if not isinstance(plan, dict) or set(plan) != {"retrieval", "reason"}:
        raise RetrievalError("the reply is one JSON object with exactly the keys retrieval and reason")
    if plan["retrieval"] not in RETRIEVALS:
        raise RetrievalError('retrieval must be "derivation", "lookup" or "abstain"')
    if not isinstance(plan["reason"], str) or not plan["reason"].strip():
        raise RetrievalError("reason must be one short sentence")
    return Retrieval(plan["retrieval"], plan["reason"].strip(), attempt)
