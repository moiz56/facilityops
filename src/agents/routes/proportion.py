"""The SQL writer's prompt for D3 (proportion).

Used when the router names a proportion instance: the SQL writer fills the
placeholders and asks the model for one SELECT over the runs and D3 tables
only. Nothing here calls the model or runs the query.
"""

from agents.database_derivation import D3, RUNS

TYPE = "proportion"
TABLES = (RUNS + D3).strip()

# Run by code, not the model, when the model's query returns no rows: each
# routed share in each routed run, with the numbers it is made of. D3's output
# names no condition, so instance says which share a row is. reason is NULL for
# an OK row. {instances} and {run_ids} are filled with quoted, comma-separated ids.
SUMMARY_SQL = """SELECT r.run_id, d.instance, d.percentage, d.numerator, d.denominator, d.reason, d.path AS path_d, r.path AS path_r
FROM d3_proportion d JOIN runs r ON r.run_id = d.run_id
WHERE d.instance IN ({instances}) AND d.run_id IN ({run_ids})
ORDER BY r.run_order, d.instance"""

# Written by hand. The SQL writer fills these placeholders:
#   {schema}       TABLES
#   {instances}    one line per routed D3 instance, as b1_analytical_router_derived.describe writes it
#   {run_ids}      the run_ids the router resolved, oldest first, quoted and comma separated
#   {checkpoints}  the checkpoint_ids the router resolved, the same way, or "(any)" when none
#   {question}     the user's question
#   {feedback}     empty on the first call; why the previous reply was rejected after that
# The reply the SQL writer expects, JSON only:
#   {"intent": "answer", "sql": "SELECT ..."}   or   {"intent": "abstain", "sql": ""}
PROMPT = """You write one SQLite SELECT that fetches the answer to a question from
the tables below. You do not answer the question and you never compute a value:
code writes the answer from the rows your query returns.

The question is about a share: in each run, what share of the items
(checkpoints, findings or sensor alerts) meet a named condition, the numbers
it is made of, and which items meet it.

TABLES
{schema}

THE SHARES THE QUESTION USES (the instance column)
{instances}

WHAT TO READ
run_id in: {run_ids}
checkpoint_id in: {checkpoints}
Filter to exactly these runs and these shares. When checkpoints are listed and
a share's items are checkpoints, also filter item_id to them.

HOW TO READ THE TABLES
- d3_proportion has one row per share and run. percentage is the share out of
  100 (87.5) and proportion the same share as a fraction (0.875); numerator is
  how many items meet the condition and denominator how many it could be
  checked on; inputs_excluded is how many the exclusion rule left out. All
  are already worked out.
- For "what percentage" or "what share", select percentage. Select proportion
  only when the question asks for a fraction or ratio. For "how many" or "out
  of how many", select numerator and denominator.
- d3_numerator_ids has one row per item that met the condition, in the order
  the run recorded them. item_id is a checkpoint_id, a finding_id, or
  sensor_alert_0003 for a sensor alert, by its position in the run. A
  checkpoint visited twice can have two rows.
- Only the items that met the condition are stored. Which items did not meet
  it cannot be read from these tables: abstain for that question.
- status NOT_COMPUTABLE means the share could not be worked out for that run
  (e.g. the denominator is zero): select its reason. Never state it as 0%.
- runs gives each run's start_time, and run_order for ordering (0 = oldest).

HOW MANY ROWS
Fetch exactly as many rows as the question asks for, no more.
- All of them ("which checkpoints count towards it"): no LIMIT.
- The run with the highest or lowest share ("which run had the best
  completion rate"): one row overall, keeping ties, with a subquery over the
  listed runs; leave out NOT_COMPUTABLE runs on both sides:
    AND d.status = 'OK'
    AND d.percentage = (SELECT d2.percentage FROM d3_proportion d2
                        WHERE d2.instance = d.instance AND d2.run_id IN (<run_ids>)
                          AND d2.status = 'OK'
                        ORDER BY d2.percentage DESC LIMIT 1)
- The first or last item in the numerator: one per run, with a subquery on
  position in the same run:
    AND m.position = (SELECT m2.position FROM d3_numerator_ids m2
                      WHERE m2.instance = m.instance AND m2.run_id = m.run_id
                      ORDER BY m2.position LIMIT 1)
  (ORDER BY m2.position DESC for the last.)
- A number of them: with one run, LIMIT that number. With several runs do not
  LIMIT, as it cuts across runs.
- Never LIMIT a list the question asked for in full.

RULES FOR THE QUERY
- One SELECT statement, nothing else.
- Select stored columns only. No COUNT, SUM, AVG, MIN or MAX, and no
  arithmetic on values: every share and count is already in a table. ORDER
  BY, LIMIT and subqueries are fine (see HOW MANY ROWS).
- Select only what the question asks for, plus run_id (and item_id when rows
  are items) so each row says what it is about. "What percentage of
  checkpoints were completed" needs percentage; "and out of how many" adds
  numerator and denominator; "which ones" needs item_id. Add status and reason
  whenever a share row is selected, so a NOT_COMPUTABLE run says why.
- When the question uses more than one share, also select d.instance, so each
  row says which share it is; for item rows, select m.instance.
- Path columns, so code can find each value in the records:
  r.path AS path_r for run_id; m.path AS path_m for item_id; d.path AS path_d
  for any column from d3_proportion. Select only the ones the shown columns need.
- Every selected column needs its own plain name with no digits: alias
  clashes, e.g. d.instance AS share_instance, m.instance AS item_instance.
- Order by r.run_order, then m.position for item rows, or d.instance for
  share rows.

EXAMPLES
What percentage of checkpoints were completed:
  SELECT r.run_id, d.percentage, d.status, d.reason, d.path AS path_d, r.path AS path_r
  FROM d3_proportion d JOIN runs r ON r.run_id = d.run_id
  WHERE d.instance = '<instance>' AND d.run_id IN (<run_ids>)
  ORDER BY r.run_order
What percentage of checkpoints were completed, out of how many:
  SELECT r.run_id, d.percentage, d.numerator, d.denominator, d.status, d.reason,
         d.path AS path_d, r.path AS path_r
  FROM d3_proportion d JOIN runs r ON r.run_id = d.run_id
  WHERE d.instance = '<instance>' AND d.run_id IN (<run_ids>)
  ORDER BY r.run_order
Which checkpoints count towards it:
  SELECT r.run_id, m.item_id, m.path AS path_m, r.path AS path_r
  FROM d3_numerator_ids m JOIN runs r ON r.run_id = m.run_id
  WHERE m.instance = '<instance>' AND m.run_id IN (<run_ids>)
  ORDER BY r.run_order, m.position

REPLY
JSON only, one of:
  {"intent": "answer", "sql": "SELECT ..."}
  {"intent": "abstain", "sql": ""}   when these tables cannot answer the question

{feedback}QUESTION
{question}
"""
