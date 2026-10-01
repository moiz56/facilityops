"""The SQL writer's prompt for D5 (group_max).

Used when the router names one or more group_max instances: the SQL writer
fills the placeholders and asks the model for one SELECT over the runs table
and the routed instances' d5_<instance> tables only. Each instance is the
maximum of a different field, so each has its own table; a question about
several maxima combines their tables with UNION ALL. Nothing here calls the
model or runs the query.
"""

from typing import Sequence

from agents.database_derivation import RUNS, d5_schema
from agents.schema import DerivationConfig

TYPE = "group_max"


def tables(names: Sequence[str], derivation: DerivationConfig) -> str:
    """The runs table and the routed instances' tables, as the database creates them."""
    return (RUNS + d5_schema(derivation, names)).strip()


def summary_sql(names: Sequence[str]) -> str:
    """Run by code, not the model, when the model's query returns no rows:
    every group's maximum and where it came from, or its reason, in each
    routed run, for each routed maximum. {run_ids} is filled with the quoted,
    comma-separated run ids. run_order and position are only there to order
    the rows; code does not show them. The names are instance names
    d5_schema already checked.
    """
    select = (
        "SELECT r.run_id, r.run_order, d.instance, d.position, d.group_key, d.max, d.source_id, d.reason, "
        "d.path AS path_d, r.path AS path_r\n"
        "FROM d5_{name} d JOIN runs r ON r.run_id = d.run_id\n"
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

The question is about maxima: in each run, the highest value of a sensor
reading, worked out for each group (the whole run, or e.g. each zone of the
route), with which reading it was and when it was taken.

TABLES
{schema}

The question may be about one of these maxima or several.

ZONES AND CHECKPOINTS
The route has two levels:
- A zone is an area of the route.
- A checkpoint is one stop inside a zone. A zone holds one checkpoint or
  several. When it holds one, the two often share a name.
<zone> and <checkpoint> below stand for any zone or checkpoint name; the
real ones are listed under WHAT TO READ.
Every reading is tagged with the zone it was taken in. So with "per zone" each
maximum is a zone's highest reading, over all its readings, and source_id says
which reading it was: with checkpoint readings, the checkpoint it came from.
There is no maximum of a checkpoint on its own, except where it is the zone's
source_id.
- A question about a zone ("in <zone>", "per zone"): read d5_<instance> and
  filter d.group_key to the zones listed under WHAT TO READ, or group by it.
- A question about a checkpoint ("at <checkpoint>"): find its zone through
  d5_<instance>_checkpoints, and answer with that zone's maximum and its
  source_id. Select the checkpoint, the zone and source_id, so the answer says
  whose maximum it is and whether the checkpoint was the one that reached it.
- A question about a zone's checkpoints ("which checkpoints are in <zone>")
  is d5_<instance>_checkpoints alone.

WHICH READING IS WHICH
The question uses everyday words; match them to the sensor block:
- vibration, shaking: accelerometer (the ADXL345 sensor), e.g.
  accelerometer.vibration_rms_g
- temperature, heat, how hot: environment (the BME680 sensor),
  environment.temperature_c. Humidity and pressure are environment too.
- particulate, particles, dust, air quality, PM2.5, PM10: particulate (the
  SPS30 sensor), e.g. particulate.pm2_5
Use only the tables whose reading the question asks about.

WHAT TO READ
run_id in: {run_ids}
zone in: {zones}
checkpoint_id in: {checkpoints}
Read only the tables of the maxima listed, filtered to exactly these runs.
When zones are listed, filter d.group_key to them; when checkpoints are
listed, find their zones through d5_<instance>_checkpoints. "(any)" means no
filter of that kind.

HOW TO READ THE TABLES
- d5_<instance> (d): one row per group and run. With "per zone" group_key is
  the zone the readings were taken in. With "per run_id" each run has one
  group, the whole run, and group_key is the run_id.
- d5_<instance>_checkpoints (c): the checkpoints in each zone, by run, with
  checkpoint_id and checkpoint_name (per zone tables only). For a
  checkpoint's maximum, when checkpoints are listed above, join c to d on
  c.run_id = d.run_id AND c.group_key = d.group_key, and filter
  c.checkpoint_id to them. A checkpoint with no row in c is in no zone of
  that run.
- max is the highest of n readings, already found; inputs_excluded readings
  were left out by the exclusion rule, and inputs_stale of the n were stale. Write it as d.max: it is a column,
  not the MAX() function.
- source_id is the reading the maximum came from: a sample by its position
  (sample_0042), or a checkpoint_id. source_timestamp is when it was taken.
  If other readings reached the same maximum, tied_with lists them.
- A group row with status NOT_COMPUTABLE had every reading excluded: max is
  NULL and reason says why. It has no maximum; it is never 0.
- A row with group_key NULL and status NOT_COMPUTABLE means no group in that
  run could be worked out (e.g. the sensor was off at every checkpoint);
  reason says why. When you filter group_key, keep these rows too:
  (d.group_key IN (...) OR d.group_key IS NULL), so such a run says why
  instead of disappearing.
- Only maxima are held. The lowest reading, a mean, or anything but the
  highest value cannot be read from these tables: abstain for that. The
  lowest of the group maxima ("which zone had the lowest peak") is fine.
- runs gives run_order for ordering runs (0 = oldest).

SHAPES OF QUESTION
- One maximum: select from its table.
- Several maxima (any two, or all three): one SELECT per table, each with the
  same columns in the same order, joined with UNION ALL. Select d.instance in
  each, so every row says which maximum it is. Put one ORDER BY at the end,
  using result column names; select r.run_order and d.position so it can use
  them. Code does not show run_order or position.
- Per run: with a per run_id table, one row per run. With a per zone table,
  the run's highest reading is its zone row with the highest max (see HOW
  MANY ROWS); the highest of the zone maxima is the run's maximum.
- Per zone: select group_key, from a per zone table. One zone: filter
  group_key. A checkpoint, or several: join d5_<instance>_checkpoints as above.
- Which zone or checkpoint had the peak: the zone is group_key; the
  checkpoint is source_id.
- Which reading, where or when ("when was the hottest reading", "which sample
  had the peak"): select source_id and source_timestamp.
- Across runs ("which run had the highest vibration"): see HOW MANY ROWS.
  Never compare maxima of different readings with each other.

HOW MANY ROWS
Fetch exactly as many rows as the question asks for, no more.
- All of them ("the maximum per zone", "the peak in each run"): no LIMIT.
- The single highest or lowest maximum. First decide: one per run, or one
  overall?
  - One per run ("the hottest zone", or the run's peak from a per zone table,
    in one run or "in each run"): keep the rows whose max equals that run's
    top max, found with a subquery on the same run and table. It works for one
    run or many, and keeps ties:
      AND d.status = 'OK'
      AND d.max = (SELECT d2.max FROM d5_<instance> d2
                   WHERE d2.run_id = d.run_id AND d2.status = 'OK'
                   ORDER BY d2.max DESC LIMIT 1)
  - One overall ("which run had the highest peak", "the highest reading
    across all runs"): the same subquery with d2.run_id IN (<run_ids>)
    instead of d2.run_id = d.run_id (and d2.group_key = d.group_key for one
    zone across runs).
  - With several maxima in a UNION ALL, each SELECT gets its own subquery on
    its own table.
  - For the lowest, ORDER BY d2.max ASC.
- A number of them ("the three hottest zones"): with one run and one maximum,
  ORDER BY d.max DESC LIMIT that number. Otherwise do not LIMIT, as it cuts
  across runs and maxima: order by run, then max.
- Never LIMIT a list the question asked for in full.

RULES FOR THE QUERY
- One statement: a SELECT, or SELECTs joined with UNION ALL. Nothing else.
- Select stored columns only. No MAX(), MIN(), AVG, SUM or COUNT, and no
  arithmetic on values: every maximum is already in a table. ORDER BY, LIMIT
  and subqueries are fine (see HOW MANY ROWS).
- Select only what the question asks for, plus run_id, group_key for group
  rows (not with a per run_id table, where it repeats run_id), and instance
  when there is more than one maximum. "What was the highest" needs max;
  "which reading" or "where" adds source_id; "when" adds source_timestamp;
  "over how many readings" adds n; inputs_excluded only when the question asks
  about left-out readings. Select tied_with whenever source_id is selected: it
  is empty unless another reading tied. Always select reason: it is empty on
  an OK row, so it only shows when a group or run could not be worked out.
- Path columns, so code can find each value in the records: r.path AS path_r
  for run_id; d.path AS path_d for any column from a d5_<instance> table;
  c.path AS path_c for any column from a d5_<instance>_checkpoints table. In a
  UNION ALL every SELECT has the same ones.
- Every selected column needs its own plain name with no digits.
- Order one table by r.run_order, then d.position. Order a UNION ALL by
  run_order, instance, position.

EXAMPLES
The maximum in one zone, in each run, and which reading it was:
  SELECT r.run_id, d.group_key, d.max, d.source_id, d.tied_with, d.reason,
         d.path AS path_d, r.path AS path_r
  FROM d5_<instance> d JOIN runs r ON r.run_id = d.run_id
  WHERE d.run_id IN (<run_ids>) AND (d.group_key = '<zone>' OR d.group_key IS NULL)
  ORDER BY r.run_order, d.position
The maximum at one checkpoint, in each run (its zone's maximum):
  SELECT r.run_id, c.checkpoint_id, d.group_key, d.max, d.source_id, d.tied_with, d.reason,
         c.path AS path_c, d.path AS path_d, r.path AS path_r
  FROM d5_<instance> d JOIN runs r ON r.run_id = d.run_id
  JOIN d5_<instance>_checkpoints c ON c.run_id = d.run_id AND c.group_key = d.group_key
  WHERE d.run_id IN (<run_ids>) AND c.checkpoint_id IN ('<checkpoint>')
  ORDER BY r.run_order, d.position
Which checkpoints are in <zone>:
  SELECT r.run_id, c.group_key, c.checkpoint_id, c.checkpoint_name, c.path AS path_c, r.path AS path_r
  FROM d5_<instance>_checkpoints c JOIN runs r ON r.run_id = c.run_id
  WHERE c.run_id IN (<run_ids>) AND c.group_key = '<zone>'
  ORDER BY r.run_order, c.position
The peak in each run, from a per run_id table:
  SELECT r.run_id, d.max, d.reason, d.path AS path_d, r.path AS path_r
  FROM d5_<instance> d JOIN runs r ON r.run_id = d.run_id
  WHERE d.run_id IN (<run_ids>)
  ORDER BY r.run_order, d.position
The peak, which reading it was and when:
  SELECT r.run_id, d.max, d.source_id, d.source_timestamp, d.tied_with, d.reason,
         d.path AS path_d, r.path AS path_r
  FROM d5_<instance> d JOIN runs r ON r.run_id = d.run_id
  WHERE d.run_id IN (<run_ids>)
  ORDER BY r.run_order, d.position
Two maxima in each run:
  SELECT r.run_id, r.run_order, d.instance, d.position, d.max, d.reason,
         d.path AS path_d, r.path AS path_r
  FROM d5_<first instance> d JOIN runs r ON r.run_id = d.run_id
  WHERE d.run_id IN (<run_ids>)
  UNION ALL
  SELECT r.run_id, r.run_order, d.instance, d.position, d.max, d.reason,
         d.path AS path_d, r.path AS path_r
  FROM d5_<second instance> d JOIN runs r ON r.run_id = d.run_id
  WHERE d.run_id IN (<run_ids>)
  ORDER BY run_order, instance, position
The run with the highest peak:
  SELECT r.run_id, d.max, d.source_id, d.tied_with, d.path AS path_d, r.path AS path_r
  FROM d5_<instance> d JOIN runs r ON r.run_id = d.run_id
  WHERE d.run_id IN (<run_ids>) AND d.status = 'OK'
    AND d.max = (SELECT d2.max FROM d5_<instance> d2
                 WHERE d2.run_id IN (<run_ids>) AND d2.status = 'OK'
                 ORDER BY d2.max DESC LIMIT 1)
  ORDER BY r.run_order, d.position

REPLY
JSON only, one of:
  {"intent": "answer", "sql": "SELECT ..."}
  {"intent": "abstain", "sql": ""}   when these tables cannot answer the question

{feedback}QUESTION
{question}
"""
