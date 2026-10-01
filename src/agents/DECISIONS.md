# Decisions

## eligible_values (derivation.py)

What it does
eligible_values(records, field_path, config) returns two lists: the values a
derivation may compute over, and the values it left out with the reason. Every
derivation uses it. None filters on its own.

Where values come from
Both checkpoints and telemetry samples. Each value is tagged "checkpoint" or
"sample" so a derivation takes the kind it needs. The grouped derivations (D4,
D5, D6) use samples: the brief's figures for group_mean (home_docking_station
1.251 n=8, checkpoint_1 0.719 n=45, checkpoint_2 0.623 n=51) and the reading
counts in section 3.1 (1,630 / 1,304 / 978) only come out of the samples.

How a sample is judged
A sample has no device flags, no ok and no hub status. It is judged by its
nearest_checkpoint_id: if that checkpoint's reading of the block cannot be
used, neither can the sample's. The sample's own status must also be
"connected". A sample with no nearest checkpoint, or one naming a checkpoint
not in the run, is excluded with that reason.

The checks, in order (section 3.2)
1. Checkpoint status is COMPLETED.
2. Sensor block present, ok is true, status is "connected", hub reachable.
3. The block's device flag is true (adxl345_ok, bme680_ok, sps30_ok). The
   flag for a block comes from report.yaml sensor.subsystem_flags, not a second
   mapping.
4. The field is present and is a number (a bool is not a number).
The first failing check is the reason recorded. Each checkpoint is judged once
per run; samples look the result up.

Stale
A checkpoint reading older than max_age_seconds (report.yaml, 120) is still
used and marked stale. Exactly at the limit is not stale. Samples carry no
age_seconds and are never marked stale.

Exclusions
Grouped by run, kind, checkpoint, zone and reason, with a count, so 1,304
excluded particulate samples are a handful of entries rather than 1,304. The
single "__run__" entry the extended record shows (section 3.4) is built from
these by extend_record, not here, so the per-zone counts the grouped
derivations need are not lost.

Duplicate checkpoint ids
If two checkpoints share an id and disagree about the sensor, samples naming
that id are excluded with a reason saying so, rather than picking one.

Sample ids
Samples have no id field. They are named by position: sample_0000, sample_0001.

Config
DerivationConfig carries subsystem_flags and max_age_seconds, built at the
entry point and passed in. Nothing in derivation.py reads a config file.


For D1

In the derivations the scope if its checkpoints its for all of most recent run and if it's 'checkpoint_3' then that specific checkpoint MOST RECENT

For D2 completed as well , though we can have for all runs as well to be discussed MOSTRECENT CAN BE FOR ALL RUNS

## Which runs and inputs each derivation uses (derivation.py)

Records reach the derivations sorted oldest first, so records[-1] is the most
recent run and records[-2] the one before it.

D1-D6 run once for every run, not just the latest. Each instance's value in
the extended record is a list, one output per run, oldest first, and each
output carries the run_id it was computed from, straight after "derivation".
A run with no eligible inputs gets its own NOT_COMPUTABLE entry with its
run_id; the other runs are unaffected.

The extended record's excluded and stale lists cover every run too: one flat
list each, oldest run first, every entry starting with its run_id. A __run__
scope means every checkpoint of that entry's run.

D1 threshold_compare: every run, one output each
Checkpoint readings. scope is one checkpoint id, or "checkpoints" for every
checkpoint in the run. Each checkpoint's result carries its checkpoint_name,
OK or NOT_COMPUTABLE, taken from its first entry in the run (the user asked
for it; it is not in the brief's D1 keys). It is stored in d1_checkpoints and,
for a single-checkpoint scope, on the d1_threshold_compare row.

D2 condition_count: every run, one output each
Items of scope (checkpoints, findings or sensor_alerts), from the eligibility
for "<scope>.<field>".
Beyond the brief (user asked, 2026-09-29): each output also carries
excluded_items, one {item_id, reason, count} per item and reason eligibility
left out, straight from its Exclusion entries: no new reasons. item_id is the
checkpoint_id, None for findings and sensor alerts (eligibility does not name
them). The counts sum to inputs_excluded. In the database: d2_excluded_items.

D3 proportion: every run, one output each
Same inputs as D2. Denominator is every eligible item, numerator the matching
ones.

D4 group_mean: every run, one output each
Telemetry samples, grouped by zone.
Beyond the brief (user asked, 2026-09-29): grouped per zone, each group also
carries checkpoints: [{checkpoint_id, checkpoint_name}], the run's checkpoints
whose own zone is that group's key (first entry per id, route order; [] when
none), so a question about a checkpoint finds its zone's mean. The mean is
still the zone's, over every reading in it. In the database:
d4_<instance>_checkpoints.

D5 group_max: every run, one output each
Telemetry samples, grouped by zone.
Beyond the brief (user asked, 2026-09-29): as for D4, grouped per zone, each
group carries checkpoints: [{checkpoint_id, checkpoint_name}] (utils.
add_zone_checkpoints, shared with D4). In the database:
d5_<instance>_checkpoints. derivations.yaml groups D5 per run_id for now, so
it stays empty until an instance is grouped per zone.
D5 groups also carry inputs_stale, as D4's do, so the two have the same keys
apart from the figure itself (user asked, 2026-09-29).

D6 rank_top_n: every run, one output each
Checkpoint readings.

D7 run_set_difference: last two runs (records[-2] as run_a, records[-1] as run_b)
Checkpoint ids read straight from the two records, not from eligibility. A
MISSED checkpoint is still on the route, so it counts as present; otherwise a
miss would look like a change of route.

D8 run_date_range: all runs
Run start_time. Runs with no start_time are left out of run_count.
span_days is the difference between the two calendar dates as recorded, each
in its own offset, with no conversion to UTC.

Beyond the brief (user asked, 2026-09-29): D8 also carries earliest_run_id,
latest_run_id, runs_without_start, day_count, days_without_run (days in the
span, both ends included, with no run) and days: one entry per day a run
started on, oldest first, with date (YYYY-MM-DD), date_formatted, weekday,
run_count and run_ids. Days use the same recorded-offset dates as span_days.
In the database: d8_run_date_range's new columns, d8_days and d8_day_runs.

## verify_numeric takes config as a third argument (verification.py)

Section 10.2 gives verify_numeric(text, extended). The verification settings
(numeric_tolerance, reject_unclassifiable, record_tolerance_anomalies,
word_numbers) live in agents.yaml, and section 10.3 bans reading config
anywhere but the entry point. So the signature is

    verify_numeric(text, extended, config: VerificationConfig)

VerificationConfig is built once at the entry point (utils.verification_config)
and passed in, the same way DerivationConfig is. It also carries:
- the timestamp and date formats from derivations.yaml, so a timestamp in the
  text is compared with the record's timestamps formatted the same way
- decimals from report.yaml, so a record value is compared in its printed form
- engine_version and template_version from report.yaml provenance, the
  versions a version token can match (0.1.0, full_report_v1)
The verifier still takes nothing about its caller: text, record, settings.

Tolerance
numeric_tolerance is 0.05, absolute (section 6.3, TUNABLE). A token that
matches a value only within the tolerance, and not exactly, passes and is
recorded in anomalies.
Differences up to the tolerance plus 1e-9 pass, so float error cannot fail a
difference of exactly 0.05. The closest value within the tolerance is the one
recorded, as source_field and source_value.

What verify_numeric returns
- position is the token's character offset in the text.
- method is template_slot_fill and regeneration_attempts is 0. The verifier
  does not know how the text was produced or how many tries it took; the
  caller sets both.
- passed is true only with no failures and emitted == verified. No tokens is
  a pass.
- An unclassifiable token fails. If reject_unclassifiable is off it is let
  through and listed in anomalies instead, never silently.
- Telemetry samples are not matched directly: text reaches them only through
  a derivation, which carries the value it used.

Identifiers
Only identifiers have an underscore in their name, so any word with one is a
token and is classed as an identifier: home_docking_station, checkpoint_1,
group_mean_temperature_c. Letters mixed with digits (cap0142) are identifiers
too. A word with neither is not a token. The referent must exist: a string
in the records or extended record, a field name, a key in a derived value,
or a part of a dotted field path (environment.temperature_c gives
environment and temperature_c). A made-up zone or checkpoint name fails.
These count in tokens_emitted and by_class.identifier like any other token.

## verify_numeric can be given the filled slots (verification.py)

Beyond section 6, which checks each token on its own against the records.
verify_numeric(text, extended, config, slots=()) takes, optionally, the slots
fill_slots placed in the text. A token inside a slot whose source field holds
text, where the slot's span is exactly that text, is confirmed by that field:
"A5" in "Rack A5 (back)" at records[1].checkpoints[3].checkpoint_name. Every
other token is checked as before.

Why: a recorded name holds letters mixed with digits that are not ids on
their own. Checked alone, "A5" matches nothing and the answer is refused
though every value came from the records; "1" in "Checkpoint 1" passed only
because some count happened to be 1. The check is still against a source
field, the one the citation names, not a skip. Without slots (the brief's
call) the behaviour is unchanged.

## fill_slots (slots.py)

Signature
fill_slots(template, extended, config: DerivationConfig). Section 7.1 gives two
arguments, but record floats and timestamps need decimals and the timestamp
format, and section 10.3 bans reading config in the module. The same reason
verify_numeric takes a third argument. Passing DerivationConfig lets slots
reuse utils.format_value and format_timestamp, so formatting lives in one
place (section 7.3).

Template
The brief does not define Template or FilledTemplate.
- Template: name, text with {{ slot_id }} placeholders, and slots, a map of
  slot_id -> source path. The agent supplies the paths, because they depend on
  the record: which zones exist, which checkpoint is meant.
- FilledTemplate: name, filled text, and the FilledSlot manifest (section 7.2).
The .j2 files are filled by our own substitution, not rendered by Jinja: Jinja
renders the whole text at once and cannot say where each value landed. Spans
are recorded as each value is placed, never found by searching afterwards.

Paths
Dotted, with [i] for list positions, starting at the extended record:
derived.values.group_max[2].groups.checkpoint_1.max, or
records[0].checkpoints[2].sensor.environment.temperature_c. The same form the
verifier reports as source_field.

Formatting, in order
1. A value with a *_formatted sibling uses it (every derived number has one).
2. Strings as they are; whole numbers as they are (counts).
3. Record floats: report.yaml decimals, through utils.format_value. A field
   with no decimals entry is an error, not a guess.
4. Record timestamps: utils.format_timestamp.
5. A list of ids: joined with ", ".
A true/false value is refused: the template says it in words.
A path ending at a group (a dict under a name) prints the name, its last key:
...groups.home_docking_station -> "home_docking_station". A zone name exists
only as a key, so this is how a template cites it.

MissingSlotError
Raised for a placeholder with no path, a path that finds nothing, a None, or
an empty string or list. A NOT_COMPUTABLE value has no mean/max key, so a slot
pointing at one raises rather than printing a blank.

## build_database (database_derivation.py)

The extended record as an in-memory SQLite database (standard library, no
file), for B-1 to query. Built after extend_record from the extended record
and the eligibility it used, rebuilt whenever the extended record is. Nothing
is computed here; every value is copied.

Signature
build_database(extended, eligibility, config: DerivationConfig). eligibility
fills readings, so the exclusion rule is not applied a second time. config
gives the timestamp format and which flag governs which block.

Only eligible values
readings holds checkpoint sensor values that passed the exclusion rule. An
excluded value has no row, so no query can select it; exclusions says why.
Telemetry samples are not tables: they reach an answer through a derivation.

Keys
Rows from a record list are keyed by position, never by id: checkpoint and
finding ids can repeat. checkpoints has one integer key, checkpoint_row, that
evidence, sensor_flags and readings reference. A reference the record only
claims (a finding's checkpoint_id, an alert's nearest checkpoint, ids inside
derived values) is a plain column with no foreign key, so odd data is kept,
not refused. Foreign keys are enforced: a violation is a builder bug.

path
Every row carries its place in the extended record, the form fill_slots
takes. For a row that is one object, a column's source is path + "." + column;
for a row that is one value (a reading, an image, a listed id) path points at
the value. A group whose key is None (samples with no zone) has no path: the
path form cannot name it.

Derived values
derivations holds one row per output (D1-D6 per run, D7 and D8 once), with
each scalar key as a column of the same name. NOT_COMPUTABLE outputs are rows
with status and reason. Per-checkpoint and per-group results go to
derivation_items, rank_top_n's ranking to derivation_ranking, id lists to
derivation_ids. *_formatted strings are left out: fill_slots finds them from
the number's path. An output key with no column fails the build.

Left out
event_log, live_detections, checkpoint detections (the same shape as
findings), coordinates, raw sensor duplicates, sensor warnings, stale (the
readings table has a stale column).

## run_query (database_derivation.py)

run_query(conn, sql, max_rows, timeout_seconds) -> (columns, rows). The limits
are enforced by SQLite, not asked of the model:
- The authorizer allows reading tables and calling ALLOWED_FUNCTIONS (count,
  min, max, lower, upper, like, coalesce). Everything else is denied: writes,
  schema changes, PRAGMA, ATTACH, transactions, recursive queries, and every
  other function (avg, sum, round, date functions, load_extension, ...).
- PRAGMA query_only is set once the database is built, a second lock on writes.
- One statement only; sqlite3 refuses a second.
- A query past timeout_seconds is interrupted; one returning more than
  max_rows fails. Neither comes back cut short.
Every failure is a QueryError with a readable reason. max_rows and
timeout_seconds are agents.yaml database settings, passed in.

What the guard does not stop: arithmetic operators (value - 30) and a count
in the SELECT list. Those make numbers that are not in the extended record.
The guard is for safety; number integrity stays with the verifier and with
citations: a result column with no path cannot fill a slot.

## B-1 analytical (b1_analytical.py, provider.py)

Three model calls: the router names the runs, checkpoints and derivations a
question is about (b1_analytical_router_derived); the SQL writer for the routed
derivation's type writes one SELECT over its tables (routes/); the prose
writer writes a lead-in (b1_analytical_prose). Code does everything else.

Safeguards, in the order they run
- The question is length-capped. The router's runs and checkpoints must be
  ones the records hold; a query returning any other run or checkpoint is sent
  back. Runs are named by position (1 oldest, -1 latest) or by the day they
  started, "YYYY-MM-DD" or a span "first/last", matched on start_time's date
  in the run's own offset. A position or day no run has abstains.
- The SELECT runs through run_query: read-only, allowed functions only, row
  limit and timeout.
- Every shown cell is traced to the extended-record path holding exactly that
  value, through the row's path columns. A cell that does not trace (a
  computed or renamed value) sends the query back.
- Code writes the answer's facts as a template: grouped by run, the run's id
  as a heading line once, then one "- column {{ slot }}, ..." line per row.
  fill_slots fills it, so every value has a span and a source_field; citations
  are those, never searched for afterwards.
- The prose writer sees the question and the filled facts, fenced and declared
  data (TB-10), and writes only a lead-in of a sentence or two that goes above
  them. Its reply may hold no value at all: no digit, number or ordinal word,
  id, or braces, and at most 300 characters. The facts follow it unchanged.
- verify_numeric checks the whole answer, lead-in and facts, given the slots.
A bad reply goes back with the reason, up to max_attempts. A router or SQL
writer that never passes: REFUSED_UNVERIFIABLE with nothing shown. A prose
writer that never passes, or a provider failure there: the facts alone,
DEGRADED_TEMPLATE_ONLY. A provider failure before that: PROVIDER_UNAVAILABLE.

Beyond the brief: the model does not write around the values
Section 8's order is fill_slots -> generate_prose, the model returning prose
around the filled values. Earlier the prose writer wrote the whole answer as a
template of {{ slot }} placeholders instead. With many rows it cited other
rows' slots for the wrong run, folded rows into "multiple checkpoints
including", and kept tripping the no-value rules. Now code writes and fills
every fact, in order, and the model writes only the lead-in, which holds no
value, so it cannot misstate, misplace or drop one. The user asked for the
brief to be exceeded where it improves results.

Output
answer, citations (claim_span, source_field), records_consulted: the runs the
cited values came from. derived_values_used comes from the slot paths, not
from the verifier's first match. An empty cell reads "not recorded".

Provider
Gemini or Claude, chosen by agents.yaml provider.name, both over REST with
urllib, no SDK. Key from GEMINI_API_KEY / ANTHROPIC_API_KEY only, never
agents.yaml; main loads them from .env (gitignored) when the shell has not. Claude is sent the configured temperature,
so its model must be one that accepts it (Opus 5, Sonnet 5 and Opus 4.7+ reject
temperature with a 400). Kept out of ProviderConfig's repr. Retries
timeouts, 429 and 5xx with backoff; other errors fail at once. A blocked or
cut-off reply is an error, not an answer.

Beyond the brief: JSON replies held to a schema
complete() takes an optional JSON schema (the brief's complete(prompt) still
works). Given one, the provider's structured output mode holds the reply to
it: Claude's output_config.format, Gemini's responseJsonSchema. The router,
and the SQL writer pass one; prose replies do not. Replies often
broke the JSON shape the prompt asked for, and each break cost a retry. Every
key is required, so an abstain fills the others with empty values, which are
ignored. The parsers still check every reply.

## Envelope (envelope.py)

One function wraps every agent's output in section 9's shape. It refuses a
status outside the closed set, and an OK whose verification did not pass, so
no caller can mislabel a result. agent_version is a constant in the module,
not config: it versions the code.

## Lookup exclusion reasons are shown uncited (b1_analytical, verification.py)

Every value in a B-1 answer is a slot traced to a path in the extended record.
A lookup's exclusion reason cannot be: "sps30_ok=false" or "sensor status is
offline" is eligibility's verdict, written by code from the record's own flags,
and no field holds it. Without it, "why is there no PM2.5 reading" could not be
answered at all.

So a lookup template may name TEXT_COLUMNS (only checkpoint_readings does:
reason). build_lines writes those cells as they are, with no slot and no
citation, and finish passes their spans in the facts to verify_numeric as
exempt: tokens inside them are not checked or counted. Only those spans: the
lead-in, every value, id, field name and timestamp are still traced and
verified. Derivation routes name no TEXT_COLUMNS, so their reason columns stay
traced as before.

An excluded reading's row cites where its field sits
(records[i].checkpoints[j].sensor.<block>.<field>), so its field and block
names trace as keys; its value there is never shown.
