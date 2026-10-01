"""B-1 Analytical: answers a question over the records (section 8.1).

First the retrieval router (b1_retrieval_router) decides whether the question
needs a derivation, a lookup of values as recorded, or neither (abstain). A
lookup takes the same steps as a derivation, over the lookup database: the
lookup router (b1_analytical_router_lookup) names the tables, and the SQL
writer writes one SELECT for each with that table's prompt (lookups/).

A derivation takes three more model calls: the router names the runs, zones,
checkpoints and derivations (b1_analytical_router_derived); the SQL writer
writes one SELECT for each derivation type (routes/); the prose writer writes
a lead-in with no values (b1_analytical_prose). Code runs the SELECT through the guard, writes the rows
as a template, fills it through fill_slots and verifies the whole answer.

A bad reply, a failed query or a value that does not trace is sent back with
the reason, up to max_attempts calls. No lead-in: the facts alone
(DEGRADED_TEMPLATE_ONLY). An answer that does not verify is not shown.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, fields, is_dataclass, replace
from datetime import datetime

from agents.b1_analytical_prose import write_prose
from agents.b1_analytical_router_derived import RECORDS, Route, RouteError, describe, route_question
from agents.b1_analytical_router_lookup import route_lookup
from agents.b1_retrieval_router import RetrievalError, classify_question
from agents.database_derivation import QueryError, run_query
from agents.provider import Provider, ProviderError
from agents.lookups import PROMPTS as LOOKUP_PROMPTS
from agents.routes import PROMPTS
from agents.schema import (
    B1Config, B1Result, DerivationConfig, ExtendedRecord, Template, VerificationConfig,
)
from agents.slots import DERIVED_OUTPUT, KEY, PLACEHOLDER, fill_slots, resolve
from agents.utils import fill_prompt, format_timestamp
from agents.verification import verify_numeric

log = logging.getLogger(__name__)

ABSTAIN = "the records do not contain this"
# Heads the facts when the query returned no rows and the route's summary is
# shown instead. It says only what happened: an empty result can mean nothing
# exists or that the query's filter missed, and the summary is the stored result.
NO_ROWS = "The search for this question returned no rows. The stored result for the runs asked about, shown instead:"

# On: every shown value is traced to its place in the extended record, filled
# through fill_slots (so it has a citation) and the answer verified. Off: the
# query's cells go into the answer as they are, with no citations or
# verification; for debugging a route only.
FILL_AND_VERIFY = True

# The JSON shape the provider holds the SQL writer's reply to. An abstain fills sql too, with "".
SQL_REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["answer", "abstain"]},
        "sql": {"type": "string"},
    },
    "required": ["intent", "sql"],
    "additionalProperties": False,
}


class PlanError(ValueError):
    """The SQL writer's reply broke a rule. The message goes back to the model."""


def answer_question(
    question: str, extended: ExtendedRecord, conn, provider: Provider, config: B1Config,
    derivation: DerivationConfig, verification: VerificationConfig, lookup_conn=None,
) -> B1Result:
    """Answer one question from the derivation database (conn, from
    database_derivation.open_database) or the lookup database (lookup_conn,
    from database_lookup.open_lookup_database).

    Raises ValueError for an empty question or one longer than max_question_chars.
    """
    question = question.strip()
    if not question:
        raise ValueError("the question is empty")
    if len(question) > config.max_question_chars:
        raise ValueError(f"the question is longer than {config.max_question_chars} characters")

    # What each step was given and gave back, for B1Result.trace.
    trace: dict = {
        "question": question,
        "retrieval": {"calls": [], "result": None},
        "router": {"kind": None, "calls": [], "route": None},
        "sql_writer": [],
        "prose": {"calls": [], "lead": None},
        "answer_from": None,
    }

    try:
        retrieval = classify_question(question, provider, config, derivation, trace["retrieval"]["calls"])
    except ProviderError as error:
        return B1Result("PROVIDER_UNAVAILABLE", None, None, None, 1, str(error), trace=trace)
    except RetrievalError as error:
        return B1Result("REFUSED_UNVERIFIABLE", None, None, None, config.max_attempts,
                        f"retrieval router: {error}", trace=trace)
    trace["retrieval"]["result"] = asdict(retrieval)
    before = retrieval.attempts
    if retrieval.kind == "abstain":
        return abstained(f"retrieval router: {retrieval.reason}", extended, verification, before, trace)
    if retrieval.kind == "lookup":
        return answer_lookup(question, extended, lookup_conn, provider, config, derivation, verification, before,
                             trace)

    trace["router"]["kind"] = "derivation"
    try:
        route = route_question(question, extended.records, provider, config, derivation, trace["router"]["calls"])
    except ProviderError as error:
        return B1Result("PROVIDER_UNAVAILABLE", None, None, None, before + 1, str(error), trace=trace)
    except RouteError as error:
        return B1Result("REFUSED_UNVERIFIABLE", None, None, None, before + config.max_attempts, f"router: {error}",
                        trace=trace)
    trace["router"]["route"] = asdict(route)
    if route.abstain:
        return abstained(route.reason, extended, verification, before + route.attempts, trace)

    # One SQL writer call per derivation type; instances of one type share it.
    by_type: dict[str, list[str]] = {}
    for name in route.derivations:
        # records is a route of its own, not a derivations.yaml instance.
        kind = RECORDS if name == RECORDS else derivation.derivations[name]["type"]
        by_type.setdefault(kind, []).append(name)

    blocks, slots, values, notes, sqls = [], {}, {}, [], []
    attempts, retries = before + route.attempts, 0
    for n, (kind, names) in enumerate(by_type.items()):
        step = {"route": kind, "instances": names, "calls": [], "sql": None, "values": {}, "slots": {}}
        trace["sql_writer"].append(step)
        try:
            sql, part_lines, part_slots, part_values, part_notes, calls = write_and_run(
                question, route, PROMPTS[kind], names, n, extended, conn, provider, config, derivation, step["calls"],
            )
        except ProviderError as error:
            return B1Result("PROVIDER_UNAVAILABLE", None, None, None, attempts + 1, str(error), trace=trace)
        except PlanError as error:
            return B1Result("REFUSED_UNVERIFIABLE", None, None, None, attempts + config.max_attempts, str(error),
                            trace=trace)
        attempts += calls
        retries += calls - 1
        if sql is None:
            return abstained(f"the SQL writer abstained ({kind})", extended, verification, attempts, trace)
        step.update(sql=sql, values=part_values, slots=part_slots)
        sqls.append(sql)
        blocks.append("\n".join(part_lines))
        slots.update(part_slots)
        values.update(part_values)
        notes += part_notes

    return finish(question, route, blocks, slots, values, notes, sqls, attempts, retries,
                  extended, provider, config, derivation, verification, trace)


def finish(
    question: str, route, blocks: list[str], slots: dict[str, str], values: dict[str, object], notes: list[str],
    sqls: list[str], attempts: int, retries: int, extended: ExtendedRecord, provider: Provider, config: B1Config,
    derivation: DerivationConfig, verification: VerificationConfig, trace: dict,
) -> B1Result:
    """From the SQL writer's rows to the answer, for a derivation or a lookup:
    the rows as a template, filled from the records, a lead-in above them,
    and the whole answer verified. route is the Route or LookupRoute. notes
    are the code-written texts the rows hold (a template's TEXT_COLUMNS), in
    order: they are shown uncited and their spans are not verified."""
    # Code writes the answer's facts: the rows as a template, filled from the
    # records, so every value and its citation comes from code.
    sql = "\n\n".join(sqls)
    template = Template("b1_answer", "\n\n".join(blocks), slots)
    if FILL_AND_VERIFY:
        try:
            filled = fill_slots(template, extended, derivation)
        except ValueError as error:
            # A traced slot that does not print, e.g. a value with no decimals entry.
            log.warning("B-1 answer could not be filled: %s", error)
            return B1Result("REFUSED_UNVERIFIABLE", None, None, sql, attempts,
                            f"the answer could not be filled: {error}", template, trace)
        facts, placed = filled.text, filled.slots
    else:
        facts, placed = PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), template.text), ()

    # The model writes only a lead-in above the facts, with no values in it.
    # If it fails, the facts alone are the answer (DEGRADED_TEMPLATE_ONLY).
    lead, calls, why = write_prose(question, facts, provider, config, trace["prose"]["calls"])
    trace["prose"]["lead"] = lead
    attempts += calls
    retries += max(calls - 1, 0)
    status = "OK" if lead is not None else "DEGRADED_TEMPLATE_ONLY"
    answer_from = "prose" if lead is not None else "rows"
    answer = f"{lead}\n\n{facts}" if lead is not None else facts
    offset = len(answer) - len(facts)
    placed = [replace(slot, span=(slot.span[0] + offset, slot.span[1] + offset)) for slot in placed]

    if not FILL_AND_VERIFY:
        trace["answer_from"] = answer_from
        output = {"answer": answer, "citations": [], "records_consulted": route.run_ids}
        return B1Result(status, output, None, sql, attempts, why or "fill and verification are off", template, trace)

    exempt = [(start + offset, end + offset) for start, end in note_spans(facts, notes)]
    result = replace(verify_numeric(answer, extended, verification, placed, exempt), regeneration_attempts=retries)
    if not result.passed:
        log.warning("B-1 answer failed verification: %s", result.failures)
        return B1Result("REFUSED_UNVERIFIABLE", None, result, sql, attempts, "the answer did not verify", template, trace)
    trace["answer_from"] = answer_from

    # The verifier credits whichever derivation first holds a matching number;
    # the slots say which one each value actually came from.
    derived = [m.group(1) for slot in placed if (m := re.match(r"derived\.values\.(\w+)", slot.source_field))]
    result = replace(result, derived_values_used=list(dict.fromkeys(derived)))
    output = {
        "answer": answer,
        "citations": [{"claim_span": list(slot.span), "source_field": slot.source_field} for slot in placed],
        "records_consulted": route.run_ids,
    }
    return B1Result(status, output, result, sql, attempts, why, template, trace)


def note_spans(facts: str, notes: list[str]) -> list[tuple[int, int]]:
    """Where each note sits in the facts. Notes are written in order, so each
    is looked for after the one before."""
    spans, at = [], 0
    for note in notes:
        start = facts.find(note, at)
        if start < 0:
            continue
        spans.append((start, start + len(note)))
        at = start + len(note)
    return spans


def run_of(path: str, extended: ExtendedRecord) -> str | None:
    """The run a cited path belongs to, or None for a value about all runs (D7, D8)."""
    if m := re.match(r"records\[(\d+)\]", path):
        return extended.records[int(m.group(1))].run_id
    if m := DERIVED_OUTPUT.match(path):
        output, _, _ = resolve(m.group(0), extended)
        if isinstance(output, dict):
            return output.get("run_id")
    return None


def answer_lookup(
    question: str, extended: ExtendedRecord, conn, provider: Provider, config: B1Config,
    derivation: DerivationConfig, verification: VerificationConfig, attempts: int, trace: dict,
) -> B1Result:
    """A question the retrieval router sent to lookup: the lookup router names
    the tables, the SQL writer writes one SELECT for each over the lookup
    database (conn), and finish writes the answer. attempts is the model calls
    made before this."""
    trace["router"]["kind"] = "lookup"
    try:
        route = route_lookup(question, extended.records, provider, config, trace["router"]["calls"])
    except ProviderError as error:
        return B1Result("PROVIDER_UNAVAILABLE", None, None, None, attempts + 1, str(error), trace=trace)
    except RouteError as error:
        return B1Result("REFUSED_UNVERIFIABLE", None, None, None, attempts + config.max_attempts,
                        f"lookup router: {error}", trace=trace)
    trace["router"]["route"] = asdict(route)
    attempts += route.attempts
    if route.abstain:
        return abstained(route.reason, extended, verification, attempts, trace)
    if conn is None:
        return B1Result("REFUSED_UNVERIFIABLE", None, None, None, attempts, "lookup: no lookup database", trace=trace)
    missing = [table for table in route.tables if table not in LOOKUP_PROMPTS]
    if missing:
        return B1Result("REFUSED_UNVERIFIABLE", None, None, None, attempts,
                        f"lookup: no template for the {missing[0]} table yet", trace=trace)

    # One SQL writer call per table.
    blocks, slots, values, notes, sqls = [], {}, {}, [], []
    retries = 0
    for n, table in enumerate(route.tables):
        step = {"route": table, "instances": [], "calls": [], "sql": None, "values": {}, "slots": {}}
        trace["sql_writer"].append(step)
        try:
            sql, part_lines, part_slots, part_values, part_notes, calls = write_and_run(
                question, route, LOOKUP_PROMPTS[table], [], n, extended, conn, provider, config, derivation,
                step["calls"],
            )
        except ProviderError as error:
            return B1Result("PROVIDER_UNAVAILABLE", None, None, None, attempts + 1, str(error), trace=trace)
        except PlanError as error:
            return B1Result("REFUSED_UNVERIFIABLE", None, None, None, attempts + config.max_attempts, str(error),
                            trace=trace)
        attempts += calls
        retries += calls - 1
        if sql is None:
            return abstained(f"the SQL writer abstained ({table})", extended, verification, attempts, trace)
        step.update(sql=sql, values=part_values, slots=part_slots)
        sqls.append(sql)
        blocks.append("\n".join(part_lines))
        slots.update(part_slots)
        values.update(part_values)
        notes += part_notes

    return finish(question, route, blocks, slots, values, notes, sqls, attempts, retries,
                  extended, provider, config, derivation, verification, trace)


def abstained(
    reason: str | None, extended: ExtendedRecord, verification: VerificationConfig, attempts: int, trace: dict,
) -> B1Result:
    trace["answer_from"] = "abstain"
    result = verify_numeric(ABSTAIN, extended, verification)
    return B1Result("OK", {"answer": ABSTAIN, "citations": [], "records_consulted": []}, result, None, attempts,
                    reason, trace=trace)


def write_and_run(
    question: str, route, module, names: list[str], n: int, extended: ExtendedRecord, conn,
    provider: Provider, config: B1Config, derivation: DerivationConfig, calls: list[dict] | None = None,
) -> tuple[str | None, list[str], dict[str, str], dict[str, object], list[str], int]:
    """Ask the SQL writer for the query in module's prompt (a derivation route
    or a lookup table) and run it on conn: (sql, lines, slots, values, notes,
    calls made).
    route is the Route or LookupRoute; names the derivation instances, or []
    for a lookup.

    sql is None when the model abstained. Raises ProviderError, or PlanError
    when no reply passes after max_attempts. calls, if given, gets {attempt,
    reply, rejected} for every model call, and summary_sql on the accepted one
    when its query returned no rows and code ran the route's summary instead.
    """
    calls = [] if calls is None else calls
    feedback = ""
    for attempt in range(1, config.max_attempts + 1):
        prompt = build_prompt(module, question, route, names, derivation, feedback)
        reply = provider.complete(prompt, temperature=config.temperature, schema=SQL_REPLY_SCHEMA)
        try:
            sql = parse_reply(reply)
            if sql is None:
                calls.append({"attempt": attempt, "reply": reply, "rejected": None})
                return None, [], {}, {}, [], attempt
            log.info("B-1 SQL attempt %d: %s", attempt, sql)
            lines, slots, values, notes, summary = build_lines(
                sql, conn, route, module, names, n, extended, config, derivation,
            )
            calls.append({"attempt": attempt, "reply": reply, "rejected": None, "summary_sql": summary})
            return sql, lines, slots, values, notes, attempt
        except ValueError as error:
            # PlanError, QueryError, or a shown value that does not trace to the records.
            log.warning("B-1 SQL attempt %d rejected: %s", attempt, error)
            calls.append({"attempt": attempt, "reply": reply, "rejected": str(error)})
            feedback = f"YOUR PREVIOUS REPLY WAS REJECTED: {error}\nFix that and reply again.\n\n"
    raise PlanError(f"no SQL reply passed after {config.max_attempts} attempts; last: {feedback.strip()}")


def quote(ids: list[str]) -> str:
    """ids as SQL string literals, comma separated."""
    return ", ".join("'" + i.replace("'", "''") + "'" for i in ids)


def build_prompt(module, question: str, route: Route, names: list[str], derivation: DerivationConfig, feedback: str) -> str:
    # A module whose tables depend on config (D4: one per instance) builds them for the routed names.
    schema = module.tables(names, derivation) if hasattr(module, "tables") else module.TABLES
    values = {
        "schema": schema,
        "instances": "\n".join(describe(name, derivation) for name in names),
        "run_ids": quote(route.run_ids),
        "checkpoints": quote(route.checkpoints) or "(any)",
        "zones": quote(route.zones) or "(any)",
        "question": question,
        "feedback": feedback,
    }
    return fill_prompt(module.PROMPT, values)


def parse_reply(reply: str) -> str | None:
    """The SQL from the model's JSON reply, or None when it abstains. Raises PlanError."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.strip())
    try:
        plan = json.loads(text)
    except json.JSONDecodeError:
        raise PlanError("the reply was not valid JSON") from None
    if not isinstance(plan, dict):
        raise PlanError("the reply must be one JSON object")
    if plan.get("intent") == "abstain":
        return None
    sql = plan.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise PlanError('the reply must be {"intent": "answer", "sql": "SELECT ..."} or {"intent": "abstain", "sql": ""}')
    return sql.strip()


def build_lines(
    sql: str, conn, route: Route, module, names: list[str], n: int, extended: ExtendedRecord,
    config: B1Config, derivation: DerivationConfig,
) -> tuple[list[str], dict[str, str], dict[str, object], list[str], str | None]:
    """Run the query; the answer's facts as a template, grouped by run.

    Each run's id is a heading line, "Run {{ slot }}", written when the run
    changes; under it one line per row, "- column {{ slot }}, ...". Rows with
    no run_id (D7, D8) are the lines alone.

    Returns (lines, slots, values, notes, summary): slots maps each slot to
    its path in the extended record (only when FILL_AND_VERIFY is on), values
    to the cell the query returned, notes are the cells of the module's
    TEXT_COLUMNS, written as they are with no slot (text code wrote that no
    field holds, e.g. a lookup's exclusion reason), and summary is the
    summary query code ran, or None. Slot ids are q<query>_r<row>_<column>.

    When it returns no rows, the module's SUMMARY_SQL is run instead, so the
    answer states the stored run-level result (e.g. exceeds_count 0) rather
    than only that nothing matched.
    """
    columns, rows = run_query(conn, sql, config.max_rows, config.query_timeout_seconds)

    bad = [c for c in columns if not re.fullmatch(r"[A-Za-z_]+", c)]
    if bad:
        raise PlanError(f"column names must be plain words with no digits, got: {', '.join(bad)}")
    if len(set(columns)) != len(columns):
        raise PlanError("every column needs its own name; alias repeated ones")
    shown = [c for c in columns if not is_path(c) and c not in ORDER_ONLY]
    if not shown:
        raise PlanError("the query shows no values, only path columns")
    if not any(is_path(c) for c in columns):
        raise PlanError("select the path column of each table a value comes from")
    # D7 and D8 give one output for all runs (PER_RUN = False), so their rows have no run_id.
    if getattr(module, "PER_RUN", True) and "run_id" not in columns:
        raise PlanError("select run_id, so each row says which run it is about")
    lines, summary = [], None
    if not rows:
        summary = module.summary_sql(names) if hasattr(module, "summary_sql") else module.SUMMARY_SQL
        summary = summary.replace("{instances}", quote(names)).replace("{run_ids}", quote(route.run_ids))
        columns, rows = run_query(conn, summary, config.max_rows, config.query_timeout_seconds)
        shown = [c for c in columns if not is_path(c) and c not in ORDER_ONLY]
        lines.append(NO_ROWS)

    slots, values, notes = {}, {}, []
    text_columns = getattr(module, "TEXT_COLUMNS", ())
    run = None
    for r, row in enumerate(rows):
        cells = dict(zip(columns, row))
        # The router's runs and checkpoints are the only ones the answer may use.
        if "run_id" in cells and cells["run_id"] not in route.run_ids:
            raise PlanError(f"the query returned run {cells['run_id']}, which is not in the list; filter run_id")
        if route.checkpoints and cells.get("checkpoint_id") not in (None, *route.checkpoints):
            raise PlanError(f"the query returned {cells['checkpoint_id']}, which is not in the list; filter checkpoint_id")
        bases = [cells[c] for c in columns if is_path(c) and cells[c]]
        parts = []
        for c in shown:
            if cells[c] is None or cells[c] == "":
                continue   # e.g. reason on an OK run, delta on a NOT_COMPUTABLE checkpoint
            if c == "run_id" and cells[c] == run:
                continue   # a run's id heads its rows once
            if c in text_columns:
                notes.append(str(cells[c]))
                parts.append(f"{c.replace('_', ' ')} {cells[c]}")
                continue
            slot_id = f"q{n}_r{r}_{c}"
            values[slot_id] = cells[c]
            if FILL_AND_VERIFY:
                slots[slot_id] = trace(cells[c], c, bases, extended, derivation)
                # A yes/no flag has no printed form (fill_slots refuses it), so
                # the query is sent back now, while the SQL writer can still fix it.
                if isinstance(resolve(slots[slot_id].removesuffix(KEY), extended)[0], bool):
                    raise PlanError(
                        f"{c} is a yes/no flag and cannot be shown; do not select it, "
                        f"filter on it in WHERE instead (e.g. {c} = 1)"
                    )
            if c == "run_id":
                run = cells[c]
                lines.append(f"Run {{{{ {slot_id} }}}}")
                continue
            # "exceeds count", not exceeds_count: the verifier would take a
            # name with an underscore for an id to check against the records.
            parts.append(f"{c.replace('_', ' ')} {{{{ {slot_id} }}}}")
        if parts:
            lines.append("- " + ", ".join(parts))
    return lines, slots, values, notes, summary


# Columns a query may select only to sort by, e.g. a UNION ALL's ORDER BY,
# which can only use result columns. Never shown in the answer.
ORDER_ONLY = ("run_order", "position")


def is_path(column: str) -> bool:
    """Whether a result column is a row's path, not a value to show."""
    return column == "path" or column.startswith("path_")


def trace(cell: object, column: str, bases: list[str], extended: ExtendedRecord, config: DerivationConfig) -> str:
    """The path in the extended record that holds cell, under one of the row's path columns.

    In order:
      1. <base>.<column>: the column is a field there (delta, run_id, ...)
      2. <base> itself: the path is the value (an item_id at matching_ids[2])
      3. a key along <base>, marked KEY: the value is a name the record is
         keyed by, not a value in it (an instance, a zone, D7's only_in_a)
      4. for a column the query renamed, the one field under the bases
         holding exactly that value
    Raises ValueError if there is none, or more than one: a computed or
    renamed-beyond-recognition value.
    """
    for base in bases:
        value, _, _ = resolve(f"{base}.{column}", extended)
        if value is not None and same(cell, value, config):
            return f"{base}.{column}"
    for base in bases:
        value, _, _ = resolve(base, extended)
        if value is not None and same(cell, value, config):
            return base
    for base in bases:
        # The deepest key on the path that is the cell: derived.values.<instance>[0].groups.<zone>
        keys = [m for m in re.finditer(r"\.([^.\[\]]+)", base) if m.group(1) == str(cell)]
        if keys:
            return base[:keys[-1].end()] + KEY
    matches = [
        f"{base}.{key}"
        for base in bases
        for key, value in items(resolve(base, extended)[0])
        if not isinstance(value, bool) and same(cell, value, config)
    ]
    if len(matches) == 1:
        return matches[0]
    raise ValueError(
        f"column {column} does not trace to one stored value under the row's path columns; "
        "select stored columns under their own names, never computed values"
    )


def items(obj: object) -> list[tuple[str, object]]:
    """The fields of a dict or dataclass, as (name, value)."""
    if isinstance(obj, dict):
        return list(obj.items())
    if is_dataclass(obj):
        return [(f.name, getattr(obj, f.name)) for f in fields(obj)]
    return []


def same(cell: object, value: object, config: DerivationConfig) -> bool:
    """Whether a database cell is this extended-record value, as the database stores it."""
    if isinstance(value, bool):
        return cell == int(value)
    if isinstance(value, datetime):
        return cell == format_timestamp(value, config)
    if isinstance(value, (list, tuple)) and value and all(isinstance(v, str) for v in value):
        # Stored comma separated (D5 tied_with), the way fill_slots prints a list.
        return cell == ", ".join(value)
    if isinstance(value, (dict, list, tuple)):
        return False
    return cell == value
