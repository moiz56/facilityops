"""The SQL writer's prompt for D1 (threshold_compare).

Used when the router names a threshold_compare instance: the SQL writer fills
the placeholders and asks the model for one SELECT over the runs and D1 tables
only. Nothing here calls the model or runs the query.
"""

from agents.database_derivation import D1, RUNS

TYPE = "threshold_compare"
TABLES = (RUNS + D1).strip()

# Run by code, not the model, when the model's query returns no rows: how many
# checkpoints exceeded in each routed run, so "none did" is stated as a stored
# zero. reason is NULL for an OK run, and is only shown for a NOT_COMPUTABLE one.
# {instances} and {run_ids} are filled with quoted, comma-separated ids.
SUMMARY_SQL = """SELECT r.run_id, d.exceeds_count, d.reason, d.path AS path_d, r.path AS path_r
FROM d1_threshold_compare d JOIN runs r ON r.run_id = d.run_id
WHERE d.instance IN ({instances}) AND d.run_id IN ({run_ids})
ORDER BY r.run_order"""

# Written by hand. The SQL writer fills these placeholders:
#   {schema}       TABLES
#   {instances}    one line per routed D1 instance, as b1_analytical_router_derived.describe writes it
#   {run_ids}      the run_ids the router resolved, oldest first, quoted and comma separated
#   {checkpoints}  the checkpoint_ids the router resolved, the same way, or "(any)" when none
#   {question}     the user's question
#   {feedback}     empty on the first call; why the previous reply was rejected after that
# The reply the SQL writer expects, JSON only:
#   {"intent": "answer", "sql": "SELECT ..."}   or   {"intent": "abstain", "sql": ""}
PROMPT = """You write one SQLite SELECT that fetches the answer to a question from
the tables below. You do not answer the question and you never compute a value:
code writes the answer from the rows your query returns.

The question is about a threshold comparison: in each run, whether a sensor
reading at each checkpoint is above or below a fixed threshold, and by how much.

TABLES
{schema}

THE COMPARISONS THE QUESTION USES (the instance column)
{instances}

WHAT TO READ
run_id in: {run_ids}
checkpoint_id in: {checkpoints}
Filter to exactly these runs and these comparisons, and to these checkpoints
when any are listed.

HOW TO READ THE TABLES
- d1_threshold_compare has one row per comparison and run. status
  NOT_COMPUTABLE means the run had no usable reading: select its reason.
- exceeds_count and not_exceeds_count are how many checkpoints did and did not
  exceed the threshold, already counted. For "how many", select them.
- d1_checkpoints has one row per checkpoint in a run, with status OK:
  value is the reading, exceeds is 1 when it is past the threshold in the
  comparison's direction (above: greater than it, below: less than it), and
  delta is value minus threshold. A reading equal to the threshold does not
  exceed it.
- checkpoint_name is the checkpoint's name, on every d1_checkpoints row
  whatever its status.
- A d1_checkpoints row with status NOT_COMPUTABLE had no usable reading; reason
  says why and value, exceeds and delta are NULL. It neither exceeded nor
  stayed within the threshold, so never list it as either. When the question
  asks about every checkpoint, or about those that did not exceed, also select
  the NOT_COMPUTABLE rows, with their status and reason.
- When a comparison's scope is one checkpoint_id, its value, exceeds and delta
  are on the d1_threshold_compare row, with its checkpoint_name, and
  d1_checkpoints has no rows for it.
- runs gives each run's start_time, and run_order for ordering (0 = oldest).

HOW MANY ROWS
Fetch exactly as many rows as the question asks for, no more.
- All of them ("which checkpoints exceeded", "list the readings"): no LIMIT.
- The single most or least ("which checkpoint exceeded by the most", "the
  hottest checkpoint", "the lowest reading"). First decide: one per run, or
  one overall?
  - One per run (the question names one run, or says "in each run"): keep the
    rows whose value equals that run's top value, found with a subquery on
    the same run. It works for one run or many, and keeps ties:
      AND c.status = 'OK'
      AND c.delta = (SELECT c2.delta FROM d1_checkpoints c2
                     WHERE c2.instance = c.instance AND c2.run_id = c.run_id
                       AND c2.status = 'OK'
                     ORDER BY c2.delta DESC LIMIT 1)
  - One overall ("the highest reading across all runs"): the same, with
    c2.run_id IN (<run_ids>) instead of c2.run_id = c.run_id.
- The first or last ("the first checkpoint over the threshold"): the same
  subquery form on position (ORDER BY c2.position for the first, DESC for the
  last), repeating the outer filters inside it (e.g. c2.exceeds = 1).
- A number of them ("the top three"): with one run, ORDER BY the value and
  LIMIT that number. With several runs do not LIMIT, as it cuts across runs:
  order by run, then value, so each run's top ones come first.
- Whether any did ("did any checkpoint exceed"): select exceeds_count from
  d1_threshold_compare, which is one row per run already.
- Never LIMIT a list the question asked for in full.

RULES FOR THE QUERY
- One SELECT statement, nothing else.
- Select stored columns only. No COUNT, SUM, AVG, MIN or MAX, and no
  arithmetic on values: every count, difference and reading is already in a
  table. ORDER BY, LIMIT and subqueries are fine (see HOW MANY ROWS).
- Select only what the question asks for, plus run_id (and checkpoint_id and
  checkpoint_name when rows are checkpoints) so each row says what it is
  about. "Which checkpoints exceeded" needs checkpoint_id and checkpoint_name
  and nothing more; "by how much" adds delta; "what was the reading" adds
  value; "how many" needs exceeds_count. Add status and reason only when the
  question is about every checkpoint or about those that did not exceed.
- Never select exceeds or another 1/0 flag to show it: filter on it in WHERE.
- Path columns, so code can find each value in the records:
  r.path AS path_r for run_id; c.path_record for checkpoint_id and
  checkpoint_name; c.path AS path_c for value, delta, status or reason from
  d1_checkpoints; d.path AS path_d for any column from d1_threshold_compare.
  Select only the ones the shown columns need.
- Every selected column needs its own plain name with no digits: alias
  clashes, e.g.
  c.status AS checkpoint_status, d.status AS run_status.
- Order by r.run_order, then c.position.

EXAMPLES
Which checkpoints exceeded the threshold:
  SELECT r.run_id, c.checkpoint_id, c.checkpoint_name, c.path_record, r.path AS path_r
  FROM d1_checkpoints c JOIN runs r ON r.run_id = c.run_id
  WHERE c.instance = '<instance>' AND c.run_id IN (<run_ids>)
    AND c.status = 'OK' AND c.exceeds = 1
  ORDER BY r.run_order, c.position
Which checkpoints exceeded the threshold, and by how much:
  SELECT r.run_id, c.checkpoint_id, c.checkpoint_name, c.delta, c.path_record, c.path AS path_c,
         r.path AS path_r
  FROM d1_checkpoints c JOIN runs r ON r.run_id = c.run_id
  WHERE c.instance = '<instance>' AND c.run_id IN (<run_ids>)
    AND c.status = 'OK' AND c.exceeds = 1
  ORDER BY r.run_order, c.position
How many checkpoints exceeded the threshold:
  SELECT r.run_id, d.exceeds_count, d.path AS path_d, r.path AS path_r
  FROM d1_threshold_compare d JOIN runs r ON r.run_id = d.run_id
  WHERE d.instance = '<instance>' AND d.run_id IN (<run_ids>)
  ORDER BY r.run_order

REPLY
JSON only, one of:
  {"intent": "answer", "sql": "SELECT ..."}
  {"intent": "abstain", "sql": ""}   when these tables cannot answer the question

{feedback}QUESTION
{question}
"""
