"""The SQL writer's prompt for D11 (count_check).

Used when the router names a count_check instance: the SQL writer fills the
placeholders and asks the model for one SELECT over the runs and D11 tables
only: whether the counts the robot declared match what each run's lists hold,
for the run's seven counts and for each event's evidence count.
Nothing here calls the model or runs the query.
"""

from agents.database_derivation import D11, RUNS

TYPE = "count_check"
TABLES = (RUNS + D11).strip()

# Run by code, not the model, when the model's query returns no rows: how many
# checks did not match in each routed run, so "none" is stated as a stored
# count. reason is NULL for an OK row. {instances} and {run_ids} are filled
# with quoted, comma-separated ids.
SUMMARY_SQL = """SELECT r.run_id, d.mismatch_count, d.reason, d.path AS path_d, r.path AS path_r
FROM d11_count_check d JOIN runs r ON r.run_id = d.run_id
WHERE d.instance IN ({instances}) AND d.run_id IN ({run_ids})
ORDER BY r.run_order, d.instance"""

# Written by hand. The SQL writer fills these placeholders:
#   {schema}       TABLES
#   {instances}    one line per routed D11 instance, as b1_analytical_router_derived.describe writes it
#   {run_ids}      the run_ids the router resolved, oldest first, quoted and comma separated
#   {checkpoints}  the checkpoint_ids the router resolved, the same way, or "(any)" when none
#   {question}     the user's question
#   {feedback}     empty on the first call; why the previous reply was rejected after that
# The reply the SQL writer expects, JSON only:
#   {"intent": "answer", "sql": "SELECT ..."}   or   {"intent": "abstain", "sql": ""}
PROMPT = """You write one SQLite SELECT that fetches the answer to a question from
the tables below. You do not answer the question and you never compute a value:
code writes the answer from the rows your query returns.

The question is about whether the counts the robot declared are right: in each
run, the counts it reported for itself next to the same counts taken from the
run's own lists, and each checkpoint event's evidence count next to the images
that visit recorded.

TABLES
{schema}

THE CHECKS THE QUESTION USES (the instance column)
{instances}

WHAT TO READ
run_id in: {run_ids}
checkpoint_id in: {checkpoints}
Filter to exactly these runs and these checks. When checkpoints are listed,
read d11_evidence and filter e.checkpoint_id to them. "(any)" means no filter.

HOW TO READ THE TABLES
- d11_count_check has one row per run. mismatch_count is how many checks in
  d11_counts and d11_evidence together do not match, already counted. 0 is a
  real count: everything the robot declared was right.
- d11_counts has one row per count the robot declares on a run, all seven
  always listed. declared is what the robot reported; counted is the same
  count taken from the run's lists. declared is NULL when the robot reported
  nothing for it: there is nothing to check.
- matches is 1 when declared and counted agree and 0 when they do not; NULL in
  d11_counts when nothing was declared.
- counted counts every entry in the run's checkpoints, so a checkpoint the
  robot passed twice on its round trip counts twice. A warned checkpoint is
  one with result WARN or a sensor warning, whatever its result.
- d11_evidence has one row per checkpoint event in the log that declared an
  evidence count, in log order. declared is the event's evidence_count;
  counted is how many images the visit it belongs to recorded. The nth event
  for a checkpoint belongs to its nth visit. counted is NULL when there was no
  such visit: the event names a checkpoint the run did not visit that often.
- status NOT_COMPUTABLE on d11_count_check means the checks could not be made
  for that run: select its reason.
- runs gives each run's start_time, and run_order for ordering (0 = oldest).

WHICH FIELD IS WHICH
Questions use everyday words for d11_counts.field; match them:
- "required", "total", "how many checkpoints on the route":
  total_required_checkpoints
- "completed", "reached", "visited": total_completed_checkpoints
- "passed": passed_checkpoints
- "failed": failed_checkpoints
- "missed": missed_checkpoints
- "warned", "warnings": warned_checkpoints
- "findings": finding_count
- "evidence count", "images it said it took", "photos per checkpoint":
  d11_evidence, not d11_counts.

WHICH ROWS
- "Do the counts match", "are the declared counts right", "compare what it
  reported with what it did": every d11_counts row of the run.
- "Which counts are wrong", "where does it disagree": d11_counts with
  c.matches = 0.
- One count ("did it report the right number of failed checkpoints"): filter
  c.field to it.
- "Which checkpoints have the wrong evidence count": d11_evidence with
  e.matches = 0.
- "How many mismatches", "how many counts are wrong": d.mismatch_count.
- "Which run had the most mismatches": one row overall, keeping ties, with a
  subquery over the listed runs:
    AND d.mismatch_count = (SELECT d2.mismatch_count FROM d11_count_check d2
                            WHERE d2.instance = d.instance AND d2.run_id IN (<run_ids>)
                            ORDER BY d2.mismatch_count DESC LIMIT 1)
- Never LIMIT a list the question asked for in full.

RULES FOR THE QUERY
- One statement: a SELECT, or two SELECTs joined with UNION ALL when the run
  counts and the evidence counts are asked for together (see EXAMPLES).
  Nothing else.
- Select stored columns only. No COUNT, SUM, AVG, MIN or MAX, and no
  arithmetic on values: every count is already in a table. ORDER BY, LIMIT
  and subqueries are fine.
- Select only what the question asks for, plus run_id so each row says which
  run it is about. On d11_counts rows select c.field, c.declared and
  c.counted; on d11_evidence rows select e.checkpoint_id, e.declared and
  e.counted, and e.checkpoint_name when it helps. Add d.status and d.reason
  only when a check is NOT_COMPUTABLE.
- Never select matches, event, visit or position: filter or order on them
  instead. matches is a yes/no flag and code cannot show it.
- Never rename a value column: select declared and counted under their own
  names. Code finds each value by its column name.
- Path columns, so code can find each value in the records:
  r.path AS path_r for run_id; d.path AS path_d for any column from
  d11_count_check; c.path AS path_c for any column from d11_counts; e.path AS
  path_e for the counts on d11_evidence, and e.path_record AS path_record for
  its checkpoint_id and checkpoint_name. Select only the ones the shown
  columns need.
- Every selected column needs its own plain name with no digits.
- Order by r.run_order, then c.position or e.position.

EXAMPLES
Do the robot's declared counts match the run:
  SELECT r.run_id, c.field, c.declared, c.counted, c.path AS path_c, r.path AS path_r
  FROM d11_counts c JOIN runs r ON r.run_id = c.run_id
  WHERE c.instance = '<instance>' AND c.run_id IN (<run_ids>)
  ORDER BY r.run_order, c.position
Which declared counts are wrong:
  SELECT r.run_id, c.field, c.declared, c.counted, c.path AS path_c, r.path AS path_r
  FROM d11_counts c JOIN runs r ON r.run_id = c.run_id
  WHERE c.instance = '<instance>' AND c.run_id IN (<run_ids>) AND c.matches = 0
  ORDER BY r.run_order, c.position
Did it report the right number of warned checkpoints:
  SELECT r.run_id, c.field, c.declared, c.counted, c.path AS path_c, r.path AS path_r
  FROM d11_counts c JOIN runs r ON r.run_id = c.run_id
  WHERE c.instance = '<instance>' AND c.run_id IN (<run_ids>)
    AND c.field = 'warned_checkpoints'
  ORDER BY r.run_order
Which checkpoints have the wrong evidence count:
  SELECT r.run_id, e.checkpoint_id, e.checkpoint_name, e.declared, e.counted,
         e.path AS path_e, e.path_record AS path_record, r.path AS path_r
  FROM d11_evidence e JOIN runs r ON r.run_id = e.run_id
  WHERE e.instance = '<instance>' AND e.run_id IN (<run_ids>) AND e.matches = 0
  ORDER BY r.run_order, e.position
How many checks did not match, in each run:
  SELECT r.run_id, d.mismatch_count, d.path AS path_d, r.path AS path_r
  FROM d11_count_check d JOIN runs r ON r.run_id = d.run_id
  WHERE d.instance = '<instance>' AND d.run_id IN (<run_ids>)
  ORDER BY r.run_order
Everything that is wrong, the run's counts and the evidence counts (the counts
first in each run, as their checkpoint_id is NULL; declared and counted stay
under their names):
  SELECT r.run_id, r.run_order, c.position, c.field, NULL AS checkpoint_id,
         c.declared, c.counted, c.path AS path_c, NULL AS path_e, NULL AS path_record, r.path AS path_r
  FROM d11_counts c JOIN runs r ON r.run_id = c.run_id
  WHERE c.instance = '<instance>' AND c.run_id IN (<run_ids>) AND c.matches = 0
  UNION ALL
  SELECT r.run_id, r.run_order, e.position, NULL, e.checkpoint_id,
         e.declared, e.counted, NULL, e.path, e.path_record, r.path
  FROM d11_evidence e JOIN runs r ON r.run_id = e.run_id
  WHERE e.instance = '<instance>' AND e.run_id IN (<run_ids>) AND e.matches = 0
  ORDER BY run_order, checkpoint_id, position

REPLY
JSON only, one of:
  {"intent": "answer", "sql": "SELECT ..."}
  {"intent": "abstain", "sql": ""}   when these tables cannot answer the question

{feedback}QUESTION
{question}
"""
