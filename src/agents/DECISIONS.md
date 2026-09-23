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

D1 threshold_compare: most recent run (records[-1])
Checkpoint readings. scope is one checkpoint id, or "checkpoints" for every
checkpoint in the run.

D2 condition_count: most recent run (records[-1])
Items of scope (checkpoints, findings or sensor_alerts), from the eligibility
for "<scope>.<field>".

D3 proportion: most recent run (records[-1])
Same inputs as D2. Denominator is every eligible item, numerator the matching
ones.

D4 group_mean: most recent run (records[-1])
Telemetry samples, grouped by zone.

D5 group_max: most recent run (records[-1])
Telemetry samples, grouped by zone.

D6 rank_top_n: most recent run (records[-1])
Checkpoint readings.

D7 run_set_difference: last two runs (records[-2] as run_a, records[-1] as run_b)
Checkpoint ids read straight from the two records, not from eligibility. A
MISSED checkpoint is still on the route, so it counts as present; otherwise a
miss would look like a change of route.

D8 run_date_range: all runs
Run start_time. Runs with no start_time are left out of run_count.
span_days is the difference between the two calendar dates as recorded, each
in its own offset, with no conversion to UTC.
