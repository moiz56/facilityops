"""The SQL writer's prompt for D9 (sensor_alert_count).

Used when the router names a sensor_alert_count instance: the SQL writer fills
the placeholders and asks the model for one SELECT over the runs and D9 tables
only: how many sensor alerts each run logged, and how many of each severity.
Nothing here calls the model or runs the query.
"""

from agents.database_derivation import D9, RUNS

TYPE = "sensor_alert_count"
TABLES = (RUNS + D9).strip()

# Run by code, not the model, when the model's query returns no rows: how many
# alerts each routed run logged, so "none" is stated as a stored count. reason
# is NULL for an OK row. {instances} and {run_ids} are filled with quoted,
# comma-separated ids.
SUMMARY_SQL = """SELECT r.run_id, d.count, d.reason, d.path AS path_d, r.path AS path_r
FROM d9_sensor_alert_count d JOIN runs r ON r.run_id = d.run_id
WHERE d.instance IN ({instances}) AND d.run_id IN ({run_ids})
ORDER BY r.run_order, d.instance"""

# Written by hand. The SQL writer fills these placeholders:
#   {schema}       TABLES
#   {instances}    one line per routed D9 instance, as b1_analytical_router_derived.describe writes it
#   {run_ids}      the run_ids the router resolved, oldest first, quoted and comma separated
#   {question}     the user's question
#   {feedback}     empty on the first call; why the previous reply was rejected after that
# The reply the SQL writer expects, JSON only:
#   {"intent": "answer", "sql": "SELECT ..."}   or   {"intent": "abstain", "sql": ""}
PROMPT = """You write one SQLite SELECT that fetches the answer to a question from
the tables below. You do not answer the question and you never compute a value:
code writes the answer from the rows your query returns.

The question is about sensor alerts: in each run, how many sensor alerts were
logged, and how many of each severity.

TABLES
{schema}

THE COUNTS THE QUESTION USES (the instance column)
{instances}

WHAT TO READ
run_id in: {run_ids}
Filter to exactly these runs and these counts.

HOW TO READ THE TABLES
- d9_sensor_alert_count has one row per run. count is how many sensor alerts
  the run logged, already counted. 0 is a real count: the run logged none.
- d9_by_severity has one row per severity in each run, severity and count. The
  expected severities come first, each listed even when count is 0; any other
  severity found follows, verbatim. For "by severity" or "a breakdown", select
  every row: a 0 is part of the answer, so never filter count > 0. For one
  severity ("how many critical alerts"), filter b.severity to it, written as
  in its line under THE COUNTS THE QUESTION USES. Never select b.expected:
  filter on it if you need it.
- These are sensor alerts only. A "warning" here is an alert's severity, not a
  checkpoint's WARN result.
- status NOT_COMPUTABLE on d9_sensor_alert_count means the count could not be
  made for that run: select its reason.
- runs gives each run's start_time, and run_order for ordering (0 = oldest).

WHAT THESE TABLES DO NOT HOLD
- No single alert: not which alerts, their code, label, description, time,
  nearest checkpoint or zone. Abstain for those.
- No count per checkpoint or per zone ("how many alerts near <checkpoint>",
  "alerts in <zone>"). Abstain for those.

HOW MANY ROWS
Fetch exactly as many rows as the question asks for, no more.
- Every run asked about ("how many alerts in each run"): no LIMIT.
- The run with the most or fewest alerts: one row overall, keeping ties, with
  a subquery over the listed runs:
    AND d.count = (SELECT d2.count FROM d9_sensor_alert_count d2
                   WHERE d2.instance = d.instance AND d2.run_id IN (<run_ids>)
                   ORDER BY d2.count DESC LIMIT 1)
  (ORDER BY d2.count ASC for the fewest.)
- The run with the most or fewest alerts of one severity: the same, over
  d9_by_severity, with the severity filtered on both sides:
    AND b.severity = '<severity>'
    AND b.count = (SELECT b2.count FROM d9_by_severity b2
                   WHERE b2.instance = b.instance AND b2.run_id IN (<run_ids>)
                     AND b2.severity = b.severity
                   ORDER BY b2.count DESC LIMIT 1)
- Never LIMIT a list the question asked for in full.

RULES FOR THE QUERY
- One statement: a SELECT, or two SELECTs joined with UNION ALL when the total
  and the severities are asked for together (see EXAMPLES). Nothing else.
- Select stored columns only. No COUNT, SUM, AVG, MIN or MAX, and no
  arithmetic on values: every count is already in a table. ORDER BY, LIMIT
  and subqueries are fine (see HOW MANY ROWS).
- Select only what the question asks for, plus run_id so each row says which
  run it is about. "How many alerts" needs d.count; "how many critical" needs
  b.severity and b.count. Add d.status and d.reason only when a count is
  NOT_COMPUTABLE.
- Never rename a value column: select d.count and b.count as count, never
  AS alert_count or AS severity_count. Code finds each value by its column
  name, and a renamed count cannot be found. When the total and the
  severities are both asked for, use the UNION ALL in EXAMPLES, where both
  stay count.
- Path columns, so code can find each value in the records:
  r.path AS path_r for run_id; d.path AS path_d for any column from
  d9_sensor_alert_count; b.path AS path_b for severity and count from
  d9_by_severity. Select only the ones the shown columns need.
- Every selected column needs its own plain name with no digits.
- Order by r.run_order, then b.position for severity rows.

EXAMPLES
How many sensor alerts were logged:
  SELECT r.run_id, d.count, d.path AS path_d, r.path AS path_r
  FROM d9_sensor_alert_count d JOIN runs r ON r.run_id = d.run_id
  WHERE d.instance = '<instance>' AND d.run_id IN (<run_ids>)
  ORDER BY r.run_order
How many sensor alerts, by severity (every severity, zeros included):
  SELECT r.run_id, b.severity, b.count, b.path AS path_b, r.path AS path_r
  FROM d9_by_severity b JOIN runs r ON r.run_id = b.run_id
  WHERE b.instance = '<instance>' AND b.run_id IN (<run_ids>)
  ORDER BY r.run_order, b.position
How many critical alerts:
  SELECT r.run_id, b.severity, b.count, b.path AS path_b, r.path AS path_r
  FROM d9_by_severity b JOIN runs r ON r.run_id = b.run_id
  WHERE b.instance = '<instance>' AND b.run_id IN (<run_ids>)
    AND b.severity = 'critical'
  ORDER BY r.run_order
How many alerts in all, and how many of each severity (the total row first in
each run, then one row per severity; both counts stay count):
  SELECT r.run_id, r.run_order, NULL AS position, NULL AS severity, d.count,
         d.path AS path_d, NULL AS path_b, r.path AS path_r
  FROM d9_sensor_alert_count d JOIN runs r ON r.run_id = d.run_id
  WHERE d.instance = '<instance>' AND d.run_id IN (<run_ids>)
  UNION ALL
  SELECT r.run_id, r.run_order, b.position, b.severity, b.count,
         NULL, b.path, r.path
  FROM d9_by_severity b JOIN runs r ON r.run_id = b.run_id
  WHERE b.instance = '<instance>' AND b.run_id IN (<run_ids>)
  ORDER BY run_order, position
Which run had the most alerts:
  SELECT r.run_id, d.count, d.path AS path_d, r.path AS path_r
  FROM d9_sensor_alert_count d JOIN runs r ON r.run_id = d.run_id
  WHERE d.instance = '<instance>' AND d.run_id IN (<run_ids>)
    AND d.count = (SELECT d2.count FROM d9_sensor_alert_count d2
                   WHERE d2.instance = d.instance AND d2.run_id IN (<run_ids>)
                   ORDER BY d2.count DESC LIMIT 1)
  ORDER BY r.run_order

REPLY
JSON only, one of:
  {"intent": "answer", "sql": "SELECT ..."}
  {"intent": "abstain", "sql": ""}   when these tables cannot answer the question

{feedback}QUESTION
{question}
"""
