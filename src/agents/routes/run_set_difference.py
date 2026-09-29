"""The SQL writer's prompt for D7 (run_set_difference).

Used when the router names a run_set_difference instance: the SQL writer
fills the placeholders and asks the model for one SELECT over the D7 tables
only. D7 gives one output for all runs, comparing the run before the latest
with the latest, so its rows have no run_id (PER_RUN = False). Nothing here
calls the model or runs the query.
"""

from agents.database import D7, RUNS

TYPE = "run_set_difference"
PER_RUN = False
TABLES = (RUNS + D7).strip()

# Run by code, not the model, when the model's query returns no rows: the
# comparison's runs and counts, or its reason. {instances} is filled with the
# quoted, comma-separated instance names.
SUMMARY_SQL = """SELECT d.run_a, d.run_b, d.count_only_in_a, d.count_only_in_b, d.count_in_both, d.reason, d.path AS path_d
FROM d7_run_set_difference d
WHERE d.instance IN ({instances})
ORDER BY d.instance"""

# Written by hand. The SQL writer fills these placeholders:
#   {schema}       TABLES
#   {checkpoints}  the checkpoint_ids the router resolved, quoted and comma separated, or "(any)" when none
#   {question}     the user's question
#   {feedback}     empty on the first call; why the previous reply was rejected after that
# The reply the SQL writer expects, JSON only:
#   {"intent": "answer", "sql": "SELECT ..."}   or   {"intent": "abstain", "sql": ""}
PROMPT = """You write one SQLite SELECT that fetches the answer to a question from
the tables below. You do not answer the question and you never compute a value:
code writes the answer from the rows your query returns.

The question is about how the route changed between the last two runs: which
checkpoints the run before the latest had on its route and the latest did not,
which the latest had and the earlier did not, and which both had.

TABLES
{schema}

WHAT TO READ
checkpoint_id in: {checkpoints}
When checkpoints are listed, filter c.checkpoint_id to them.

WHAT THE COMPARISON IS
- There is one comparison, always between the same two runs: run_a is the run
  before the latest, run_b is the latest. No other pair of runs is held: if
  the question compares any other runs ("the first and the third", "last week
  and this week"), abstain.
- It compares the checkpoints on each run's route, as each run's record lists
  them. A checkpoint the robot missed is still on that run's route, so it
  counts as present. So this is not whether a checkpoint was visited,
  completed or skipped: abstain for "which checkpoints did the latest run
  skip" or "which were missed".
- The three memberships, and the words questions use for them:
  - only_in_a: on the earlier run's route and not the latest's: "dropped",
    "removed", "no longer on the route", "missing from the latest run".
  - only_in_b: on the latest run's route and not the earlier's: "new",
    "added", "only in the latest run".
  - in_both: on both: "kept", "unchanged", "the same", "common to both".
- "Did the route change", "what changed", "compare the last two routes" and
  "is the route the same" all ask for the whole picture: both runs and all
  three counts, plus the dropped and added checkpoints. The route is
  unchanged when count_only_in_a and count_only_in_b are both 0; select them,
  never decide it yourself.
- It says only what differs. It never says whether that is better or worse,
  or why: abstain if the question asks for that.
- status NOT_COMPUTABLE means there are fewer than two runs: select reason.

HOW TO READ THE TABLES
- d7_run_set_difference (d): the one comparison row: run_a, run_b, and how
  many checkpoints are in each membership, already counted.
- d7_checkpoints (c): one row per checkpoint in each membership, in route
  order (position). A checkpoint in neither run has no row.

KEEP THE ANSWER COMPLETE
An empty list is an answer: "no checkpoint was added" is the count 0, not a
missing row. So whenever the question is about the checkpoints of a
membership, read the comparison row and LEFT JOIN the checkpoints of that
membership to it. When the list is empty the query still returns the
comparison row, with its count and the two runs, and the checkpoint is empty.
- Always select d.run_a and d.run_b, so the answer says which two runs it
  compared.
- With a list, also select that membership's count, so the answer says how
  many there are.
- For a named checkpoint, filter c.checkpoint_id and select c.membership; no
  row means neither run had it on its route.

HOW MANY ROWS
Fetch exactly as many rows as the question asks for, no more.
- Which checkpoints of one membership: every row of it; no LIMIT.
- How many: the counts from d7_run_set_difference, one row.
- The whole picture: the comparison row with the dropped and added
  checkpoints (not in_both, which is usually the whole route; its count says
  how many are kept).
- The first or last of a membership in the route: ORDER BY c.position,
  LIMIT 1.

RULES FOR THE QUERY
- One SELECT statement, nothing else.
- Select stored columns only. No COUNT, SUM, AVG, MIN or MAX, and no
  arithmetic: every count is already in a table. LEFT JOIN, ORDER BY and LIMIT
  are fine.
- Always select d.reason when d7_run_set_difference is in the query: it is
  empty when OK.
- Path columns, so code can find each value in the records: c.path AS path_c
  for any column from d7_checkpoints; d.path AS path_d for any column from
  d7_run_set_difference.
- Every selected column needs its own plain name with no digits.
- Order checkpoint rows by c.membership, then c.position.

EXAMPLES
Which checkpoints are new in the latest run (the comparison row even if none are):
  SELECT d.run_a, d.run_b, d.count_only_in_b, c.checkpoint_id, d.reason,
         c.path AS path_c, d.path AS path_d
  FROM d7_run_set_difference d
  LEFT JOIN d7_checkpoints c ON c.instance = d.instance AND c.membership = 'only_in_b'
  WHERE d.instance = '<instance>'
  ORDER BY c.position
Did the route change between the last two runs:
  SELECT d.run_a, d.run_b, d.count_only_in_a, d.count_only_in_b, d.count_in_both,
         c.membership, c.checkpoint_id, d.reason, c.path AS path_c, d.path AS path_d
  FROM d7_run_set_difference d
  LEFT JOIN d7_checkpoints c ON c.instance = d.instance AND c.membership IN ('only_in_a', 'only_in_b')
  WHERE d.instance = '<instance>'
  ORDER BY c.membership, c.position
How many checkpoints each run had that the other did not:
  SELECT d.run_a, d.run_b, d.count_only_in_a, d.count_only_in_b, d.reason, d.path AS path_d
  FROM d7_run_set_difference d
  WHERE d.instance = '<instance>'
Was a3_back on both routes:
  SELECT d.run_a, d.run_b, c.checkpoint_id, c.membership, c.path AS path_c, d.path AS path_d
  FROM d7_checkpoints c JOIN d7_run_set_difference d ON d.instance = c.instance
  WHERE c.instance = '<instance>' AND c.checkpoint_id = 'a3_back'

REPLY
JSON only, one of:
  {"intent": "answer", "sql": "SELECT ..."}
  {"intent": "abstain", "sql": ""}   when these tables cannot answer the question

{feedback}QUESTION
{question}
"""
