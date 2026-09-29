"""The SQL writer's prompt for D2 (condition_count).

Used when the router names a condition_count instance: the SQL writer fills
the placeholders and asks the model for one SELECT over the runs and D2 tables
only: the counts, the items that met the condition, and the items left out
and why. Nothing here calls the model or runs the query.
"""

from agents.database import D2, RUNS

TYPE = "condition_count"
TABLES = (RUNS + D2).strip()

# Run by code, not the model, when the model's query returns no rows: each
# routed count in each routed run, so "none did" is stated as a stored zero out
# of its population. condition says which count a row is; reason is NULL for an
# OK row. {instances} and {run_ids} are filled with quoted, comma-separated ids.
SUMMARY_SQL = """SELECT r.run_id, d.condition, d.count, d.population, d.reason, d.path AS path_d, r.path AS path_r
FROM d2_condition_count d JOIN runs r ON r.run_id = d.run_id
WHERE d.instance IN ({instances}) AND d.run_id IN ({run_ids})
ORDER BY r.run_order, d.instance"""

# Written by hand. The SQL writer fills these placeholders:
#   {schema}       TABLES
#   {instances}    one line per routed D2 instance, as b1_analytical_router.describe writes it
#   {run_ids}      the run_ids the router resolved, oldest first, quoted and comma separated
#   {checkpoints}  the checkpoint_ids the router resolved, the same way, or "(any)" when none
#   {question}     the user's question
#   {feedback}     empty on the first call; why the previous reply was rejected after that
# The reply the SQL writer expects, JSON only:
#   {"intent": "answer", "sql": "SELECT ..."}   or   {"intent": "abstain", "sql": ""}
PROMPT = """You write one SQLite SELECT that fetches the answer to a question from
the tables below. You do not answer the question and you never compute a value:
code writes the answer from the rows your query returns.

The question is about a count: in each run, which items (checkpoints, findings
or sensor alerts) meet a named condition, how many do, and out of how many
could be checked.

TABLES
{schema}

THE COUNTS THE QUESTION USES (the instance column)
{instances}

WHAT TO READ
run_id in: {run_ids}
checkpoint_id in: {checkpoints}
Filter to exactly these runs and these counts. When checkpoints are listed and
a count's scope is checkpoints, also filter item_id to them.

HOW TO READ THE TABLES
- d2_condition_count has one row per count and run. count is how many items
  meet the condition, population how many it could be checked on, and
  inputs_excluded how many the exclusion rule left out; all already counted.
  For "how many", select count; for "out of how many", also population.
- d2_matching_ids has one row per item that met the condition, in the order
  the run recorded them. item_id is a checkpoint_id, a finding_id, or
  sensor_alert_0003 for a sensor alert, by its position in the run. A
  checkpoint visited twice can have two rows.
- d2_excluded_items has one row per item the exclusion rule left out of the
  count, and why (reason, e.g. checkpoint status is MISSED), in the order the
  run recorded them. item_id is the checkpoint_id; it is NULL for findings and
  sensor alerts, which are left out by reason only. count is how many items a
  row covers: 2 for a checkpoint visited twice. Use it for "which checkpoints
  were left out", "why was checkpoint_3 not counted" or "which were missed".
  Its counts add up to inputs_excluded; never add them yourself.
- An item is either counted (in population), or excluded (in
  d2_excluded_items). Of the counted items, only the ones that met the
  condition are stored. Which counted items did not meet it cannot be read
  from these tables: abstain for that question.
- status NOT_COMPUTABLE on d2_condition_count means the count could not be
  made for that run: select its reason.
- runs gives each run's start_time, and run_order for ordering (0 = oldest).

HOW MANY ROWS
Fetch exactly as many rows as the question asks for, no more.
- All of them ("which checkpoints failed"): no LIMIT.
- The run with the most or fewest ("which run had the most failures"): one
  row overall, keeping ties, with a subquery over the listed runs:
    AND d.count = (SELECT d2.count FROM d2_condition_count d2
                   WHERE d2.instance = d.instance AND d2.run_id IN (<run_ids>)
                   ORDER BY d2.count DESC LIMIT 1)
- The first or last matching item ("the first checkpoint that failed"): one
  per run, with a subquery on position in the same run:
    AND m.position = (SELECT m2.position FROM d2_matching_ids m2
                      WHERE m2.instance = m.instance AND m2.run_id = m.run_id
                      ORDER BY m2.position LIMIT 1)
  (ORDER BY m2.position DESC for the last.)
- A number of them ("name two failed checkpoints"): with one run, LIMIT that
  number. With several runs do not LIMIT, as it cuts across runs.
- Whether any did ("were there any critical alerts"): select count, which is
  one row per run already. Whether a named item did ("did checkpoint_3
  fail"): filter m.item_id to it. No row means it did not, or that it was
  left out: when that matters ("was checkpoint_3 checked"), read
  d2_excluded_items for it instead.
- Never LIMIT a list the question asked for in full.

RULES FOR THE QUERY
- One SELECT statement, nothing else.
- Select stored columns only. No COUNT, SUM, AVG, MIN or MAX, and no
  arithmetic on values: every count is already in a table. ORDER BY, LIMIT
  and subqueries are fine (see HOW MANY ROWS).
- Select only what the question asks for, plus run_id (and item_id when rows
  are items) so each row says what it is about. "Which checkpoints failed"
  needs item_id and nothing more; "how many" needs count; "out of how many"
  adds population. Add d.status and d.reason only when a count is
  NOT_COMPUTABLE. x.reason is different: it is why an item was left out, so
  select it with every excluded item ("which were left out" needs x.item_id
  and x.reason).
- When the question uses more than one count, also select d.condition, so each
  row says which count it is from; for item rows, join d2_condition_count d
  ON d.instance = m.instance AND d.run_id = m.run_id.
- Path columns, so code can find each value in the records:
  r.path AS path_r for run_id; m.path AS path_m for item_id; x.path AS path_x
  for any column from d2_excluded_items; d.path AS path_d for any column from
  d2_condition_count. Select only the ones the shown columns need.
- Every selected column needs its own plain name: alias clashes.
- Order by r.run_order, then m.position or x.position for item rows, or
  d.instance for count rows.

EXAMPLES
Which checkpoints failed:
  SELECT r.run_id, m.item_id, m.path AS path_m, r.path AS path_r
  FROM d2_matching_ids m JOIN runs r ON r.run_id = m.run_id
  WHERE m.instance = '<instance>' AND m.run_id IN (<run_ids>)
  ORDER BY r.run_order, m.position
How many checkpoints failed, out of how many:
  SELECT r.run_id, d.count, d.population, d.path AS path_d, r.path AS path_r
  FROM d2_condition_count d JOIN runs r ON r.run_id = d.run_id
  WHERE d.instance = '<instance>' AND d.run_id IN (<run_ids>)
  ORDER BY r.run_order
Which checkpoints were left out of the count, and why:
  SELECT r.run_id, x.item_id, x.reason, x.path AS path_x, r.path AS path_r
  FROM d2_excluded_items x JOIN runs r ON r.run_id = x.run_id
  WHERE x.instance = '<instance>' AND x.run_id IN (<run_ids>)
  ORDER BY r.run_order, x.position
How many checkpoints passed and how many failed:
  SELECT r.run_id, d.condition, d.count, d.path AS path_d, r.path AS path_r
  FROM d2_condition_count d JOIN runs r ON r.run_id = d.run_id
  WHERE d.instance IN ('<instance>', '<instance>') AND d.run_id IN (<run_ids>)
  ORDER BY r.run_order, d.instance

REPLY
JSON only, one of:
  {"intent": "answer", "sql": "SELECT ..."}
  {"intent": "abstain", "sql": ""}   when these tables cannot answer the question

{feedback}QUESTION
{question}
"""
