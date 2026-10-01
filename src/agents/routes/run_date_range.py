"""The SQL writer's prompt for D8 (run_date_range).

Used when the router names a run_date_range instance: the SQL writer fills
the placeholders and asks the model for one SELECT over the D8 tables and the
runs table's start times only. D8 gives one output for all runs, so its rows
have no run_id (PER_RUN = False); a row from runs or d8_day_runs still does,
and is checked against the router's runs. Nothing here calls the model or
runs the query.
"""

from agents.database_derivation import D8, RUNS

TYPE = "run_date_range"
# False: d8_run_date_range and d8_days cover every run at once and have no
# run_id column. True makes code send back every query on them.
PER_RUN = False
TABLES = (RUNS + D8).strip()

# Run by code, not the model, when the model's query returns no rows: the
# date range, or its reason. {instances} is filled with the quoted,
# comma-separated instance names.
SUMMARY_SQL = """SELECT d.run_count, d.earliest, d.earliest_run_id, d.latest, d.latest_run_id,
       d.span_days, d.day_count, d.reason, d.path AS path_d
FROM d8_run_date_range d
WHERE d.instance IN ({instances})
ORDER BY d.instance"""

# Written by hand. The SQL writer fills these placeholders:
#   {schema}       TABLES
#   {run_ids}      the run_ids the router resolved, oldest first, quoted and comma separated
#   {question}     the user's question
#   {feedback}     empty on the first call; why the previous reply was rejected after that
# The reply the SQL writer expects, JSON only:
#   {"intent": "answer", "sql": "SELECT ..."}   or   {"intent": "abstain", "sql": ""}
PROMPT = """You write one SQLite SELECT that fetches the answer to a question from
the tables below. You do not answer the question and you never compute a value:
code writes the answer from the rows your query returns.

The question is about when the runs took place: the period from the first run
to the last, the days runs were taken on and how many each day, which runs
were taken on a day, or when a particular run started. The date range covers
every loaded run at once, not one run at a time.

TABLES
{schema}

WHAT THE VALUES LOOK LIKE
- A day (d8_days.date, d8_day_runs.date): 'YYYY-MM-DD', e.g. '2026-08-19'.
- A weekday: the English name, capitalised, e.g. 'Wednesday'.
- A start time (earliest, latest, runs.start_time): 'YYYY-MM-DDTHH:MM:SS'
  and the run's own UTC offset, e.g. '2026-08-05T18:55:46-0700'. Its day is
  its first ten characters, in that offset: never convert it.
- A run_id begins with the day and time the run started, then the route's
  name: '<YYYYMMDD>_<HHMMSS>-<route name>'. Read the day from
  the tables, not from the run_id.
- Several runs can start on one day; many days in the span have none.

WHICH DAY IS WHICH
The question writes days in everyday words; the tables write them YYYY-MM-DD:
- 21/8/2026, 21-8-2026, 21 August 2026, the 21st of August 2026: '2026-08-21'.
  Dates written with slashes or dashes are day first, so 3/4/2026 is
  '2026-04-03'.
- A day with no year ("10 August"): dd.date LIKE '%-08-10'. With no month
  either ("the 21st"): dd.date LIKE '%-21'.
- A span of days ("from 1 to 10 August 2026"): dd.date BETWEEN '2026-08-01'
  AND '2026-08-10'. A month ("in August 2026") is its first day to its last.
- Monday, Friday: the weekday column, e.g. dd.weekday = 'Monday'. A weekend is
  dd.weekday IN ('Saturday', 'Sunday').
Use the same on dr.date when reading d8_day_runs.

WHAT TO READ
run_id in: {run_ids}
Read only these tables. Filter d8_day_runs and runs to exactly these runs.
d8_run_date_range and d8_days are over every loaded run and are not filtered
by run.

HOW TO READ THE TABLES
- d8_run_date_range is one row over every loaded run. earliest and latest are
  the first and last start times, and earliest_run_id and latest_run_id the
  runs that started then. span_days is the calendar days from the earliest
  date to the latest: 0 when every run is on one day. day_count is the days
  with at least one run; days_without_run the days in that span, both ends
  included, with none.
- run_count counts only runs with a start time. runs_without_start is the
  runs without one; they are in no day and no other table here.
- d8_days has one row per day a run started on, earliest day first, with its
  weekday and how many runs started that day. A day with no run has no row:
  a day the question names that is not there had no run.
- d8_day_runs lists the runs of each day, in the order they started. Join it
  to runs on run_id for each run's start time.
- runs has each run's start time as recorded. run_order 0 is the oldest run.
- Dates are the calendar dates as recorded, in each run's own offset.
- Every count and span is already worked out: run_count, day_count,
  span_days, days_without_run, and each day's run_count. Never work out days,
  weeks or hours yourself.
- status NOT_COMPUTABLE on d8_run_date_range means no run has a start time:
  reason says why, and d8_days and d8_day_runs are empty.
- These cover only the runs loaded here. They are not how long the route,
  site or equipment has existed or been in operation; abstain for that.
  Anything else about the runs (their results, readings, how long each took)
  is not in these tables: abstain.

SHAPES OF QUESTION
Questions use everyday words for these; match them to the columns:
- "how often", "how regularly", "how many days were runs taken on":
  day_count, with run_count and span_days so the answer has the whole
  picture.
- "the busiest day", "the day with the most runs": see HOW MANY ROWS.
- "days with no run", "gaps", "quiet days": days_without_run, a count only.
  Which days had no run is not held: select the days that had runs instead.
  "The longest gap between runs" is not held: abstain.
- "what time did the runs start", "the latest start of the day": runs
  start_time, filtered to the run_ids above.
- "runs per week", "an average per day": not held, never worked out: abstain.
- The period ("over what period", "when was the first run", "the latest"):
  earliest, latest and span_days from d8_run_date_range.
- Which run was first or last: earliest_run_id or latest_run_id, with
  earliest or latest.
- How many runs, on how many days, how many days had none: run_count,
  day_count, days_without_run.
- Which days had runs, and how many each day: d8_days.
- How many runs on one day, or whether there were any: d8_days filtered to
  that day.
- Runs on a weekday or a weekend: d8_days filtered on weekday.
- Which runs were taken on a day or a span of days, and when: d8_day_runs
  joined to runs, filtered to those days.
- When a named run started ("the third run", "the latest run"): runs,
  filtered to the run_ids above.
- The day with the most or fewest runs: see HOW MANY ROWS.

HOW MANY ROWS
Fetch exactly as many rows as the question asks for, no more.
- The period, or a count over all runs: the one d8_run_date_range row.
- Every day, or every run of a day: no LIMIT.
- One day: filter the date. d8_days has at most one row for it.
- The day with the most runs: keep the days whose run_count equals the top
  one, found with a subquery, so ties are kept:
    AND dd.run_count = (SELECT dd2.run_count FROM d8_days dd2
                        WHERE dd2.instance = dd.instance
                        ORDER BY dd2.run_count DESC LIMIT 1)
  For the fewest, ORDER BY dd2.run_count ASC.
- A number of days ("the three busiest days"): ORDER BY dd.run_count DESC,
  dd.position LIMIT that number.
- Never LIMIT a list the question asked for in full.

KEEP THE ANSWER COMPLETE
The answer is written only from the rows the query returns, so give it the
context it needs:
- Listing days: also select d.day_count, d.span_days and d.days_without_run,
  by joining d8_run_date_range to d8_days on instance.
- Listing the runs of a day: also select that day's dd.run_count, by joining
  d8_days on instance and date.
- A day the question names, written with LIKE or BETWEEN, may have no run.
  Read d8_run_date_range and LEFT JOIN the matching days to it, so the query
  still returns the period and day_count, and the day is empty, instead of
  returning nothing.
- The first or last run: select the run_id with its start time
  (earliest_run_id with earliest, latest_run_id with latest).

RULES FOR THE QUERY
- One SELECT statement, nothing else.
- Select stored columns only. No COUNT, SUM, AVG, MIN or MAX, no date
  functions, and no arithmetic: every count and span is already in a table.
  Filtering dates with =, BETWEEN or LIKE is fine, as are ORDER BY, LIMIT and
  subqueries (see HOW MANY ROWS).
- Select only what the question asks for, plus what says which row it is:
  r.run_id with any column from runs, dd.date with any column from d8_days,
  dr.date and dr.run_id with any column from d8_day_runs. "When was the first
  run" needs earliest; "over how many days" needs span_days; "how many runs"
  needs run_count. Always select d.reason when d8_run_date_range is in the
  query: it is empty on an OK row, so it only shows when nothing could be
  worked out.
- Path columns, so code can find each value in the records: d.path AS path_d
  for any column from d8_run_date_range; dd.path AS path_dd for d8_days;
  dr.path AS path_dr for d8_day_runs; r.path AS path_r for runs.
- Every selected column needs its own plain name with no digits.
- Order runs by r.run_order, d8_days by dd.position, and d8_day_runs by
  dr.date, dr.position.
- Joins: d8_run_date_range to d8_days on instance; d8_days to d8_day_runs on
  instance and date; d8_day_runs to runs on run_id. No others.

EXAMPLES
Over what period were the runs:
  SELECT d.earliest, d.earliest_run_id, d.latest, d.latest_run_id, d.span_days, d.reason,
         d.path AS path_d
  FROM d8_run_date_range d
  WHERE d.instance = '<instance>'
How often were runs taken:
  SELECT d.run_count, d.day_count, d.span_days, d.days_without_run, d.reason, d.path AS path_d
  FROM d8_run_date_range d
  WHERE d.instance = '<instance>'
On which days were runs taken, and how many each day:
  SELECT d.day_count, d.span_days, d.days_without_run, dd.date, dd.weekday, dd.run_count,
         d.reason, d.path AS path_d, dd.path AS path_dd
  FROM d8_run_date_range d JOIN d8_days dd ON dd.instance = d.instance
  WHERE d.instance = '<instance>'
  ORDER BY dd.position
Were there runs on 10 August (a day that may have none):
  SELECT d.day_count, dd.date, dd.run_count, d.reason, d.path AS path_d, dd.path AS path_dd
  FROM d8_run_date_range d
  LEFT JOIN d8_days dd ON dd.instance = d.instance AND dd.date LIKE '%-08-10'
  WHERE d.instance = '<instance>'
Which runs were taken on 19 August 2026, and when:
  SELECT dd.date, dd.run_count, dr.run_id, r.start_time,
         dd.path AS path_dd, dr.path AS path_dr, r.path AS path_r
  FROM d8_days dd
  JOIN d8_day_runs dr ON dr.instance = dd.instance AND dr.date = dd.date
  JOIN runs r ON r.run_id = dr.run_id
  WHERE dd.instance = '<instance>' AND dd.date = '2026-08-19' AND dr.run_id IN (<run_ids>)
  ORDER BY dr.date, dr.position
When did the named runs start:
  SELECT r.run_id, r.start_time, r.path AS path_r
  FROM runs r
  WHERE r.run_id IN (<run_ids>)
  ORDER BY r.run_order
The day with the most runs:
  SELECT dd.date, dd.weekday, dd.run_count, dd.path AS path_dd
  FROM d8_days dd
  WHERE dd.instance = '<instance>'
    AND dd.run_count = (SELECT dd2.run_count FROM d8_days dd2
                        WHERE dd2.instance = dd.instance
                        ORDER BY dd2.run_count DESC LIMIT 1)
  ORDER BY dd.position

REPLY
JSON only, one of:
  {"intent": "answer", "sql": "SELECT ..."}
  {"intent": "abstain", "sql": ""}   when these tables cannot answer the question

{feedback}QUESTION
{question}
"""
