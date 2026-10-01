"""The SQL writer's prompt for the evidence lookup.

Used when the lookup router names the evidence table: the SQL writer fills
the placeholders and asks the model for one SELECT over the lookup database's
runs, checkpoints and evidence tables only. Nothing here calls the model or
runs the query.
"""

from agents.database_lookup import CHECKPOINTS, EVIDENCE, RUNS

TYPE = "evidence"
PER_RUN = True
TABLES = (RUNS + CHECKPOINTS + EVIDENCE).strip()

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

The question is about the evidence the runs captured: the image files taken
at each checkpoint, as recorded.

TABLES
{schema}

WHAT TO READ
run_id in: {run_ids}
zone in: {zones}
checkpoint_id in: {checkpoints}
Always filter e.run_id to the run_ids. When zones are listed, filter e.zone
to them; when checkpoints are listed, filter e.checkpoint_id to them. When
both are listed, keep a row that matches either:
  AND (e.zone IN (<zones>) OR e.checkpoint_id IN (<checkpoints>))
"(any)" means no filter on that column.

HOW THE DATA IS ORGANISED
  run                    one drive of the route (runs)
  └─ zone                an area of the route
     └─ checkpoint       one stop in that zone (checkpoints)
        └─ evidence      the images captured there (evidence), one row each
- Each run has its own evidence: the same checkpoint has different images in
  different runs.
- A checkpoint the robot missed, or reached without taking images, has no
  evidence rows.
- <zone> and <checkpoint> in the examples stand for any zone or checkpoint
  name; use the ones listed under WHAT TO READ.

HOW TO READ THE TABLES
- runs (r): one row per run. run_order 0 is the oldest.
- checkpoints (c): one row per checkpoint in each run, in route order
  (position). Join it for the checkpoint and its zone:
    JOIN checkpoints c ON c.run_id = e.run_id AND c.checkpoint_id = e.checkpoint_id
- evidence (e): one row per image file, in the order recorded (position,
  counted within each checkpoint and kind). image_path is the file's path as
  the run recorded it.
- kind says what the image is:
  - evidence_image: a photo the robot took at the checkpoint
  - annotated_image: an image with marks drawn on it
  - finding_image: the image of a finding made at the checkpoint; finding_id
    says which finding
- What an image shows beyond its kind (a thermal image, a direction it
  faces) is only in its file name: match it with LIKE on image_path, e.g.
  e.image_path LIKE '%thermal%'.
- c.status is COMPLETED or MISSED: use it when the question asks why a
  checkpoint has no images.

WHICH COLUMN IS WHICH
Questions use everyday words for these; match them to the columns:
- "images", "photos", "pictures", "evidence", "what was captured": every
  kind, unless the question names one.
- "annotated", "marked up", "labelled": e.kind = 'annotated_image'.
- "images of the findings", "the image of finding <id>": e.kind =
  'finding_image', filtered on e.finding_id when one is named.
- "thermal", "infrared", or any other kind of picture the file names carry:
  LIKE on e.image_path.
- "how many images", "which checkpoint has the most": worked out from
  several rows and not in these tables: abstain.

KEEP THE ANSWER COMPLETE
The answer is written only from the rows the query returns, so give it the
context it needs:
- Always select r.run_id, c.checkpoint_id and e.image_path, so each image
  says which run and checkpoint it is from. Select c.zone too when the
  question is about a zone.
- A checkpoint named in the question may have no images in a run. Read its
  checkpoints row and LEFT JOIN its evidence, so the query still returns the
  checkpoint with its status, and the image is empty, instead of returning
  nothing:
    FROM checkpoints c JOIN runs r ON r.run_id = c.run_id
    LEFT JOIN evidence e ON e.run_id = c.run_id AND e.checkpoint_id = c.checkpoint_id

HOW MANY ROWS
Fetch exactly as many rows as the question asks for, no more.
- Every image of a checkpoint, a zone or a run: no LIMIT.
- One image ("the first image taken at <checkpoint>"): one per checkpoint and
  run, with ORDER BY e.position and a subquery on position in the same run
  and checkpoint.
- Never LIMIT a list the question asked for in full.

RULES FOR THE QUERY
- One SELECT statement, nothing else.
- Select stored columns only. No COUNT, SUM, AVG, MIN or MAX, and no
  arithmetic. Filtering, LIKE, LEFT JOIN, ORDER BY, LIMIT and subqueries are
  fine.
- Never select e.kind, e.finding_id, e.zone, e.checkpoint_id or e.position:
  code cannot find them in the records. Filter on them in WHERE; take the
  checkpoint and zone from checkpoints (c.checkpoint_id, c.zone).
- Path columns, so code can find each value in the records: r.path AS path_r
  for run_id; c.path AS path_c for any column from checkpoints; e.path AS
  path_e for image_path.
- Every selected column needs its own plain name with no digits.
- Order by r.run_order, then c.position, then e.kind and e.position.

EXAMPLES
Which images were taken at <checkpoint>:
  SELECT r.run_id, c.checkpoint_id, e.image_path,
         e.path AS path_e, c.path AS path_c, r.path AS path_r
  FROM evidence e JOIN runs r ON r.run_id = e.run_id
  JOIN checkpoints c ON c.run_id = e.run_id AND c.checkpoint_id = e.checkpoint_id
  WHERE e.run_id IN (<run_ids>) AND e.checkpoint_id IN ('<checkpoint>')
  ORDER BY r.run_order, c.position, e.kind, e.position
The thermal images in <zone>:
  SELECT r.run_id, c.zone, c.checkpoint_id, e.image_path,
         e.path AS path_e, c.path AS path_c, r.path AS path_r
  FROM evidence e JOIN runs r ON r.run_id = e.run_id
  JOIN checkpoints c ON c.run_id = e.run_id AND c.checkpoint_id = e.checkpoint_id
  WHERE e.run_id IN (<run_ids>) AND e.zone IN ('<zone>') AND e.image_path LIKE '%thermal%'
  ORDER BY r.run_order, c.position, e.kind, e.position
The annotated images at <checkpoint>:
  SELECT r.run_id, c.checkpoint_id, e.image_path,
         e.path AS path_e, c.path AS path_c, r.path AS path_r
  FROM evidence e JOIN runs r ON r.run_id = e.run_id
  JOIN checkpoints c ON c.run_id = e.run_id AND c.checkpoint_id = e.checkpoint_id
  WHERE e.run_id IN (<run_ids>) AND e.checkpoint_id IN ('<checkpoint>')
    AND e.kind = 'annotated_image'
  ORDER BY r.run_order, c.position, e.position
The images at <checkpoint>, or why there are none (a missed stop still returns its row):
  SELECT r.run_id, c.checkpoint_id, c.status, e.image_path,
         e.path AS path_e, c.path AS path_c, r.path AS path_r
  FROM checkpoints c JOIN runs r ON r.run_id = c.run_id
  LEFT JOIN evidence e ON e.run_id = c.run_id AND e.checkpoint_id = c.checkpoint_id
  WHERE c.run_id IN (<run_ids>) AND c.checkpoint_id IN ('<checkpoint>')
  ORDER BY r.run_order, c.position, e.kind, e.position

REPLY
JSON only, one of:
  {"intent": "answer", "sql": "SELECT ..."}
  {"intent": "abstain", "sql": ""}   when these tables cannot answer the question

{feedback}QUESTION
{question}
"""
