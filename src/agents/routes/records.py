"""The SQL writer's prompt for the records route: what each run recorded.

Not a derivation and not a fallback. The router names it, as "records", when
the question asks for details a run recorded: the run's own fields, what
happened at a checkpoint, findings, sensor alerts, the event log, a single
eligible reading, or why a reading is missing. Anything a derivation works out
(a count, share, mean, maximum, ranking, threshold comparison, run difference
or date range) is routed to that derivation instead.

The SQL writer asks the model for one SELECT over runs and the rec_ tables
only. Nothing here calls the model or runs the query.
"""

from agents.database_derivation import RECORDS, RUNS

TYPE = "records"   # the name the router gives this route; not a derivations.yaml type
TABLES = (RUNS + RECORDS).strip()

# Run by code, not the model, when the model's query returns no rows: the runs
# that were searched, so the answer says which runs hold nothing that matched.
# {run_ids} is filled with the quoted, comma-separated run ids.
SUMMARY_SQL = """SELECT r.run_id, r.path AS path_r
FROM runs r
WHERE r.run_id IN ({run_ids})
ORDER BY r.run_order"""

# Written by hand. The SQL writer fills these placeholders:
#   {schema}       TABLES
#   {run_ids}      the run_ids the router resolved, oldest first, quoted and comma separated
#   {checkpoints}  the checkpoint_ids the router resolved, the same way, or "(any)" when none
#   {question}     the user's question
#   {feedback}     empty on the first call; why the previous reply was rejected after that
# The reply the SQL writer expects, JSON only:
#   {"intent": "answer", "sql": "SELECT ..."}   or   {"intent": "abstain", "sql": ""}
PROMPT = """You write one SQLite SELECT that fetches the answer to a question from
the tables below. You do not answer the question and you never compute a value:
code writes the answer from the rows your query returns.

The question is about what inspection runs recorded. An inspection robot
drives a route of checkpoints; each pass is a run. The tables hold each run's
own records as they were written: the run's details, every checkpoint visit,
the findings, the sensor alerts, the event log, the sensor readings that can be
trusted, and why other readings were left out.

TABLES
{schema}

WHAT TO READ
run_id in: {run_ids}
checkpoint_id in: {checkpoints}
Filter to exactly these runs. When checkpoints are listed, filter to them on
the column that names a checkpoint in the table you read: checkpoint_id in
rec_checkpoints, rec_findings and rec_events; nearest_checkpoint_id in
rec_sensor_alerts; source_id (with kind = 'checkpoint') in rec_readings; scope
in rec_exclusions (also keep scope = '__run__', which covers every checkpoint).

HOW TO READ THE TABLES
- runs: one row per run, with its own details (facility, run_status,
  final_status, start and end time, duration, progress). The *_checkpoints and
  finding_count columns are counts the robot declared, not counted from the
  run's lists; if you select one, the answer states it as what the run
  reported.
- rec_checkpoints: one row per checkpoint visit, in route order. status says
  whether the robot got there (COMPLETED or MISSED); result_status is the
  verdict (PASS, FAIL or WARN); the two are independent. missed_reason is set
  when it was MISSED. observed, expected_text and notes are as recorded. A
  checkpoint visited twice has two rows.
- rec_findings: one row per finding. A finding with status 'abstained' is one
  the detector would not call: it needs human review.
- rec_sensor_alerts: threshold breaches logged during the run. The nearest
  checkpoint is where the robot was closest, not where the cause is.
- rec_events: the run's own log, in order.
- rec_readings: sensor readings that passed the exclusion rule, one row per
  reading and field (field_path, e.g. environment.temperature_c). kind
  'checkpoint' is the reading taken at a checkpoint stop; kind 'sample' is the
  continuous telemetry, with many rows per run. A reading that is not here was
  left out: never say it was zero. Show value and timestamp (and a sample's
  zone) with g.path AS path_g and g.path_item. Filter on source_id, kind and
  field_path, but never select them: say which checkpoint a reading is from by
  joining rec_checkpoints and selecting c.checkpoint_id with c.path AS path_c.
- rec_exclusions: why readings were left out, per checkpoint (scope) and
  sensor block, or for the whole run (scope '__run__'). Use it when a reading
  the question asks for is missing.
- Sensor words: vibration is accelerometer, temperature, humidity and
  pressure are environment, particulate or dust is particulate.

WHAT THIS ROUTE DOES NOT DO
No counting, averaging, maximum, ranking or comparison is worked out here;
those are separate figures. If the question needs a number that is not stored
in a column (how many, the average, the highest), abstain, unless a declared
count in runs is exactly what it asks for. Listing the rows that match is fine
("which checkpoints were missed" lists them).

HOW MANY ROWS
Fetch exactly as many rows as the question asks for, no more; at most 500 can
come back, so always filter.
- A list ("which findings need review"): every matching row; no LIMIT.
- One item ("the last event", "the first finding"): pick it per run with a
  subquery on position in the same run, so each run gets its own:
    AND e.position = (SELECT e2.position FROM rec_events e2
                      WHERE e2.run_id = e.run_id ORDER BY e2.position DESC LIMIT 1)
- Readings: always filter field_path, and kind; for samples also filter by
  zone, source_id or time, or the query returns too many rows.

RULES FOR THE QUERY
- One SELECT statement, nothing else. Functions, and no arithmetic. Filters, joins, ORDER BY, LIMIT and subqueries
  are fine.
- Select only what the question asks for, plus run_id so each row says which
  run it is, and the item's id (checkpoint_id, finding_id, code, event_id,
  source_id) when rows are items.
- Never select a 1/0 flag (locked) to show it: filter on it in WHERE.
- Path columns, so code can find each value in the records: r.path AS path_r
  for columns from runs, and <alias>.path AS path_<alias> for every rec_ table
  a shown column comes from (e.g. c.path AS path_c for rec_checkpoints).
- Every selected column needs its own plain name with no digits: alias
  clashes, e.g. c.status AS checkpoint_status.
- Order by r.run_order, then the table's position.

EXAMPLES
What happened at one checkpoint:
  SELECT r.run_id, c.checkpoint_id, c.status, c.result_status, c.observed, c.notes,
         c.path AS path_c, r.path AS path_r
  FROM rec_checkpoints c JOIN runs r ON r.run_id = c.run_id
  WHERE c.run_id IN (<run_ids>) AND c.checkpoint_id IN ('<checkpoint>')
  ORDER BY r.run_order, c.position
Which findings need human review:
  SELECT r.run_id, f.finding_id, f.checkpoint_id, f.feature, f.description,
         f.path AS path_f, r.path AS path_r
  FROM rec_findings f JOIN runs r ON r.run_id = f.run_id
  WHERE f.run_id IN (<run_ids>) AND f.status = 'abstained'
  ORDER BY r.run_order, f.position
The temperature reading at one checkpoint, or why there is none:
  SELECT r.run_id, c.checkpoint_id, g.value, x.reason,
         c.path AS path_c, g.path AS path_g, x.path AS path_x, r.path AS path_r
  FROM rec_checkpoints c JOIN runs r ON r.run_id = c.run_id
  LEFT JOIN rec_readings g ON g.run_id = c.run_id AND g.kind = 'checkpoint'
       AND g.source_id = c.checkpoint_id AND g.field_path = 'environment.temperature_c'
  LEFT JOIN rec_exclusions x ON x.run_id = c.run_id AND x.block = 'environment'
       AND x.scope IN (c.checkpoint_id, '__run__')
  WHERE c.run_id IN (<run_ids>) AND c.checkpoint_id IN ('<checkpoint>')
  ORDER BY r.run_order, c.position
How the run ended:
  SELECT r.run_id, r.run_status, r.final_status, r.end_time, r.duration, r.path AS path_r
  FROM runs r
  WHERE r.run_id IN (<run_ids>)
  ORDER BY r.run_order

REPLY
JSON only, one of:
  {"intent": "answer", "sql": "SELECT ..."}
  {"intent": "abstain", "sql": ""}   when these tables cannot answer the question

{feedback}QUESTION
{question}
"""
