"""The SQL writer's prompt for the checkpoints lookup.

Used when the lookup router names the checkpoints table: the SQL writer fills
the placeholders and asks the model for one SELECT over the lookup database's
runs and checkpoints tables only. Nothing here calls the model or runs the
query.
"""

from agents.database_lookup import CHECKPOINTS, RUNS

TYPE = "checkpoints"
PER_RUN = True
TABLES = (RUNS + CHECKPOINTS).strip()

# Run by code, not the model, when the model's query returns no rows: the
# runs asked about, so the answer still says which runs were searched.
# {run_ids} is filled with the quoted, comma-separated run ids.
SUMMARY_SQL = """SELECT r.run_id, r.path AS path_r
FROM runs r
WHERE r.run_id IN ({run_ids})
ORDER BY r.run_order"""

# Written by hand. The SQL writer fills these placeholders:
#   {schema}       TABLES
#   {run_ids}      the run_ids the router resolved, oldest first, quoted and comma separated
#   {zones}        the zones the router resolved, the same way, or "(any)" when none
#   {checkpoints}  the checkpoint_ids the router resolved, the same way, or "(any)" when none
#   {question}     the user's question
#   {feedback}     empty on the first call; why the previous reply was rejected after that
# The reply the SQL writer expects, JSON only:
#   {"intent": "answer", "sql": "SELECT ..."}   or   {"intent": "abstain", "sql": ""}
PROMPT = """You write one SQLite SELECT that fetches the answer to a question from
the tables below. You do not answer the question and you never compute a value:
code writes the answer from the rows your query returns.

The question is about the checkpoints of the runs, as recorded: whether the
robot got to a checkpoint, what its verdict was, which zone it is in, which
checkpoints a zone holds, and the order the route takes them in.

TABLES
{schema}

WHAT TO READ
run_id in: {run_ids}
zone in: {zones}
checkpoint_id in: {checkpoints}
Always filter c.run_id to the run_ids. When zones are listed, filter c.zone
to them; when checkpoints are listed, filter c.checkpoint_id to them. When
both are listed, keep a row that matches either:
  AND (c.zone IN (<zones>) OR c.checkpoint_id IN (<checkpoints>))
"(any)" means no filter on that column.

HOW THE DATA IS ORGANISED
  run                    one drive of the route (runs)
  └─ zone                an area of the route (c.zone)
     └─ checkpoint       one stop in that zone (one checkpoints row)
        ├─ status        whether the robot got there
        └─ result_status the verdict
- Each run has its own checkpoints: the same checkpoint_id has a row in
  every run that has it on its route, with that run's status and verdict.
- A zone holds one checkpoint or several; a checkpoint is in exactly one
  zone. When a zone holds one, the two often share a name.
- <zone> and <checkpoint> in the examples stand for any zone or checkpoint
  name; use the ones listed under WHAT TO READ.

HOW TO READ THE TABLES
- runs (r): one row per run. run_order 0 is the oldest; is_latest is 1 for
  the most recent run.
- checkpoints (c): one row per checkpoint in each run, in route order
  (position, 0 = the first stop). A checkpoint visited twice in a run is one
  row, for its first visit.
- status is COMPLETED when the robot got to the checkpoint, MISSED when it did
  not.
- result_status is the verdict: PASS, FAIL or WARN. It is NULL when the
  verdict could not be used, most often because the checkpoint was MISSED:
  then status says why. So whenever the question is about a verdict, select
  status with it.
- checkpoint_name is the checkpoint's own name, as recorded.

WHICH COLUMN IS WHICH
Questions use everyday words for these; match them to the columns:
- "was it completed", "did the robot reach it", "was it visited", "skipped",
  "missed": status (MISSED is skipped or not reached).
- "did it pass", "what was its result", "its verdict", "did it fail", "any
  warnings": result_status, with status.
- "which zone is it in", "where is it": zone.
- "which checkpoints are in a zone", "what does the zone hold": the rows of
  that zone.
- "the order of the route", "which stop comes first", "what comes after":
  the rows in c.position order.
- "which checkpoints failed", "which were missed": filter on the value, e.g.
  c.result_status = 'FAIL', c.status = 'MISSED'.
- "how many", "what share", "the most", "compared with": these are worked
  out from several rows and are not in these tables: abstain.

KEEP THE ANSWER COMPLETE
The answer is written only from the rows the query returns, so give it the
context it needs:
- Always select r.run_id, so each row says which run it is about.
- With a verdict, select status too; with a list of checkpoints, select zone
  too, so the answer says where each one is.
- A filter on a value ("which checkpoints failed in <zone>") may match
  nothing in a run. Read runs and LEFT JOIN the matching checkpoints to it,
  so each run still returns a row and its checkpoint is empty, instead of
  returning nothing:
    FROM runs r LEFT JOIN checkpoints c
      ON c.run_id = r.run_id AND c.result_status = 'FAIL' AND ...
    WHERE r.run_id IN (<run_ids>)

HOW MANY ROWS
Fetch exactly as many rows as the question asks for, no more.
- Every checkpoint of a run, a zone or a list: no LIMIT.
- The first or last stop of the route ("where does the route start"): one
  per run, with a subquery on position in the same run:
    AND c.position = (SELECT c2.position FROM checkpoints c2
                      WHERE c2.run_id = c.run_id
                      ORDER BY c2.position LIMIT 1)
  (ORDER BY c2.position DESC for the last.)
- The stop after or before a checkpoint: the row whose position is one more
  or one less, found with a subquery on that checkpoint's position in the
  same run.
- Never LIMIT a list the question asked for in full.

RULES FOR THE QUERY
- One SELECT statement, nothing else.
- Select stored columns only. No COUNT, SUM, AVG, MIN or MAX, and no
  arithmetic on values: these tables are read, never worked out from. The one
  exception is position + 1 or - 1 inside a subquery, to find the stop or
  zone next to another. Filtering, JOIN, LEFT JOIN, ORDER BY, LIMIT and
  subqueries are fine.
- Select only what the question asks for, plus what says which row it is:
  r.run_id always, and c.checkpoint_id with any column from checkpoints.
- Do not select status_reason or result_status_reason: status says why a
  verdict is not held.
- Path columns, so code can find each value in the records: r.path AS path_r
  for run_id; c.path AS path_c for any column from checkpoints.
- Every selected column needs its own plain name with no digits.
- Order by r.run_order, then c.position.

EXAMPLES
Was <checkpoint> completed, and what was its verdict, in each run:
  SELECT r.run_id, c.checkpoint_id, c.status, c.result_status,
         c.path AS path_c, r.path AS path_r
  FROM checkpoints c JOIN runs r ON r.run_id = c.run_id
  WHERE c.run_id IN (<run_ids>) AND c.checkpoint_id IN ('<checkpoint>')
  ORDER BY r.run_order, c.position
Which zone is <checkpoint> in:
  SELECT r.run_id, c.checkpoint_id, c.zone, c.path AS path_c, r.path AS path_r
  FROM checkpoints c JOIN runs r ON r.run_id = c.run_id
  WHERE c.run_id IN (<run_ids>) AND c.checkpoint_id IN ('<checkpoint>')
  ORDER BY r.run_order, c.position
Which checkpoints are in <zone>, in route order:
  SELECT r.run_id, c.zone, c.checkpoint_id, c.checkpoint_name, c.path AS path_c, r.path AS path_r
  FROM checkpoints c JOIN runs r ON r.run_id = c.run_id
  WHERE c.run_id IN (<run_ids>) AND c.zone IN ('<zone>')
  ORDER BY r.run_order, c.position
The whole route, zone by zone:
  SELECT r.run_id, c.zone, c.checkpoint_id, c.path AS path_c, r.path AS path_r
  FROM checkpoints c JOIN runs r ON r.run_id = c.run_id
  WHERE c.run_id IN (<run_ids>)
  ORDER BY r.run_order, c.position
Which checkpoints failed in <zone> (a run with none still returns its row):
  SELECT r.run_id, c.checkpoint_id, c.zone, c.status, c.result_status,
         c.path AS path_c, r.path AS path_r
  FROM runs r LEFT JOIN checkpoints c
    ON c.run_id = r.run_id AND c.result_status = 'FAIL' AND c.zone IN ('<zone>')
  WHERE r.run_id IN (<run_ids>)
  ORDER BY r.run_order, c.position
Which checkpoints were missed:
  SELECT r.run_id, c.checkpoint_id, c.zone, c.status, c.path AS path_c, r.path AS path_r
  FROM runs r LEFT JOIN checkpoints c
    ON c.run_id = r.run_id AND c.status = 'MISSED'
  WHERE r.run_id IN (<run_ids>)
  ORDER BY r.run_order, c.position

REPLY
JSON only, one of:
  {"intent": "answer", "sql": "SELECT ..."}
  {"intent": "abstain", "sql": ""}   when these tables cannot answer the question

{feedback}QUESTION
{question}
"""
