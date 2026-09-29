"""The SQL writer's prompt for D4 (group_mean).

Used when the router names one or more group_mean instances: the SQL writer
fills the placeholders and asks the model for one SELECT over the runs table
and the routed instances' d4_<instance> tables only. Each instance is a mean
of a different field, so each has its own table; a question about several
means combines their tables with UNION ALL. Nothing here calls the model or
runs the query.
"""

from typing import Sequence

from agents.database import RUNS, d4_schema
from agents.schema import DerivationConfig

TYPE = "group_mean"


def tables(names: Sequence[str], derivation: DerivationConfig) -> str:
    """The runs table and the routed instances' tables, as the database creates them."""
    return (RUNS + d4_schema(derivation, names)).strip()


def summary_sql(names: Sequence[str]) -> str:
    """Run by code, not the model, when the model's query returns no rows:
    every group's mean, or its reason, in each routed run, for each routed
    mean. {run_ids} is filled with the quoted, comma-separated run ids.
    run_order and position are only there to order the rows; code does not
    show them. The names are instance names d4_schema already checked.
    """
    select = (
        "SELECT r.run_id, r.run_order, d.instance, d.position, d.group_key, d.mean, d.reason, "
        "d.path AS path_d, r.path AS path_r\n"
        "FROM d4_{name} d JOIN runs r ON r.run_id = d.run_id\n"
        "WHERE d.run_id IN ({{run_ids}})"
    )
    return "\nUNION ALL\n".join(select.format(name=name) for name in names) + "\nORDER BY run_order, instance, position"


# Written by hand. The SQL writer fills these placeholders:
#   {schema}       tables(): the runs table and the routed instances' tables
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

The question is about means: in each run, the mean of a sensor reading over
the run's samples, worked out separately for each group, usually each zone of
the route.

TABLES
{schema}

The question may be about one of these means or several.

ZONES AND CHECKPOINTS
The route has two levels:
- A zone is an area of the route: a row of racks (rowA_back), a place (home),
  or a stretch named like its checkpoint (checkpoint_1).
- A checkpoint is one stop inside a zone: a single rack (a3_back). A zone
  holds one checkpoint or several. When it holds one, the two often share a
  name.
Samples are tagged with the zone they were taken in, never with a checkpoint.
So every mean here is a zone's mean, over all its readings; there is no mean
of a checkpoint on its own.
- A question about a zone ("in rowA_back", "per zone"): read d4_<instance> and
  filter d.group_key to the zones listed under WHAT TO READ, or group by it.
- A question about a checkpoint ("at a3_back"): find its zone through
  d4_<instance>_checkpoints, and answer with that zone's mean. Select the
  checkpoint and the zone, so the answer says whose mean it is.
- A question about a zone's checkpoints ("which checkpoints are in rowA_back")
  is d4_<instance>_checkpoints alone.

WHICH READING IS WHICH
The question uses everyday words; match them to the sensor block:
- vibration, shaking: accelerometer (the ADXL345 sensor), e.g.
  accelerometer.vibration_rms_g
- temperature, heat, how hot or cold: environment (the BME680 sensor),
  environment.temperature_c. Humidity and pressure are environment too.
- particulate, particles, dust, air quality, PM2.5, PM10: particulate (the
  SPS30 sensor), e.g. particulate.pm2_5
Use only the tables whose reading the question asks about.

WHAT TO READ
run_id in: {run_ids}
zone in: {zones}
checkpoint_id in: {checkpoints}
Read only the tables of the means listed, filtered to exactly these runs.
When zones are listed, filter d.group_key to them; when checkpoints are
listed, find their zones through d4_<instance>_checkpoints. "(any)" means no
filter of that kind.

HOW TO READ THE TABLES
- d4_<instance> (d): one row per group and run. group_key is the group: with
  "per zone" it is the zone the samples were taken in.
- d4_<instance>_checkpoints (c): the checkpoints in each zone, by run, with
  checkpoint_id and checkpoint_name. For a checkpoint's mean, when
  checkpoints are listed above, join c to d on c.run_id = d.run_id AND
  c.group_key = d.group_key, and filter c.checkpoint_id to them. A checkpoint
  with no row in c is in no zone of that run.
- With "per run_id" each run has one group, the whole run: group_key is the
  run_id.
- mean is already worked out over n readings; inputs_excluded readings were
  left out by the exclusion rule, and inputs_stale of the n were stale.
- A group row with status NOT_COMPUTABLE had every reading excluded: mean is
  NULL and reason says why. It has no mean; it is never 0.
- A row with group_key NULL and status NOT_COMPUTABLE means no group in that
  run could be worked out (e.g. the sensor was off at every checkpoint);
  reason says why. When you filter group_key, keep these rows too:
  (d.group_key IN (...) OR d.group_key IS NULL), so such a run says why
  instead of disappearing.
- There is no mean over a whole run unless a table is grouped per run_id. The
  mean of the zone means is not the run's mean. If the question needs a run's
  overall mean and no listed table is per run_id, abstain.
- runs gives run_order for ordering runs (0 = oldest).

SHAPES OF QUESTION
- One mean: select from its table.
- Several means (any two, or all three): one SELECT per table, each with the
  same columns in the same order, joined with UNION ALL. Select d.instance in
  each, so every row says which mean it is. Put one ORDER BY at the end, using
  result column names; select r.run_order and d.position so it can use them.
  Code does not show run_order or position.
- Per zone: select group_key. One zone: filter group_key. A checkpoint, or
  several: join d4_<instance>_checkpoints as above.
- Per run: every group in each run named, ordered by run.
- One checkpoint across runs ("the mean temperature at checkpoint_1 in each
  run"): join d4_<instance>_checkpoints, filter c.checkpoint_id, order by run.
- Highest or lowest: see HOW MANY ROWS. Only within one mean; never compare
  means of different readings with each other.

HOW MANY ROWS
Fetch exactly as many rows as the question asks for, no more.
- All of them ("the mean per zone"): no LIMIT.
- The single highest or lowest. First decide: one per run, or one overall?
  - The zone with the highest mean, per run ("which zone was hottest", in one
    run or "in each run"): keep the rows whose mean equals that run's top
    mean, found with a subquery on the same run and table. It works for one
    run or many, and keeps ties:
      AND d.status = 'OK'
      AND d.mean = (SELECT d2.mean FROM d4_<instance> d2
                    WHERE d2.run_id = d.run_id AND d2.status = 'OK'
                    ORDER BY d2.mean DESC LIMIT 1)
  - The run with the highest mean in one zone ("which run was hottest at
    checkpoint_1"): the same subquery with d2.group_key = d.group_key AND
    d2.run_id IN (<run_ids>) instead of d2.run_id = d.run_id.
  - With several means in a UNION ALL, each SELECT gets its own subquery on
    its own table.
- A number of them ("the three hottest zones"): with one run and one mean,
  ORDER BY d.mean DESC LIMIT that number. Otherwise do not LIMIT, as it cuts
  across runs and means: order by run, then mean.
- Never LIMIT a list the question asked for in full.

RULES FOR THE QUERY
- One statement: a SELECT, or SELECTs joined with UNION ALL. Nothing else.
- Select stored columns only. No AVG, SUM, COUNT, MIN or MAX, and no
  arithmetic on values: every mean is already in a table, and a mean is never
  averaged again. ORDER BY, LIMIT and subqueries are fine (see HOW MANY ROWS).
- Select only what the question asks for, plus run_id, group_key for group
  rows, and instance when there is more than one mean. "What was the mean"
  needs mean; "over how many readings" adds n; inputs_excluded and
  inputs_stale only when the question asks about left-out or stale readings.
  Always select reason: it is empty on an OK row, so it only shows when a
  group or run could not be worked out.
- Path columns, so code can find each value in the records: r.path AS path_r
  for run_id; d.path AS path_d for any column from a d4_<instance> table;
  c.path AS path_c for any column from a d4_<instance>_checkpoints table. In a
  UNION ALL every SELECT has the same ones.
- Every selected column needs its own plain name with no digits.
- Order one table by r.run_order, then d.position. Order a UNION ALL by
  run_order, instance, position.

EXAMPLES
The mean in one zone, rowA_back, in each run:
  SELECT r.run_id, d.group_key, d.mean, d.reason, d.path AS path_d, r.path AS path_r
  FROM d4_<instance> d JOIN runs r ON r.run_id = d.run_id
  WHERE d.run_id IN (<run_ids>) AND (d.group_key = 'rowA_back' OR d.group_key IS NULL)
  ORDER BY r.run_order, d.position
Which checkpoints are in rowA_back:
  SELECT r.run_id, c.group_key, c.checkpoint_id, c.checkpoint_name, c.path AS path_c, r.path AS path_r
  FROM d4_<instance>_checkpoints c JOIN runs r ON r.run_id = c.run_id
  WHERE c.run_id IN (<run_ids>) AND c.group_key = 'rowA_back'
  ORDER BY r.run_order, c.position
The mean per zone:
  SELECT r.run_id, d.group_key, d.mean, d.reason, d.path AS path_d, r.path AS path_r
  FROM d4_<instance> d JOIN runs r ON r.run_id = d.run_id
  WHERE d.run_id IN (<run_ids>)
  ORDER BY r.run_order, d.position
The mean at a3_back, in each run (its zone's mean):
  SELECT r.run_id, c.checkpoint_id, d.group_key, d.mean, d.reason,
         c.path AS path_c, d.path AS path_d, r.path AS path_r
  FROM d4_<instance> d JOIN runs r ON r.run_id = d.run_id
  JOIN d4_<instance>_checkpoints c ON c.run_id = d.run_id AND c.group_key = d.group_key
  WHERE d.run_id IN (<run_ids>) AND c.checkpoint_id IN ('a3_back')
  ORDER BY r.run_order, d.position
Two means per zone:
  SELECT r.run_id, r.run_order, d.instance, d.position, d.group_key, d.mean, d.reason,
         d.path AS path_d, r.path AS path_r
  FROM d4_<first instance> d JOIN runs r ON r.run_id = d.run_id
  WHERE d.run_id IN (<run_ids>)
  UNION ALL
  SELECT r.run_id, r.run_order, d.instance, d.position, d.group_key, d.mean, d.reason,
         d.path AS path_d, r.path AS path_r
  FROM d4_<second instance> d JOIN runs r ON r.run_id = d.run_id
  WHERE d.run_id IN (<run_ids>)
  ORDER BY run_order, instance, position
The zone with the highest mean, in each run:
  SELECT r.run_id, d.group_key, d.mean, d.path AS path_d, r.path AS path_r
  FROM d4_<instance> d JOIN runs r ON r.run_id = d.run_id
  WHERE d.run_id IN (<run_ids>) AND d.status = 'OK'
    AND d.mean = (SELECT d2.mean FROM d4_<instance> d2
                  WHERE d2.run_id = d.run_id AND d2.status = 'OK'
                  ORDER BY d2.mean DESC LIMIT 1)
  ORDER BY r.run_order, d.position

REPLY
JSON only, one of:
  {"intent": "answer", "sql": "SELECT ..."}
  {"intent": "abstain", "sql": ""}   when these tables cannot answer the question

{feedback}QUESTION
{question}
"""
