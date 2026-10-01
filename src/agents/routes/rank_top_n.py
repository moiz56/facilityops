"""The SQL writer's prompt for D6 (rank_top_n).

Used when the router names one or more rank_top_n instances: the SQL writer
fills the placeholders and asks the model for one SELECT over the runs table
and the routed instances' d6_<instance> and d6_<instance>_ranking tables only.
Each instance ranks a different field, so each has its own tables; a question
about several rankings combines them with UNION ALL. Nothing here calls the
model or runs the query.
"""

from typing import Sequence

from agents.database_derivation import RUNS, d6_schema
from agents.schema import DerivationConfig

TYPE = "rank_top_n"


def tables(names: Sequence[str], derivation: DerivationConfig) -> str:
    """The runs table and the routed instances' tables, as the database creates them."""
    return (RUNS + d6_schema(derivation, names)).strip()


def summary_sql(names: Sequence[str]) -> str:
    """Run by code, not the model, when the model's query returns no rows:
    each routed ranking's run row in each routed run (how many readings it
    holds, out of how many, or its reason). {run_ids} is filled with the
    quoted, comma-separated run ids. run_order is only there to order the
    rows; code does not show it. The names are instance names d6_schema
    already checked.
    """
    select = (
        "SELECT r.run_id, r.run_order, t.instance, t.n_returned, t.population, t.reason, "
        "t.path AS path_t, r.path AS path_r\n"
        "FROM d6_{name} t JOIN runs r ON r.run_id = t.run_id\n"
        "WHERE t.run_id IN ({{run_ids}})"
    )
    return "\nUNION ALL\n".join(select.format(name=name) for name in names) + "\nORDER BY run_order, instance"


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

The question is about a ranking: in each run, the highest readings of one
sensor field, highest first, and which checkpoint each came from.

TABLES
{schema}

The question may be about one of these rankings or several.

CHECKPOINTS AND ZONES
The route has two levels: zones, the areas of the route, and checkpoints,
the stops inside them. <zone> and <checkpoint> below stand for any zone or
checkpoint name.
- A ranking is over the checkpoint readings of the whole run: one reading
  per checkpoint stop. item_id is the checkpoint_id the reading came from. A
  checkpoint visited twice can be ranked twice.
- A ranking holds no zones. It is never worked out per zone, and the tables
  do not say which zone a ranked checkpoint is in. A question about the top
  readings in a zone ("the five hottest checkpoints in <zone>"), or which
  zone the top readings were in, cannot be answered: abstain.
- A question about a checkpoint ("did <checkpoint> make the top five",
  "where did <checkpoint> rank"): filter k.item_id to it. A row gives its rank and reading;
  no row means it was not among the highest, or its reading was excluded.

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
Read only the tables of the rankings listed, filtered to exactly these runs.
When checkpoints are listed, filter k.item_id to them. When zones are listed,
abstain: a ranking holds no zones. "(any)" means no filter of that kind.

HOW TO READ THE TABLES
- The ranking table (k) holds the readings; the run table (t) holds how many
  there are and out of how many. Join them on run_id when a question needs
  both.
- rank 1 is the highest reading. Readings that are equal take the next ranks,
  earliest first, so several ranks can share one value.
- Only the highest n_requested readings are held (more on a tie). Anything
  below them, the lowest reading, or a mean cannot be read from these tables:
  abstain for that.
- A run with status NOT_COMPUTABLE had no eligible reading: it has no ranking
  rows, and its run row's reason says why. Whenever you select from the
  ranking table, also make such runs say why: select from the run table and
  LEFT JOIN the ranking table, so a run with no ranking keeps one row with its
  reason.
- A checkpoint missing from the ranking was not among the highest, or its
  reading was excluded; the tables do not say which.
- runs gives run_order for ordering runs (0 = oldest).

SHAPES OF QUESTION
- The top readings ("the five highest vibration readings"): the ranking rows,
  ordered by run, then rank.
- Fewer than held ("the top three"): see HOW MANY ROWS.
- More than held ("the top ten" when the ranking keeps five): select every
  ranked row and t.n_requested, so the answer says only that many are held.
- The single highest ("which checkpoint vibrated most"): rank 1, plus any
  reading tied with it (see HOW MANY ROWS).
- Whether a checkpoint is among them ("is <checkpoint> in the top five"), or
  where it ranked: filter k.item_id; no row means it is not.
- Out of how many ("out of how many readings"): t.population.
- Several rankings (any two, or all three): one SELECT per ranking, each with
  the same columns in the same order, joined with UNION ALL. Select the
  instance in each, so every row says which ranking it is. Put one ORDER BY at
  the end using result column names; select r.run_order so it can use it.
  Code does not show run_order. Never compare readings of different fields.

HOW MANY ROWS
Fetch exactly as many rows as the question asks for, no more.
- All that are held ("the top readings"): no LIMIT.
- The top m, fewer than held ("the top three"): keep rank m and above, and any
  reading tied with rank m, in each run:
    AND (k.rank <= 3 OR k.value = (SELECT k2.value FROM d6_<instance>_ranking k2
                                    WHERE k2.run_id = k.run_id AND k2.rank = 3))
- The single highest: the same with 1 in place of 3.
- The run with the highest top reading ("which run had the highest
  vibration"): one row overall, keeping ties:
    AND k.rank = 1
    AND k.value = (SELECT k2.value FROM d6_<instance>_ranking k2
                   WHERE k2.run_id IN (<run_ids>) AND k2.rank = 1
                   ORDER BY k2.value DESC LIMIT 1)
- Never LIMIT: ranks already say how far down to go, and LIMIT cuts across
  runs and ties.

RULES FOR THE QUERY
- One statement: a SELECT, or SELECTs joined with UNION ALL. Nothing else.
- Select stored columns only. No MAX(), MIN(), AVG, SUM or COUNT, and no
  arithmetic on values: every rank and count is already in a table. Filters,
  ORDER BY and subqueries are fine (see HOW MANY ROWS).
- Write the rank column as k.rank: it is a column, not the RANK() function.
- Select only what the question asks for, plus run_id, and the instance when
  there is more than one ranking. "Which checkpoints" needs item_id; "what
  were the readings" adds value; "in what order" adds rank; "out of how many"
  needs t.population; "how many are ranked" needs t.n_returned. Always select
  t.reason when the run table is in the query: it is empty on an OK run, so it
  only shows when a run could not be ranked.
- Path columns, so code can find each value in the records: r.path AS path_r
  for run_id; k.path AS path_k for any column from a ranking table; t.path AS
  path_t for any column from a run table. In a UNION ALL every SELECT has the
  same ones.
- Every selected column needs its own plain name with no digits.
- Order by r.run_order, then k.rank. Order a UNION ALL by run_order, instance,
  rank.

EXAMPLES
The top readings in each run, with runs that could not be ranked:
  SELECT r.run_id, k.rank, k.item_id, k.value, t.reason,
         k.path AS path_k, t.path AS path_t, r.path AS path_r
  FROM d6_<instance> t JOIN runs r ON r.run_id = t.run_id
  LEFT JOIN d6_<instance>_ranking k ON k.run_id = t.run_id
  WHERE t.run_id IN (<run_ids>)
  ORDER BY r.run_order, k.rank
The top three, keeping ties:
  SELECT r.run_id, k.rank, k.item_id, k.value, k.path AS path_k, r.path AS path_r
  FROM d6_<instance>_ranking k JOIN runs r ON r.run_id = k.run_id
  WHERE k.run_id IN (<run_ids>)
    AND (k.rank <= 3 OR k.value = (SELECT k2.value FROM d6_<instance>_ranking k2
                                    WHERE k2.run_id = k.run_id AND k2.rank = 3))
  ORDER BY r.run_order, k.rank
Did two named checkpoints make the top readings, and at what rank:
  SELECT r.run_id, k.item_id, k.rank, k.value, k.path AS path_k, r.path AS path_r
  FROM d6_<instance>_ranking k JOIN runs r ON r.run_id = k.run_id
  WHERE k.run_id IN (<run_ids>) AND k.item_id IN ('<checkpoint>', '<checkpoint>')
  ORDER BY r.run_order, k.rank
How many readings the ranking was taken from:
  SELECT r.run_id, t.population, t.reason, t.path AS path_t, r.path AS path_r
  FROM d6_<instance> t JOIN runs r ON r.run_id = t.run_id
  WHERE t.run_id IN (<run_ids>)
  ORDER BY r.run_order

REPLY
JSON only, one of:
  {"intent": "answer", "sql": "SELECT ..."}
  {"intent": "abstain", "sql": ""}   when these tables cannot answer the question

{feedback}QUESTION
{question}
"""
