"""The SQL writer's prompt for D10 (evidence_count).

Used when the router names an evidence_count instance: the SQL writer fills
the placeholders and asks the model for one SELECT over the runs and D10 tables
only: how many evidence images each run and each checkpoint captured, how many
are thermal or RGB, and how many resolve to a readable file or do not.
Nothing here calls the model or runs the query.
"""

from agents.database_derivation import D10, RUNS

TYPE = "evidence_count"
TABLES = (RUNS + D10).strip()

# Run by code, not the model, when the model's query returns no rows: how many
# images each routed run captured, so "none" is stated as a stored count.
# reason is NULL for an OK row. {instances} and {run_ids} are filled with
# quoted, comma-separated ids.
SUMMARY_SQL = """SELECT r.run_id, d.count, d.reason, d.path AS path_d, r.path AS path_r
FROM d10_evidence_count d JOIN runs r ON r.run_id = d.run_id
WHERE d.instance IN ({instances}) AND d.run_id IN ({run_ids})
ORDER BY r.run_order, d.instance"""

# Written by hand. The SQL writer fills these placeholders:
#   {schema}       TABLES
#   {instances}    one line per routed D10 instance, as b1_analytical_router_derived.describe writes it
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

The question is about how many evidence images were captured: in each run and
at each checkpoint, how many images, how many are thermal or RGB, and how many
resolve to a readable file or do not.

TABLES
{schema}

THE COUNTS THE QUESTION USES (the instance column)
{instances}

WHAT TO READ
run_id in: {run_ids}
zone in: {zones}
checkpoint_id in: {checkpoints}
Filter to exactly these runs and these counts. When zones or checkpoints are
listed, read d10_by_checkpoint and filter b.zone or b.checkpoint_id to them;
when both are listed, keep a row that matches either:
  AND (b.zone IN (<zones>) OR b.checkpoint_id IN (<checkpoints>))
"(any)" means no filter on that column.

HOW TO READ THE TABLES
- d10_evidence_count has one row per run: the counts for the whole run,
  already counted. 0 is a real count: the run captured none.
- d10_by_checkpoint has one row per checkpoint in each run, in route order
  (position), with the same counts for that checkpoint alone. A checkpoint
  that captured no images still has its row, with count 0. checkpoint_name
  and zone are the checkpoint's, as recorded.
- count is every image. thermal_count and rgb_count split it by kind;
  resolved_count and unresolved_count split it by whether the file works.
- The images counted are the photos the robot took at each checkpoint.
  Annotated images and the images of findings are not counted here.
- Thermal is read from the image's file name; every other image is RGB.
- An image resolves when its file is there and opens as an image. It does
  not resolve when the file is missing, empty or will not open.
- resolved_count and unresolved_count are NULL when the image files were not
  checked: select them all the same, and code reports that.
- status NOT_COMPUTABLE on d10_evidence_count means the counts could not be
  made for that run: select its reason.
- runs gives each run's start_time, and run_order for ordering (0 = oldest).

WHICH COLUMN IS WHICH
Questions use everyday words for these; match them to the columns:
- "images", "photos", "pictures", "evidence", "captured": count
- "thermal", "infrared": thermal_count
- "RGB", "colour", "normal", "visible", "not thermal": rgb_count
- "resolve", "load", "open", "work", "are available": resolved_count
- "broken", "missing", "failed", "unreadable", "do not resolve":
  unresolved_count
- "which checkpoints captured no images", "have no evidence": b.count = 0.
  The same for one kind: "no thermal images" is b.thermal_count = 0.
- "which checkpoints have broken images": b.unresolved_count > 0.

WHAT THESE TABLES DO NOT HOLD
- No single image: which images, their file names, which ones are thermal or
  which ones failed to resolve. Abstain for those.
- No total per zone. For "how many images in <zone>", select the zone's
  checkpoints from d10_by_checkpoint, one row each. Abstain when the
  question asks for one total over the zone.

HOW MANY ROWS
Fetch exactly as many rows as the question asks for, no more.
- Every run or every checkpoint asked about: no LIMIT.
- The checkpoint with the most or fewest images in each run: one row per
  run, keeping ties, with a subquery on the same run:
    AND b.count = (SELECT b2.count FROM d10_by_checkpoint b2
                   WHERE b2.instance = b.instance AND b2.run_id = b.run_id
                   ORDER BY b2.count DESC LIMIT 1)
  (ORDER BY b2.count ASC for the fewest; another count column, e.g.
  thermal_count, on both sides for one kind.)
- The run with the most or fewest images: one row overall, keeping ties,
  with a subquery over the listed runs:
    AND d.count = (SELECT d2.count FROM d10_evidence_count d2
                   WHERE d2.instance = d.instance AND d2.run_id IN (<run_ids>)
                   ORDER BY d2.count DESC LIMIT 1)
- Never LIMIT a list the question asked for in full.

RULES FOR THE QUERY
- One statement: a SELECT, or two SELECTs joined with UNION ALL when the run
  total and the checkpoints are asked for together (see EXAMPLES). Nothing
  else.
- Select stored columns only. No COUNT, SUM, AVG, MIN or MAX, and no
  arithmetic on values: every count is already in a table. ORDER BY, LIMIT
  and subqueries are fine (see HOW MANY ROWS).
- Select only what the question asks for, plus run_id so each row says which
  run it is about, and b.checkpoint_id on every d10_by_checkpoint row. Add
  b.checkpoint_name, and b.zone when the question is about a zone. Add
  d.status and d.reason only when a count is NOT_COMPUTABLE.
- Never rename a value column: select count, thermal_count, rgb_count,
  resolved_count and unresolved_count under their own names. Code finds each
  value by its column name, and a renamed count cannot be found.
- Path columns, so code can find each value in the records:
  r.path AS path_r for run_id; d.path AS path_d for any column from
  d10_evidence_count; b.path AS path_b for checkpoint_id and the counts from
  d10_by_checkpoint; b.path_record AS path_record for checkpoint_name and
  zone. Select only the ones the shown columns need.
- Every selected column needs its own plain name with no digits.
- Order by r.run_order, then b.position for checkpoint rows.

EXAMPLES
How many images did each run capture:
  SELECT r.run_id, d.count, d.path AS path_d, r.path AS path_r
  FROM d10_evidence_count d JOIN runs r ON r.run_id = d.run_id
  WHERE d.instance = '<instance>' AND d.run_id IN (<run_ids>)
  ORDER BY r.run_order
How many images at each checkpoint:
  SELECT r.run_id, b.checkpoint_id, b.checkpoint_name, b.count,
         b.path AS path_b, b.path_record AS path_record, r.path AS path_r
  FROM d10_by_checkpoint b JOIN runs r ON r.run_id = b.run_id
  WHERE b.instance = '<instance>' AND b.run_id IN (<run_ids>)
  ORDER BY r.run_order, b.position
Which checkpoints captured no evidence:
  SELECT r.run_id, b.checkpoint_id, b.checkpoint_name, b.count,
         b.path AS path_b, b.path_record AS path_record, r.path AS path_r
  FROM d10_by_checkpoint b JOIN runs r ON r.run_id = b.run_id
  WHERE b.instance = '<instance>' AND b.run_id IN (<run_ids>) AND b.count = 0
  ORDER BY r.run_order, b.position
How many thermal and RGB images at <checkpoint>:
  SELECT r.run_id, b.checkpoint_id, b.thermal_count, b.rgb_count,
         b.path AS path_b, r.path AS path_r
  FROM d10_by_checkpoint b JOIN runs r ON r.run_id = b.run_id
  WHERE b.instance = '<instance>' AND b.run_id IN (<run_ids>)
    AND b.checkpoint_id IN ('<checkpoint>')
  ORDER BY r.run_order
How many images failed to resolve, in each run:
  SELECT r.run_id, d.unresolved_count, d.path AS path_d, r.path AS path_r
  FROM d10_evidence_count d JOIN runs r ON r.run_id = d.run_id
  WHERE d.instance = '<instance>' AND d.run_id IN (<run_ids>)
  ORDER BY r.run_order
How many images in all, and at each checkpoint (the run's row first, then
one row per checkpoint; both counts stay count):
  SELECT r.run_id, r.run_order, NULL AS position, NULL AS checkpoint_id, d.count,
         d.path AS path_d, NULL AS path_b, r.path AS path_r
  FROM d10_evidence_count d JOIN runs r ON r.run_id = d.run_id
  WHERE d.instance = '<instance>' AND d.run_id IN (<run_ids>)
  UNION ALL
  SELECT r.run_id, r.run_order, b.position, b.checkpoint_id, b.count,
         NULL, b.path, r.path
  FROM d10_by_checkpoint b JOIN runs r ON r.run_id = b.run_id
  WHERE b.instance = '<instance>' AND b.run_id IN (<run_ids>)
  ORDER BY run_order, position
Which checkpoint captured the most images, in each run:
  SELECT r.run_id, b.checkpoint_id, b.checkpoint_name, b.count,
         b.path AS path_b, b.path_record AS path_record, r.path AS path_r
  FROM d10_by_checkpoint b JOIN runs r ON r.run_id = b.run_id
  WHERE b.instance = '<instance>' AND b.run_id IN (<run_ids>)
    AND b.count = (SELECT b2.count FROM d10_by_checkpoint b2
                   WHERE b2.instance = b.instance AND b2.run_id = b.run_id
                   ORDER BY b2.count DESC LIMIT 1)
  ORDER BY r.run_order, b.position

REPLY
JSON only, one of:
  {"intent": "answer", "sql": "SELECT ..."}
  {"intent": "abstain", "sql": ""}   when these tables cannot answer the question

{feedback}QUESTION
{question}
"""
