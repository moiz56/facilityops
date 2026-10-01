"""The SQL writer's prompt for the zones lookup.

Used when the lookup router names the zones table: the SQL writer fills the
placeholders and asks the model for one SELECT over the lookup database's
runs, zones and checkpoints tables only. Nothing here calls the model or runs
the query.
"""

from agents.database_lookup import CHECKPOINTS, RUNS, ZONES

TYPE = "zones"
PER_RUN = True
TABLES = (RUNS + ZONES + CHECKPOINTS).strip()

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

The question is about the zones of the runs, as recorded: which zones a run's
route passed through, in what order, and which checkpoints each one holds.

TABLES
{schema}

WHAT TO READ
run_id in: {run_ids}
zone in: {zones}
checkpoint_id in: {checkpoints}
Always filter z.run_id to the run_ids. When zones are listed, filter z.zone to
them. When checkpoints are listed, the question is about the zones those
checkpoints are in: join checkpoints and filter c.checkpoint_id to them.
"(any)" means no filter on that column.

HOW THE DATA IS ORGANISED
  run                    one drive of the route (runs)
  └─ zone                an area of the route (zones), in the order the route reaches it
     └─ checkpoint       one stop in that zone (checkpoints)
- Each run has its own zones: two runs can pass through different zones, or
  reach them in a different order.
- A zone holds one checkpoint or several; a checkpoint is in exactly one
  zone. When a zone holds one, the two often share a name.
- <zone> and <checkpoint> in the examples stand for any zone or checkpoint
  name; use the ones listed under WHAT TO READ.

HOW TO READ THE TABLES
- runs (r): one row per run. run_order 0 is the oldest.
- zones (z): one row per zone in each run, in the order the route first
  reaches it (position, 0 = the first zone). A zone the route returns to
  later is still one row, at its first position.
- checkpoints (c): one row per checkpoint in each run, with its zone and its
  place in the whole route (position). Join it for a zone's checkpoints:
    JOIN checkpoints c ON c.run_id = z.run_id AND c.zone = z.zone

WHICH COLUMN IS WHICH
Questions use everyday words for these; match them to the columns:
- "which zones", "which areas", "what did the route cover": every zone of
  the run, in z.position order.
- "which zone comes first", "where does the route start", "the last zone":
  the zone at the first or last position (see HOW MANY ROWS).
- "which zone comes after <zone>", "before <zone>": the zone whose position
  is one more or one less, found with a subquery on <zone>'s position in the
  same run.
- "which zone is <checkpoint> in": the zone joined to that checkpoint.
- "the zones and their checkpoints", "what does each zone hold": each zone
  with its checkpoints, joined.
- "how many zones", "which zone has the most checkpoints", "which zones did
  one run have and another not": worked out from several rows and not in
  these tables: abstain.

KEEP THE ANSWER COMPLETE
The answer is written only from the rows the query returns, so give it the
context it needs:
- Always select r.run_id and z.zone, so each row says which run and zone it
  is.
- With a zone's checkpoints, select c.checkpoint_id too, one row per
  checkpoint.

HOW MANY ROWS
Fetch exactly as many rows as the question asks for, no more.
- Every zone of a run: no LIMIT.
- The first or last zone: one per run, with a subquery on position in the
  same run:
    AND z.position = (SELECT z2.position FROM zones z2
                      WHERE z2.run_id = z.run_id
                      ORDER BY z2.position LIMIT 1)
  (ORDER BY z2.position DESC for the last.)
- Never LIMIT a list the question asked for in full.

RULES FOR THE QUERY
- One SELECT statement, nothing else.
- Select stored columns only. No COUNT, SUM, AVG, MIN or MAX, and no
  arithmetic on values: these tables are read, never worked out from. The one
  exception is position + 1 or - 1 inside a subquery, to find the stop or
  zone next to another. Filtering, JOIN, LEFT JOIN, ORDER BY, LIMIT and
  subqueries are fine.
- Never select z.run_id or z.position, nor c.run_id or c.position: code
  cannot show them. Take the run from r, and order by position.
- Path columns, so code can find each value in the records: r.path AS path_r
  for run_id; z.path AS path_z for zone; c.path AS path_c for any column
  from checkpoints.
- Every selected column needs its own plain name with no digits.
- Order by r.run_order, then z.position, then c.position.

EXAMPLES
Which zones the route covered, in order:
  SELECT r.run_id, z.zone, z.path AS path_z, r.path AS path_r
  FROM zones z JOIN runs r ON r.run_id = z.run_id
  WHERE z.run_id IN (<run_ids>)
  ORDER BY r.run_order, z.position
The first zone of the route:
  SELECT r.run_id, z.zone, z.path AS path_z, r.path AS path_r
  FROM zones z JOIN runs r ON r.run_id = z.run_id
  WHERE z.run_id IN (<run_ids>)
    AND z.position = (SELECT z2.position FROM zones z2
                      WHERE z2.run_id = z.run_id
                      ORDER BY z2.position LIMIT 1)
  ORDER BY r.run_order
The zone after <zone>:
  SELECT r.run_id, z.zone, z.path AS path_z, r.path AS path_r
  FROM zones z JOIN runs r ON r.run_id = z.run_id
  WHERE z.run_id IN (<run_ids>)
    AND z.position = (SELECT z2.position + 1 FROM zones z2
                      WHERE z2.run_id = z.run_id AND z2.zone = '<zone>')
  ORDER BY r.run_order
Each zone and its checkpoints:
  SELECT r.run_id, z.zone, c.checkpoint_id, z.path AS path_z, c.path AS path_c, r.path AS path_r
  FROM zones z JOIN runs r ON r.run_id = z.run_id
  JOIN checkpoints c ON c.run_id = z.run_id AND c.zone = z.zone
  WHERE z.run_id IN (<run_ids>)
  ORDER BY r.run_order, z.position, c.position
Which zone <checkpoint> is in:
  SELECT r.run_id, c.checkpoint_id, z.zone, z.path AS path_z, c.path AS path_c, r.path AS path_r
  FROM zones z JOIN runs r ON r.run_id = z.run_id
  JOIN checkpoints c ON c.run_id = z.run_id AND c.zone = z.zone
  WHERE z.run_id IN (<run_ids>) AND c.checkpoint_id IN ('<checkpoint>')
  ORDER BY r.run_order, z.position

REPLY
JSON only, one of:
  {"intent": "answer", "sql": "SELECT ..."}
  {"intent": "abstain", "sql": ""}   when these tables cannot answer the question

{feedback}QUESTION
{question}
"""
