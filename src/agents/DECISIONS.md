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
