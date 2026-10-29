"""The SQL writer's prompt for D7 (run_set_difference).

Used when the router names a run_set_difference instance: the SQL writer
fills the placeholders and asks the model for one SELECT over the D7 tables
only. D7 gives one output for all runs, comparing the checkpoints, findings
and sensor blocks of the run before the latest with the latest, so its rows
have no run_id (PER_RUN = False). Nothing here calls the model or runs the query.
"""

from agents.database_derivation import D7
from agents.utils import fill_prompt

TYPE = "run_set_difference"
PER_RUN = False
# D7 only: run_a and run_b are on the comparison row, so no query needs runs.
# The prompt shows it table by table, under the ability each table serves.
TABLES = D7.strip()
SCHEMA = {chunk.split("CREATE TABLE ")[1].split()[0]: chunk for chunk in TABLES.split("\n\n")}

# How these columns are named in the answer. run_a and run_b are the
# comparison's own names for the two runs, not words a reader knows.
LABELS = {
    "run_a": "previous run",
    "run_b": "latest run",
    "count_only_in_a": "checkpoints only in the previous run",
    "count_only_in_b": "checkpoints only in the latest run",
    "count_in_both": "checkpoints in both runs",
    "count_findings_only_in_a": "findings only in the previous run",
    "count_findings_only_in_b": "new findings in the latest run",
    "count_findings_in_both": "findings in both runs",
    "block": "sensor block",
    "count_available_previous_only": "checkpoints where it was available in the previous run only",
    "count_available_latest_only": "checkpoints where it was available in the latest run only",
    "count_available_both": "checkpoints where it was available in both runs",
    "unavailable_reason": "why not available",
}

# Run by code, not the model, when the model's query returns no rows: the
# comparison's runs and counts, or its reason. {instances} is filled with the
# quoted, comma-separated instance names.
SUMMARY_SQL = """SELECT d.run_a, d.run_b, d.count_only_in_a, d.count_only_in_b, d.count_in_both,
       d.count_findings_only_in_a, d.count_findings_only_in_b, d.count_findings_in_both, d.reason, d.path AS path_d
FROM d7_run_set_difference d
WHERE d.instance IN ({instances})
ORDER BY d.instance"""

# Written by hand. The tables are put in once, here, from SCHEMA; the SQL
# writer fills the rest:
#   {instances}    one line per routed D7 instance, as b1_analytical_router_derived.describe writes it
#   {checkpoints}  the checkpoint_ids the router resolved, quoted and comma separated, or "(any)" when none
#   {question}     the user's question
#   {feedback}     empty on the first call; why the previous reply was rejected after that
# The reply the SQL writer expects, JSON only:
#   {"intent": "answer", "sql": "SELECT ..."}   or   {"intent": "abstain", "sql": ""}
PROMPT = """You write one SQLite SELECT that fetches the answer to a question from
the tables below. You do not answer the question and you never compute a value:
code writes the answer from the rows your query returns.

WHAT YOU CAN ANSWER
The question compares the last two runs: the run before the latest (the
previous run, run_a) and the latest run (run_b). You can answer it about
three things, and only these:
  1. CHECKPOINTS: which checkpoints were on each run's route
  2. FINDINGS: which findings each run recorded
  3. SENSOR BLOCKS: at which checkpoints each sensor block was available
Every question routed here is about one of the three. Decide which first.
If it is about none of them (coverage, evidence, readings, results, alerts,
anything else), or about more than one, abstain.

For each of the three, the question asks for one of three groups:
  - previous only: in the previous run and not the latest (only_in_a)
  - latest only:   in the latest run and not the previous (only_in_b)
  - both:          in both runs (in_both)

QUESTIONS USE THEIR OWN WORDS
Expect synonyms and do not be thrown by them: match what the question means,
not the exact words. Under each ability below are the words questions often
use for each group; these are examples, not a full list.
- The previous run: "last run", "last time", "the run before", "the earlier
  run", "previously", "before".
- The latest run: "this run", "this time", "now", "the current run", "the
  most recent run", "today's run".
- Both: "each time", "both times", "still", "again", "every run" (of these
  two).

THE COMPARISON THE QUESTION USES (the instance column)
{instances}

WHAT TO READ
checkpoint_id in: {checkpoints}
Filter d.instance to the instance above. When checkpoints are listed, filter
the checkpoint_id of the table you read to them. "(any)" means no filter.

WHAT IT DOES NOT HOLD
- Any other pair of runs ("the first and the third", "last week and this
  week"): abstain.
- Whether a difference is better or worse, or why it happened: abstain.
- status NOT_COMPUTABLE on the comparison row means there are fewer than two
  runs: select d.reason.

THE COMPARISON ROW, READ BY EVERY QUERY
{table_row}
- Always select d.run_a, d.run_b and d.reason (empty when OK), so the answer
  says which two runs it compared.
- An empty group is an answer: "no checkpoint was added" or "no finding is
  new" is the count 0, not a missing row. So read this row and LEFT JOIN the
  group's list to it; when the list is empty, the row still comes back.

1. CHECKPOINTS
{table_checkpoints}
It compares the checkpoints each run's record lists on its route. A
checkpoint the robot missed is still on that run's route, so this is not
whether a checkpoint was visited, completed or skipped: abstain for "which
checkpoints did the latest run skip" or "which were missed".
The groups, and words questions use for them:
- previous only (only_in_a): "dropped", "removed", "taken off the route",
  "no longer on the route", "missing from the latest run".
- latest only (only_in_b): "new", "added", "only in the latest run".
- both (in_both): "kept", "unchanged", "the same", "common to both".
What to select:
- c.checkpoint_id, with the group's count from the comparison row:
  d.count_only_in_a, d.count_only_in_b or d.count_in_both.
- For one named checkpoint, c.membership: no row means neither run had it on
  its route.
- "Did the route change", "compare the last two routes": all three counts,
  and the previous only and latest only checkpoints (not both, which is
  usually the whole route; its count says how many were kept). Never decide
  whether the route changed yourself.
Examples:
  Which checkpoints are new in the latest run:
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
  Was one checkpoint on both routes:
    SELECT d.run_a, d.run_b, c.checkpoint_id, c.membership, d.reason, c.path AS path_c, d.path AS path_d
    FROM d7_run_set_difference d
    LEFT JOIN d7_checkpoints c ON c.instance = d.instance AND c.checkpoint_id = '<checkpoint>'
    WHERE d.instance = '<instance>'

2. FINDINGS
{table_findings}
A finding is a problem the robot found at a checkpoint: something that
should not be there, such as a blocked airflow path or a sensor over its
threshold. Questions call findings "problems", "issues", "faults",
"defects", "concerns", "things flagged" or "what was found"; all of these
mean findings.
A finding is in both runs when it is at the same checkpoint with the same
feature. A question about findings reads d7_findings, never d7_checkpoints:
a checkpoint on both routes can still have a new finding.
The groups, and words questions use for them:
- latest only (only_in_b): "new findings", "new issues", "new problems",
  "since the last run", "not there before", "newly flagged".
- both (in_both): "recurring", "still there", "persistent", "keeps showing
  up", "same as last run".
- previous only (only_in_a): "no longer reported", "gone", "went away". The
  latest run does not have them; that is all. A question may call them
  "resolved" or "fixed": select previous only all the same, as the answer
  only says they are not in the latest run.
What to select:
- f.finding_id, f.checkpoint_id and f.feature, with the group's count from
  the comparison row: d.count_findings_only_in_a, d.count_findings_only_in_b
  or d.count_findings_in_both.
- Filter f.membership in the JOIN; never select it.
Example:
  Which findings are new since the previous run:
    SELECT d.run_a, d.run_b, d.count_findings_only_in_b, f.finding_id, f.checkpoint_id, f.feature,
           d.reason, f.path AS path_f, f.path_record AS path_record, d.path AS path_d
    FROM d7_run_set_difference d
    LEFT JOIN d7_findings f ON f.instance = d.instance AND f.membership = 'only_in_b'
    WHERE d.instance = '<instance>'
    ORDER BY f.position

3. SENSOR BLOCKS
{table_blocks}
A block is available at a checkpoint when its reading passes the exclusion
rule there. Only checkpoints on both routes are compared. A question about
sensor blocks reads these two tables, never d7_checkpoints or d7_findings.
Questions name blocks in everyday words: vibration or shaking is the
accelerometer; temperature, humidity or pressure is environment; dust,
particles, air quality, PM2.5 or PM10 is particulate. "Sensors" means the
blocks.
The groups, and words questions use for them:
- previous only (only_in_a): available last run and not this one: "dropped
  out", "stopped reporting", "stopped working", "went offline", "new sensor
  failures", "working last time but not now".
- latest only (only_in_b): available this run and not last: "came back",
  "working again", "back online".
- both (in_both): available in both runs: "still working", "available both
  times", "working each time".
A block that was available in neither run is in no group: abstain for
"which sensors were down both times". A question about one run only ("which
sensors were down this run") is not a comparison: abstain.
What to select:
- One group per query. Filter k.membership in the JOIN; never select
  k.membership or b.membership.
- k.block, and k.count under the name for its group:
  count_available_previous_only (previous only), count_available_latest_only
  (latest only) or count_available_both (both).
- LEFT JOIN d7_blocks on instance, membership and block, and select
  b.checkpoint_id and b.reason AS unavailable_reason.
- Filter k.block when the question names a block.
Example:
  Which sensor blocks were available last run but not this one (every block, even with none):
    SELECT d.run_a, d.run_b, k.block, k.count AS count_available_previous_only, b.checkpoint_id,
           b.reason AS unavailable_reason, d.reason, k.path AS path_k, b.path AS path_b,
           b.path_excluded AS path_excluded, d.path AS path_d
    FROM d7_run_set_difference d
    JOIN d7_block_counts k ON k.instance = d.instance AND k.membership = 'only_in_a'
    LEFT JOIN d7_blocks b ON b.instance = k.instance AND b.membership = k.membership AND b.block = k.block
    WHERE d.instance = '<instance>'
    ORDER BY k.block, b.position
In the examples, <instance> is the instance above.

HOW MANY ROWS
Fetch exactly as many rows as the question asks for, no more.
- Which checkpoints, findings or blocks of a group: every row of it; no LIMIT.
- How many: the counts only, with no list.
- The first or last of a group in the route: ORDER BY position, LIMIT 1.

RULES FOR THE QUERY
- One SELECT statement, nothing else.
- Select stored columns only. No COUNT, SUM, AVG, MIN or MAX, and no
  arithmetic: every count is already in a table. JOIN, LEFT JOIN, ORDER BY
  and LIMIT are fine.
- Path columns, so code can find each value in the records. For each table
  you select a column from:
    d7_run_set_difference  d.path AS path_d
    d7_checkpoints         c.path AS path_c
    d7_findings            f.path AS path_f, f.path_record AS path_record
    d7_block_counts        k.path AS path_k
    d7_blocks              b.path AS path_b, b.path_excluded AS path_excluded
- Every selected column needs its own plain name with no digits.
- Order checkpoint rows by c.membership, then c.position; finding rows by
  f.membership, then f.position; block rows by k.block, then b.position.

REPLY
JSON only, one of:
  {"intent": "answer", "sql": "SELECT ..."}
  {"intent": "abstain", "sql": ""}   when these tables cannot answer the question

{feedback}QUESTION
{question}
"""
PROMPT = fill_prompt(PROMPT, {
    "table_row": SCHEMA["d7_run_set_difference"],
    "table_checkpoints": SCHEMA["d7_checkpoints"],
    "table_findings": SCHEMA["d7_findings"],
    "table_blocks": SCHEMA["d7_block_counts"] + "\n\n" + SCHEMA["d7_blocks"],
})
