"""The SQL writer's prompt for the checkpoint_readings lookup.

Used when the lookup router names the checkpoint_readings table: the SQL
writer fills the placeholders and asks the model for one SELECT over the
lookup database's runs, checkpoints and checkpoint_readings tables only.
Nothing here calls the model or runs the query.
"""

from agents.database_lookup import CHECKPOINTS, READINGS, RUNS

TYPE = "checkpoint_readings"
PER_RUN = True
TABLES = (RUNS + CHECKPOINTS + READINGS).strip()

# Shown as code wrote them, with no citation: why eligibility left a reading
# out is its own verdict, held in no field of the records (b1_analytical.build_lines).
TEXT_COLUMNS = ("reason",)

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

The question is about the sensor readings taken at checkpoints, as recorded:
what a reading was, or why there is no usable reading.

TABLES
{schema}

WHAT TO READ
run_id in: {run_ids}
zone in: {zones}
checkpoint_id in: {checkpoints}
Always filter g.run_id to the run_ids. When zones are listed, filter g.zone
to them; when checkpoints are listed, filter g.checkpoint_id to them. When
both are listed, keep a row that matches either:
  AND (g.zone IN (<zones>) OR g.checkpoint_id IN (<checkpoints>))
"(any)" means no filter on that column.

HOW THE DATA IS ORGANISED
  run                    one drive of the route (runs)
  └─ zone                an area of the route
     └─ checkpoint       one stop in that zone (checkpoints)
        └─ readings      by sensor block, then field (checkpoint_readings):
                         each one either its value, or left out with the reason
- Each run has its own readings: the same checkpoint has different readings
  in different runs.
- The blocks and their fields:
  - accelerometer: accel_x, accel_y, accel_z, vibration_peak, vibration_rms_g
    (vibration, shaking)
  - environment: temperature_c, humidity_pct, pressure_hpa (temperature,
    heat, humidity, pressure)
  - particulate: pm1_0, pm2_5, pm4_0, pm10 (particles, dust, air quality)
- <zone> and <checkpoint> in the examples stand for any zone or checkpoint
  name; use the ones listed under WHAT TO READ.

HOW TO READ THE TABLES
- runs (r): one row per run. run_order 0 is the oldest.
- checkpoints (c): one row per checkpoint in each run, in route order
  (position), with its status (COMPLETED or MISSED). Join it for the
  checkpoint and its zone:
    JOIN checkpoints c ON c.run_id = g.run_id AND c.checkpoint_id = g.checkpoint_id
- checkpoint_readings (g): one row per field of each reading at a
  checkpoint. Every field has a row, one of two kinds (g.status):
  - included: the reading could be used. value is the reading, timestamp
    when it was read.
  - excluded: the reading could not be used. value is empty, and reason says
    why: the sensor was flagged faulty (e.g. sps30_ok=false), was offline,
    or the checkpoint was missed (checkpoint status is MISSED).
  A value is never both. An excluded reading has no value to give: select
  its reason instead.
- A checkpoint visited twice in a run has a row per visit for each field.

WHICH COLUMN IS WHICH
Questions use everyday words for these; match them to the columns:
- "the temperature at <checkpoint>", "what was the vibration": g.value, with
  g.field, filtered to the field the question names.
- "the accelerometer values", "all the environment readings": every field of
  that block, g.block = '<block>'.
- "all the readings", "what did the sensors record": every field.
- "why is there no <reading>", "was the <reading> usable", "which readings
  were left out": g.reason, with g.field, filtered to g.status = 'excluded'.
- "when was it read": g.timestamp.
- "the highest", "the average", "how many", "compared with": worked out from
  several readings and not in these tables: abstain.

KEEP THE ANSWER COMPLETE
The answer is written only from the rows the query returns, so give it the
context it needs:
- Always select r.run_id, c.checkpoint_id and g.field, so each reading says
  which run, checkpoint and field it is. Select c.zone too when the question
  is about a zone.
- Select g.value and g.reason together, whether the question asks for a
  value or not: an included row fills value, an excluded row fills reason,
  so the answer gives the reading or why there is none, never a gap.

HOW MANY ROWS
Fetch exactly as many rows as the question asks for, no more.
- The fields and checkpoints the question names, in the runs named: no
  LIMIT.
- Never LIMIT a list the question asked for in full.

RULES FOR THE QUERY
- One SELECT statement, nothing else.
- Select stored columns only. No COUNT, SUM, AVG, MIN or MAX, and no
  arithmetic. Filtering, JOIN, ORDER BY and subqueries are fine.
- Never select g.run_id, g.zone, g.checkpoint_id, g.status, g.count or
  g.stale: code cannot find them in the records. Take the run from r, the
  checkpoint and zone from c, and filter on the rest in WHERE.
- Path columns, so code can find each value in the records: r.path AS path_r
  for run_id; c.path AS path_c for any column from checkpoints; g.path AS
  path_g for field, block, value and reason; g.path_item AS path_item for
  timestamp.
- Every selected column needs its own plain name with no digits.
- Order by r.run_order, then c.position, then g.block and g.field.

EXAMPLES
The temperature at <checkpoint>, or why there is none:
  SELECT r.run_id, c.checkpoint_id, g.field, g.value, g.reason,
         g.path AS path_g, c.path AS path_c, r.path AS path_r
  FROM checkpoint_readings g JOIN runs r ON r.run_id = g.run_id
  JOIN checkpoints c ON c.run_id = g.run_id AND c.checkpoint_id = g.checkpoint_id
  WHERE g.run_id IN (<run_ids>) AND g.checkpoint_id IN ('<checkpoint>')
    AND g.field = 'temperature_c'
  ORDER BY r.run_order, c.position, g.block, g.field
The accelerometer values at <checkpoint>, and when they were read:
  SELECT r.run_id, c.checkpoint_id, g.field, g.value, g.reason, g.timestamp,
         g.path AS path_g, g.path_item AS path_item, c.path AS path_c, r.path AS path_r
  FROM checkpoint_readings g JOIN runs r ON r.run_id = g.run_id
  JOIN checkpoints c ON c.run_id = g.run_id AND c.checkpoint_id = g.checkpoint_id
  WHERE g.run_id IN (<run_ids>) AND g.checkpoint_id IN ('<checkpoint>')
    AND g.block = 'accelerometer'
  ORDER BY r.run_order, c.position, g.block, g.field
Why there is no PM2.5 reading in <zone>:
  SELECT r.run_id, c.zone, c.checkpoint_id, g.field, g.reason,
         g.path AS path_g, c.path AS path_c, r.path AS path_r
  FROM checkpoint_readings g JOIN runs r ON r.run_id = g.run_id
  JOIN checkpoints c ON c.run_id = g.run_id AND c.checkpoint_id = g.checkpoint_id
  WHERE g.run_id IN (<run_ids>) AND g.zone IN ('<zone>')
    AND g.field = 'pm2_5' AND g.status = 'excluded'
  ORDER BY r.run_order, c.position, g.block, g.field
Every reading at <checkpoint>:
  SELECT r.run_id, c.checkpoint_id, g.block, g.field, g.value, g.reason,
         g.path AS path_g, c.path AS path_c, r.path AS path_r
  FROM checkpoint_readings g JOIN runs r ON r.run_id = g.run_id
  JOIN checkpoints c ON c.run_id = g.run_id AND c.checkpoint_id = g.checkpoint_id
  WHERE g.run_id IN (<run_ids>) AND g.checkpoint_id IN ('<checkpoint>')
  ORDER BY r.run_order, c.position, g.block, g.field

REPLY
JSON only, one of:
  {"intent": "answer", "sql": "SELECT ..."}
  {"intent": "abstain", "sql": ""}   when these tables cannot answer the question

{feedback}QUESTION
{question}
"""
