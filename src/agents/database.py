"""The database B-1 queries: the runs and the extended record as tables, in memory.

Built after extend_record. Every value here is copied from the records or the
extended record; nothing is computed. Each row that holds record values carries
`path`, where it sits in the extended record, in the form fill_slots takes.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Sequence

from common.schema import Record
from agents.schema import DerivationConfig, ExtendedRecord
from agents.derivation import compute_eligibility
from agents.utils import SENSOR_BLOCKS, format_timestamp, item_id, read_path

# The schema in parts, so each derivation's SQL prompt can show only the tables it reads.
CORPUS = """
-- Exactly one row, describing the loaded runs as a whole.
CREATE TABLE corpus (
    run_count         INTEGER NOT NULL,
    first_run_id      TEXT NOT NULL REFERENCES runs,     -- the oldest run
    latest_run_id     TEXT NOT NULL REFERENCES runs,     -- the most recent run
    previous_run_id   TEXT REFERENCES runs,              -- the run before it; NULL with one run
    first_start_time  TEXT,                              -- the oldest run's start_time
    latest_start_time TEXT                               -- the most recent run's start_time
);
"""

RUNS = """
-- One row per run, oldest first.
CREATE TABLE runs (
    run_id                      TEXT PRIMARY KEY,
    run_order                   INTEGER NOT NULL UNIQUE,  -- 0 = oldest
    is_latest                   INTEGER NOT NULL,         -- 1 for the most recent run
    is_previous                 INTEGER NOT NULL,         -- 1 for the run before it

    -- The run's own fields, as recorded.
    facility_id                 TEXT NOT NULL,
    facility_name               TEXT NOT NULL,
    run_status                  TEXT NOT NULL,            -- RUNNING, COMPLETED, ABORTED
    final_status                TEXT NOT NULL,            -- PASS, FAIL, WARN
    start_time                  TEXT,
    end_time                    TEXT,                     -- NULL if the run did not finish
    duration                    TEXT,                     -- HH:MM:SS, as recorded
    progress_percentage         INTEGER,
    locked                      INTEGER,                  -- 1 or 0
    current_checkpoint_id       TEXT,                     -- non-NULL only mid-run

    -- Counts the robot declared. Claims, not counted from the run's lists:
    -- they may disagree with them.
    total_required_checkpoints  INTEGER,
    total_completed_checkpoints INTEGER,
    passed_checkpoints          INTEGER,
    failed_checkpoints          INTEGER,
    missed_checkpoints          INTEGER,
    warned_checkpoints          INTEGER,
    finding_count               INTEGER,

    path                        TEXT NOT NULL             -- records[i]
);
"""

# Retrieval: what each run recorded, as recorded, with nothing derived. Every
# row is one item of a run's list, and path is where it is in the records.
RECORDS = """
-- One row per checkpoint visit, in route order. A checkpoint visited twice has two rows.
CREATE TABLE rec_checkpoints (
    run_id          TEXT NOT NULL REFERENCES runs,
    position        INTEGER NOT NULL,                    -- order in the run's checkpoints, 0 = first
    checkpoint_id   TEXT NOT NULL,
    checkpoint_name TEXT,
    zone            TEXT,
    status          TEXT,                                -- COMPLETED or MISSED: whether the robot got there
    result_status   TEXT,                                -- PASS, FAIL or WARN: the verdict
    sequence_number INTEGER,
    timestamp       TEXT,
    missed_reason   TEXT,                                -- set when status is MISSED
    observed        TEXT,                                -- normal_scene, no_evidence, or what was seen
    expected_text   TEXT,
    notes           TEXT,
    rule_type       TEXT,
    confidence      REAL,                                -- NULL when not scored
    path            TEXT NOT NULL,                       -- records[i].checkpoints[position]
    PRIMARY KEY (run_id, position)
);

-- One row per finding the run logged.
CREATE TABLE rec_findings (
    run_id             TEXT NOT NULL REFERENCES runs,
    position           INTEGER NOT NULL,                 -- order in the run's findings, 0 = first
    finding_id         TEXT NOT NULL,
    severity           TEXT,                             -- info, warning or fail
    status             TEXT,                             -- logged, acknowledged or abstained (needs human review)
    timestamp          TEXT,
    checkpoint_id      TEXT,                             -- the checkpoint it came from
    zone               TEXT,
    feature            TEXT,
    description        TEXT,
    recommended_action TEXT,
    path               TEXT NOT NULL,                    -- records[i].findings[position]
    PRIMARY KEY (run_id, position)
);

-- One row per sensor alert: a threshold breach logged during the run. The
-- nearest checkpoint is proximity, not cause: "near", never "at".
CREATE TABLE rec_sensor_alerts (
    run_id                  TEXT NOT NULL REFERENCES runs,
    position                INTEGER NOT NULL,            -- order in the run's alerts, 0 = first
    code                    TEXT,
    severity                TEXT,                        -- warning or critical
    timestamp               TEXT,
    label                   TEXT,
    description             TEXT,
    nearest_checkpoint_id   TEXT,
    nearest_checkpoint_name TEXT,
    zone                    TEXT,
    path                    TEXT NOT NULL,               -- records[i].sensor_alerts[position]
    PRIMARY KEY (run_id, position)
);

-- One row per entry in the run's own event log.
CREATE TABLE rec_events (
    run_id          TEXT NOT NULL REFERENCES runs,
    position        INTEGER NOT NULL,                    -- order in the log, 0 = first
    event_id        TEXT,
    event_type      TEXT,                                -- run_started, checkpoint_completed, run_completed
    timestamp       TEXT,
    message         TEXT,
    status          TEXT,
    result_status   TEXT,
    checkpoint_id   TEXT,                                -- NULL on run-level events
    checkpoint_name TEXT,
    evidence_count  INTEGER,                             -- declared by the robot; a claim
    path            TEXT NOT NULL,                       -- records[i].event_log[position]
    PRIMARY KEY (run_id, position)
);

-- Sensor readings that passed the exclusion rule, one row per reading and
-- field. A reading from a sensor that was offline or flagged faulty, or from
-- a checkpoint that was not completed, is not here: rec_exclusions says why.
CREATE TABLE rec_readings (
    run_id          TEXT NOT NULL REFERENCES runs,
    kind            TEXT NOT NULL,                       -- checkpoint (one per stop) or sample (the continuous telemetry)
    source_id       TEXT NOT NULL,                       -- the checkpoint_id, or sample_0042 by position
    zone            TEXT,
    timestamp       TEXT,
    field_path      TEXT NOT NULL,                       -- block.field, e.g. environment.temperature_c
    value           REAL NOT NULL,
    stale           INTEGER NOT NULL,                    -- 1 when older than max_age_seconds when recorded
    path            TEXT NOT NULL,                       -- records[i].checkpoints[j].sensor.<field_path>, or a sample's: the value
    path_item       TEXT NOT NULL                        -- records[i].checkpoints[j].sensor or records[i].sensor_samples[j]:
                                                         -- where the reading's timestamp is (and a sample's zone)
);

-- What the exclusion rule left out of each run, and why: one row per scope,
-- sensor block and reason. scope is a checkpoint_id, or __run__ for the whole run.
CREATE TABLE rec_exclusions (
    run_id          TEXT NOT NULL REFERENCES runs,
    position        INTEGER NOT NULL,                    -- order in the run's list, 0 = first
    scope           TEXT,
    block           TEXT,                                -- the sensor block or record list, e.g. particulate
    reason          TEXT,                                -- e.g. sps30_ok=false
    path            TEXT NOT NULL,                       -- derived.excluded.<run_id>[position]
    PRIMARY KEY (run_id, position)
);


"""

# Derived values, as extend_record computed them.

D1 = """
-- D1 threshold_compare: one row per instance and run, its run-level output.
-- A NOT_COMPUTABLE run has status and reason and nothing else.
CREATE TABLE d1_threshold_compare (
    instance        TEXT NOT NULL,                       -- its name in derivations.yaml
    run_id          TEXT NOT NULL REFERENCES runs,
    status          TEXT NOT NULL,                       -- OK or NOT_COMPUTABLE
    reason          TEXT,                                -- why, when NOT_COMPUTABLE
    scope           TEXT,                                -- "checkpoints", or one checkpoint_id
    field_path      TEXT,                                -- e.g. environment.temperature_c
    threshold       REAL,
    direction       TEXT,                                -- above or below
    -- Only when scope is one checkpoint_id; with "checkpoints" each checkpoint has its own.
    checkpoint_name TEXT,                                -- the scope checkpoint's name
    value           REAL,
    exceeds         INTEGER,                             -- 1 or 0
    delta           REAL,                                -- value - threshold
    -- Only when scope is "checkpoints": how many OK checkpoints exceed it, and how many do not.
    exceeds_count     INTEGER,
    not_exceeds_count INTEGER,
    inputs_used     INTEGER,
    inputs_excluded INTEGER,
    path            TEXT NOT NULL,                       -- derived.values.<instance>[i]
    PRIMARY KEY (instance, run_id)
);

-- D1 per checkpoint: one row per checkpoint in a run's "checkpoints" output,
-- only when the instance's scope is "checkpoints". NOT_COMPUTABLE checkpoints
-- are kept, with their reason. field_path, threshold and direction are on the
-- d1_threshold_compare row: join on (instance, run_id).
CREATE TABLE d1_checkpoints (
    instance        TEXT NOT NULL,
    run_id          TEXT NOT NULL REFERENCES runs,
    checkpoint_id   TEXT NOT NULL,
    checkpoint_name TEXT NOT NULL,                       -- the checkpoint's name in the run
    position        INTEGER NOT NULL,                    -- order in the run's output, 0 = first
    status          TEXT NOT NULL,                       -- OK or NOT_COMPUTABLE
    reason          TEXT,                                -- why, when NOT_COMPUTABLE
    value           REAL,                                -- NULL when NOT_COMPUTABLE
    exceeds         INTEGER,                             -- 1 or 0; NULL when NOT_COMPUTABLE
    delta           REAL,                                -- value - threshold
    path            TEXT NOT NULL,                       -- derived.values.<instance>[i].checkpoints.<checkpoint_id>
    path_record     TEXT NOT NULL,                       -- records[i].checkpoints[j]: where checkpoint_id is recorded
    PRIMARY KEY (instance, run_id, checkpoint_id),
    FOREIGN KEY (instance, run_id) REFERENCES d1_threshold_compare
);
"""

D2 = """
-- D2 condition_count: one row per instance and run: how many items of scope
-- meet the instance's named condition (e.g. result_status_is_fail).
CREATE TABLE d2_condition_count (
    instance        TEXT NOT NULL,                       -- its name in derivations.yaml
    run_id          TEXT NOT NULL REFERENCES runs,
    status          TEXT NOT NULL,                       -- OK or NOT_COMPUTABLE
    reason          TEXT,                                -- why, when NOT_COMPUTABLE
    scope           TEXT,                                -- checkpoints, findings or sensor_alerts
    condition       TEXT,                                -- the condition's name in derivations.yaml
    count           INTEGER,                             -- items that meet the condition
    population      INTEGER,                             -- items it could be checked on, after exclusions
    inputs_excluded INTEGER,                             -- items the exclusion rule left out
    path            TEXT NOT NULL,                       -- derived.values.<instance>[i]
    PRIMARY KEY (instance, run_id)
);

-- D2 per matching item: one row per entry in a run's matching_ids, in order.
-- A checkpoint visited twice can appear twice, at two positions.
CREATE TABLE d2_matching_ids (
    instance        TEXT NOT NULL,
    run_id          TEXT NOT NULL REFERENCES runs,
    item_id         TEXT NOT NULL,                       -- checkpoint_id, finding_id, or sensor_alert_0003 by position
    position        INTEGER NOT NULL,                    -- order in matching_ids, 0 = first
    path            TEXT NOT NULL,                       -- derived.values.<instance>[i].matching_ids[position]
    path_record     TEXT NOT NULL,                       -- records[i].<scope>[j]: the first item with this id
    PRIMARY KEY (instance, run_id, position),
    FOREIGN KEY (instance, run_id) REFERENCES d2_condition_count
);

-- D2 per excluded item: one row per entry in a run's excluded_items, in record
-- order: an item the exclusion rule left out of the count, and why.
CREATE TABLE d2_excluded_items (
    instance        TEXT NOT NULL,
    run_id          TEXT NOT NULL REFERENCES runs,
    item_id         TEXT,                                -- checkpoint_id; NULL for findings and sensor alerts
    reason          TEXT NOT NULL,                       -- why it was left out, e.g. checkpoint status is MISSED
    count           INTEGER NOT NULL,                    -- items this row covers: 2 for a checkpoint visited twice
    position        INTEGER NOT NULL,                    -- order in excluded_items, 0 = first
    path            TEXT NOT NULL,                       -- derived.values.<instance>[i].excluded_items[position]
    PRIMARY KEY (instance, run_id, position),
    FOREIGN KEY (instance, run_id) REFERENCES d2_condition_count
);
"""

D3 = """
-- D3 proportion: one row per instance and run: what share of the items of the
-- instance's scope meet its named condition (e.g. status_is_completed).
-- A NOT_COMPUTABLE run (denominator is zero) has status and reason and nothing else.
CREATE TABLE d3_proportion (
    instance        TEXT NOT NULL,                       -- its name in derivations.yaml
    run_id          TEXT NOT NULL REFERENCES runs,
    status          TEXT NOT NULL,                       -- OK or NOT_COMPUTABLE
    reason          TEXT,                                -- why, when NOT_COMPUTABLE
    numerator       INTEGER,                             -- items that meet the condition
    denominator     INTEGER,                             -- items it could be checked on, after exclusions
    proportion      REAL,                                -- numerator / denominator, e.g. 0.875
    percentage      REAL,                                -- the same share out of 100, e.g. 87.5
    inputs_excluded INTEGER,                             -- items the exclusion rule left out
    path            TEXT NOT NULL,                       -- derived.values.<instance>[i]
    PRIMARY KEY (instance, run_id)
);

-- D3 per numerator item: one row per entry in a run's numerator_ids, in order.
-- A checkpoint visited twice can appear twice, at two positions.
CREATE TABLE d3_numerator_ids (
    instance        TEXT NOT NULL,
    run_id          TEXT NOT NULL REFERENCES runs,
    item_id         TEXT NOT NULL,                       -- checkpoint_id, finding_id, or sensor_alert_0003 by position
    position        INTEGER NOT NULL,                    -- order in numerator_ids, 0 = first
    path            TEXT NOT NULL,                       -- derived.values.<instance>[i].numerator_ids[position]
    path_record     TEXT NOT NULL,                       -- records[i].<scope>[j]: the first item with this id
    PRIMARY KEY (instance, run_id, position),
    FOREIGN KEY (instance, run_id) REFERENCES d3_proportion
);
"""

# D4 group_mean: one table per instance, d4_<instance>, since each instance is a
# mean of a different field. d4_schema fills this in for each one from derivations.yaml.
D4_TABLE = """
-- D4 {instance}: the mean of {field_path} over each run's {source}, per {group_by}.
-- Per zone: a zone is an area of the route (e.g. rowA_back) holding one or more
-- checkpoints (e.g. a3_back); the mean is over every reading in the zone.
-- One row per group and run. A run where no group could be worked out has a
-- single row: group_key NULL, status NOT_COMPUTABLE, and the reason.
CREATE TABLE d4_{instance} (
    instance        TEXT NOT NULL,                       -- '{instance}' on every row: says which mean a row is when tables are combined
    run_id          TEXT NOT NULL REFERENCES runs,
    group_key       TEXT,                                -- the group's {group_by}; NULL on a whole-run NOT_COMPUTABLE row
    position        INTEGER NOT NULL,                    -- order of the group in the run's output, 0 = first
    status          TEXT NOT NULL,                       -- OK or NOT_COMPUTABLE
    reason          TEXT,                                -- why, when NOT_COMPUTABLE
    mean            REAL,                                -- mean of {field_path} in the group; NULL when NOT_COMPUTABLE
    n               INTEGER,                             -- values the mean is over, after exclusions
    inputs_excluded INTEGER,                             -- values the exclusion rule left out of the group
    inputs_stale    INTEGER,                             -- values used that were stale
    path            TEXT NOT NULL,                       -- derived.values.{instance}[i].groups.<group_key>, or derived.values.{instance}[i]
    UNIQUE (run_id, group_key)
);

-- D4 {instance}, one row per checkpoint in a group's zone, from the run's
-- checkpoint records. Only when grouped per zone. The mean is the zone's, over
-- every reading in it, not the checkpoint's own.
CREATE TABLE d4_{instance}_checkpoints (
    instance        TEXT NOT NULL,                       -- '{instance}' on every row
    run_id          TEXT NOT NULL REFERENCES runs,
    group_key       TEXT,                                -- the zone, as in d4_{instance}.group_key
    checkpoint_id   TEXT NOT NULL,
    checkpoint_name TEXT,
    position        INTEGER NOT NULL,                    -- order in the zone's checkpoints, 0 = first
    path            TEXT NOT NULL,                       -- derived.values.{instance}[i].groups.<group_key>.checkpoints[position]
    UNIQUE (run_id, group_key, checkpoint_id)
);
"""

# D5 group_max: one table per instance, d5_<instance>, as for D4. d5_schema
# fills this in for each one from derivations.yaml.
D5_TABLE = """
-- D5 {instance}: the maximum of {field_path} over each run's {source}, per {group_by},
-- and which reading it was. Per zone: a zone is an area of the route (e.g.
-- rowA_back) holding one or more checkpoints (e.g. a3_back). One row per group and run. A run where no group
-- could be worked out has a single row: group_key NULL, status NOT_COMPUTABLE,
-- and the reason.
CREATE TABLE d5_{instance} (
    instance         TEXT NOT NULL,                      -- '{instance}' on every row: says which maximum a row is when tables are combined
    run_id           TEXT NOT NULL REFERENCES runs,
    group_key        TEXT,                               -- the group's {group_by}; NULL on a whole-run NOT_COMPUTABLE row
    position         INTEGER NOT NULL,                   -- order of the group in the run's output, 0 = first
    status           TEXT NOT NULL,                      -- OK or NOT_COMPUTABLE
    reason           TEXT,                               -- why, when NOT_COMPUTABLE
    max              REAL,                               -- the highest {field_path} in the group; NULL when NOT_COMPUTABLE
    source_id        TEXT,                               -- the reading it came from: sample_0042 by position, or a checkpoint_id
    source_timestamp TEXT,                               -- when that reading was taken
    tied_with        TEXT,                               -- other readings with the same maximum, comma separated; NULL when none
    n                INTEGER,                            -- values the maximum is over, after exclusions
    inputs_excluded  INTEGER,                            -- values the exclusion rule left out of the group
    inputs_stale     INTEGER,                            -- values used that were stale
    path             TEXT NOT NULL,                      -- derived.values.{instance}[i].groups.<group_key>, or derived.values.{instance}[i]
    path_record      TEXT,                               -- records[i].sensor_samples[j] or records[i].checkpoints[j]: the source_id reading
    UNIQUE (run_id, group_key)
);

-- D5 {instance}, one row per checkpoint in a group's zone, from the run's
-- checkpoint records. Only when grouped per zone. The maximum is the zone's,
-- over every reading in it; source_id says which reading it was.
CREATE TABLE d5_{instance}_checkpoints (
    instance        TEXT NOT NULL,                       -- '{instance}' on every row
    run_id          TEXT NOT NULL REFERENCES runs,
    group_key       TEXT,                                -- the zone, as in d5_{instance}.group_key
    checkpoint_id   TEXT NOT NULL,
    checkpoint_name TEXT,
    position        INTEGER NOT NULL,                    -- order in the zone's checkpoints, 0 = first
    path            TEXT NOT NULL,                       -- derived.values.{instance}[i].groups.<group_key>.checkpoints[position]
    UNIQUE (run_id, group_key, checkpoint_id)
);
"""

# D6 rank_top_n: two tables per instance, d6_<instance> (one row per run) and
# d6_<instance>_ranking (one row per ranked reading). d6_schema fills this in
# for each one from derivations.yaml.
D6_TABLES = """
-- D6 {instance}: the {n} highest {field_path} readings over each run's {source},
-- highest first. One row per run. A NOT_COMPUTABLE run has status and reason
-- and nothing else.
CREATE TABLE d6_{instance} (
    instance        TEXT NOT NULL,                       -- '{instance}' on every row: says which ranking a row is when tables are combined
    run_id          TEXT NOT NULL REFERENCES runs,
    status          TEXT NOT NULL,                       -- OK or NOT_COMPUTABLE
    reason          TEXT,                                -- why, when NOT_COMPUTABLE
    n_requested     INTEGER,                             -- how many highest readings were asked for ({n})
    n_returned      INTEGER,                             -- how many are ranked: fewer when fewer readings were eligible,
                                                         -- more when readings tie with the last place
    population      INTEGER,                             -- eligible readings the ranking was taken from, after exclusions
    inputs_excluded INTEGER,                             -- readings the exclusion rule left out
    path            TEXT NOT NULL,                       -- derived.values.{instance}[i]
    PRIMARY KEY (run_id)
);

-- D6 {instance}, one row per ranked reading, in rank order.
CREATE TABLE d6_{instance}_ranking (
    instance        TEXT NOT NULL,                       -- '{instance}' on every row
    run_id          TEXT NOT NULL REFERENCES d6_{instance},
    rank            INTEGER NOT NULL,                    -- 1 = highest; equal readings take the next ranks, earliest first
    item_id         TEXT NOT NULL,                       -- which reading: a checkpoint_id, or sample_0042 by position
    value           REAL NOT NULL,                       -- the reading
    path            TEXT NOT NULL,                       -- derived.values.{instance}[i].ranking[rank - 1]
    path_record     TEXT,                                -- records[i].checkpoints[j] or records[i].sensor_samples[j]: the reading
    PRIMARY KEY (run_id, rank)
);
"""

D7 = """
-- D7 run_set_difference: one row per instance. Compares the run before the
-- latest (run_a) with the latest run (run_b): which checkpoints each has. It
-- says what differs, nothing about whether that is better or worse.
-- NOT_COMPUTABLE (fewer than two runs) has status and reason and nothing else.
CREATE TABLE d7_run_set_difference (
    instance        TEXT PRIMARY KEY,                    -- its name in derivations.yaml
    status          TEXT NOT NULL,                       -- OK or NOT_COMPUTABLE
    reason          TEXT,                                -- why, when NOT_COMPUTABLE
    run_a           TEXT REFERENCES runs,                -- the run before the latest
    run_b           TEXT REFERENCES runs,                -- the latest run
    count_only_in_a INTEGER,                             -- checkpoints in run_a and not in run_b
    count_only_in_b INTEGER,                             -- checkpoints in run_b and not in run_a
    count_in_both   INTEGER,                             -- checkpoints in both runs
    path            TEXT NOT NULL                        -- derived.values.<instance>
);

-- D7 per checkpoint: one row per checkpoint in only_in_a, only_in_b or in_both,
-- in route order.
CREATE TABLE d7_checkpoints (
    instance        TEXT NOT NULL REFERENCES d7_run_set_difference,
    checkpoint_id   TEXT NOT NULL,
    membership      TEXT NOT NULL,                       -- only_in_a, only_in_b or in_both
    position        INTEGER NOT NULL,                    -- order in its list, 0 = first
    path            TEXT NOT NULL,                       -- derived.values.<instance>.<membership>[position]
    PRIMARY KEY (instance, checkpoint_id)
);
"""

D8 = """
-- D8 run_date_range: one row per instance, over every loaded run with a start
-- time. Dates are as recorded, in each run's own offset.
-- NOT_COMPUTABLE (no run has a start time) has status and reason and nothing else.
CREATE TABLE d8_run_date_range (
    instance        TEXT PRIMARY KEY,                    -- its name in derivations.yaml
    status          TEXT NOT NULL,                       -- OK or NOT_COMPUTABLE
    reason          TEXT,                                -- why, when NOT_COMPUTABLE
    run_count       INTEGER,                             -- runs with a start time
    runs_without_start INTEGER,                          -- runs with no start time, in no day
    earliest        TEXT,                                -- the earliest start time, as recorded
    latest          TEXT,                                -- the latest start time, as recorded
    earliest_run_id TEXT REFERENCES runs,                -- the run that started first
    latest_run_id   TEXT REFERENCES runs,                -- the run that started last
    span_days       INTEGER,                             -- calendar days from the earliest date to the latest
    day_count       INTEGER,                             -- days on which at least one run started
    days_without_run INTEGER,                            -- days in the span, both ends included, with no run
    path            TEXT NOT NULL                        -- derived.values.<instance>
);

-- D8 per day: one row per day a run started on, oldest first.
CREATE TABLE d8_days (
    instance        TEXT NOT NULL REFERENCES d8_run_date_range,
    date            TEXT NOT NULL,                       -- YYYY-MM-DD
    weekday         TEXT,                                -- e.g. Friday
    run_count       INTEGER,                             -- runs that started that day
    position        INTEGER NOT NULL,                    -- order in days, 0 = the earliest day
    path            TEXT NOT NULL,                       -- derived.values.<instance>.days[position]
    PRIMARY KEY (instance, date)
);

-- D8 per run: one row per run in a day, in the order they started.
CREATE TABLE d8_day_runs (
    instance        TEXT NOT NULL REFERENCES d8_run_date_range,
    date            TEXT NOT NULL,                       -- the day, as in d8_days.date
    run_id          TEXT NOT NULL REFERENCES runs,
    position        INTEGER NOT NULL,                    -- order within the day, 0 = first
    path            TEXT NOT NULL,                       -- derived.values.<instance>.days[k].run_ids[position]
    PRIMARY KEY (instance, run_id)
);
"""

SCHEMA = CORPUS + RUNS + RECORDS + D1 + D2 + D3 + D7 + D8


def d4_schema(config: DerivationConfig, names: Sequence[str] | None = None) -> str:
    """The D4 tables for every group_mean instance, or only those in names."""
    return per_instance_schema(config, "group_mean", D4_TABLE, names)


def d5_schema(config: DerivationConfig, names: Sequence[str] | None = None) -> str:
    """The D5 tables for every group_max instance, or only those in names."""
    return per_instance_schema(config, "group_max", D5_TABLE, names)


def d6_schema(config: DerivationConfig, names: Sequence[str] | None = None) -> str:
    """The D6 tables for every rank_top_n instance, or only those in names."""
    return per_instance_schema(config, "rank_top_n", D6_TABLES, names)


def per_instance_schema(config: DerivationConfig, kind: str, table: str, names: Sequence[str] | None) -> str:
    """table filled in for each instance of type kind, from its derivations.yaml entry.

    Raises ValueError for an instance name that cannot be a table name.
    """
    parts = []
    for name, entry in config.derivations.items():
        if entry.get("type") != kind or (names is not None and name not in names):
            continue
        if not re.fullmatch(r"[A-Za-z_]\w*", name):
            raise ValueError(f"{kind} instance {name!r} cannot be a table name")
        parts.append(table.format(instance=name, **entry))
    return "".join(parts)


def build_database(extended: ExtendedRecord, config: DerivationConfig) -> sqlite3.Connection:
    """The extended record as an in-memory database. config gives the timestamp format."""
    if not extended.records:
        raise ValueError("no runs to build the database from")
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA + d4_schema(config) + d5_schema(config) + d6_schema(config))
    add_runs(conn, extended.records, config)
    add_corpus(conn, extended.records, config)
    add_d1(conn, extended.records, extended.derived.values)
    add_d2(conn, extended.records, extended.derived.values, config)
    add_d3(conn, extended.records, extended.derived.values, config)
    add_grouped(conn, extended.records, extended.derived.values, config, "group_mean", "d4")
    add_grouped(conn, extended.records, extended.derived.values, config, "group_max", "d5")
    add_d6(conn, extended.records, extended.derived.values, config)
    add_d7(conn, extended.derived.values)
    add_d8(conn, extended.derived.values)
    add_records(conn, extended, config)
    conn.commit()
    # Built once, then read-only.
    conn.execute("PRAGMA query_only = ON")
    return conn


def open_database(extended: ExtendedRecord, config: DerivationConfig, path: Path) -> tuple[sqlite3.Connection, bool]:
    """The database for extended, in memory, and whether it was reused.

    A copy is kept at path between runs, with the fingerprint it was built
    from. When the fingerprint still matches, the copy is loaded into memory
    instead of building again; otherwise the database is built and the copy
    replaced. A copy that cannot be read is rebuilt.
    """
    fingerprint = database_fingerprint(extended, config)
    if path.is_file():
        try:
            disk = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                stored = disk.execute("SELECT fingerprint FROM build").fetchone()
                if stored and stored[0] == fingerprint:
                    conn = sqlite3.connect(":memory:")
                    disk.backup(conn)
                    conn.execute("DROP TABLE build")   # bookkeeping, not for queries
                    conn.execute("PRAGMA query_only = ON")
                    return conn, True
            finally:
                disk.close()
        except sqlite3.Error:
            pass   # an older or damaged copy: build a new one

    conn = build_database(extended, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    disk = sqlite3.connect(temporary)
    try:
        conn.backup(disk)
        disk.execute("CREATE TABLE build (fingerprint TEXT NOT NULL)")
        disk.execute("INSERT INTO build VALUES (?)", (fingerprint,))
        disk.commit()
    finally:
        disk.close()
    temporary.replace(path)   # whole, or not at all
    return conn, False


def database_fingerprint(extended: ExtendedRecord, config: DerivationConfig) -> str:
    """sha256 of everything the database is built from: the records, the
    derived values, the configs (their hash) and this module's code."""
    text = json.dumps({
        "records": [asdict(record) for record in extended.records],
        "values": extended.derived.values,
        "config_hash": config.config_hash,
        "code": Path(__file__).read_text(encoding="utf-8"),
    }, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class QueryError(ValueError):
    """The query was refused or failed. The message goes back to the model."""


# The only SQL functions a query may call. Aggregates are left out on purpose:
# every count is already in a table, and the model never computes one.
ALLOWED_FUNCTIONS = {"lower", "upper", "like", "coalesce", "ifnull"}


def run_query(
    conn: sqlite3.Connection, sql: str, max_rows: int, timeout_seconds: float,
) -> tuple[list[str], list[tuple]]:
    """Run one read-only SELECT and return (column names, rows).

    Refuses anything but reads and ALLOWED_FUNCTIONS, stops at the timeout, and
    fails past max_rows rather than cutting the result short. Raises QueryError.
    """
    if not sql.lstrip().lower().startswith("select"):
        raise QueryError("the query must be one SELECT statement")

    def authorize(action, arg1, arg2, db_name, source):
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and arg2.lower() in ALLOWED_FUNCTIONS:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    deadline = time.monotonic() + timeout_seconds
    conn.set_authorizer(authorize)
    conn.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
    try:
        cursor = conn.execute(sql)
        rows = cursor.fetchmany(max_rows + 1)
    except (sqlite3.Error, sqlite3.Warning) as error:
        # "not authorized" for a refused table or function, "interrupted" at the timeout.
        raise QueryError(f"the query failed: {error}") from None
    finally:
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)
    if len(rows) > max_rows:
        raise QueryError(f"the query returned more than {max_rows} rows; narrow it")
    return [d[0] for d in cursor.description], rows


def insert(conn: sqlite3.Connection, table: str, row: dict) -> None:
    """One row into table. The row's keys are its column names."""
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", list(row.values()))


def add_runs(conn: sqlite3.Connection, records: Sequence[Record], config: DerivationConfig) -> None:
    """One runs row per record, oldest first."""
    last = len(records) - 1
    for i, record in enumerate(records):
        insert(conn, "runs", {
            "run_id": record.run_id,
            "run_order": i,
            "is_latest": int(i == last),
            "is_previous": int(i == last - 1),
            "facility_id": record.facility_id,
            "facility_name": record.facility_name,
            "run_status": record.run_status,
            "final_status": record.final_status,
            "start_time": format_timestamp(record.start_time, config),
            "end_time": format_timestamp(record.end_time, config),
            "duration": record.duration,
            "progress_percentage": record.progress_percentage,
            "locked": record.locked,
            "current_checkpoint_id": record.current_checkpoint_id,
            "total_required_checkpoints": record.total_required_checkpoints,
            "total_completed_checkpoints": record.total_completed_checkpoints,
            "passed_checkpoints": record.passed_checkpoints,
            "failed_checkpoints": record.failed_checkpoints,
            "missed_checkpoints": record.missed_checkpoints,
            "warned_checkpoints": record.warned_checkpoints,
            "finding_count": record.finding_count,
            "path": f"records[{i}]",
        })


def add_corpus(conn: sqlite3.Connection, records: Sequence[Record], config: DerivationConfig) -> None:
    """The one corpus row."""
    insert(conn, "corpus", {
        "run_count": len(records),
        "first_run_id": records[0].run_id,
        "latest_run_id": records[-1].run_id,
        "previous_run_id": records[-2].run_id if len(records) > 1 else None,
        "first_start_time": format_timestamp(records[0].start_time, config),
        "latest_start_time": format_timestamp(records[-1].start_time, config),
    })


def instances(values: dict, derivation: str):
    """(instance, output) for each instance of derivation: a list of per-run
    outputs for D1-D6, one output for D7 and D8."""
    for instance, out in values.items():
        first = out[0] if isinstance(out, list) and out else out
        if isinstance(first, dict) and first.get("derivation") == derivation:
            yield instance, out


def stored(out: dict, *leave_out: str) -> dict:
    """An output's fields as columns: without derivation, the keys in
    leave_out (lists that get tables of their own) and *_formatted strings,
    which fill_slots finds from the number's path."""
    return {
        key: value for key, value in out.items()
        if key != "derivation" and key not in leave_out and not key.endswith("_formatted")
    }


def add_d1(conn: sqlite3.Connection, records: Sequence[Record], values: dict) -> None:
    """d1_threshold_compare: one row per run; d1_checkpoints: one per checkpoint.

    A checkpoint's path_record is its first entry in the run, the visit D1 keeps too.
    """
    # run_id -> (its index in records, checkpoint_id -> its first index in the run)
    where = {}
    for i, record in enumerate(records):
        first = {}
        for j, checkpoint in enumerate(record.checkpoints):
            first.setdefault(checkpoint.checkpoint_id, j)
        where[record.run_id] = (i, first)
    for instance, outputs in instances(values, "threshold_compare"):
        for k, out in enumerate(outputs):
            path = f"derived.values.{instance}[{k}]"
            insert(conn, "d1_threshold_compare", {"instance": instance, "path": path, **stored(out, "checkpoints")})
            i, positions = where[out["run_id"]]
            for position, (cid, result) in enumerate(out.get("checkpoints", {}).items()):
                insert(conn, "d1_checkpoints", {
                    "instance": instance, "run_id": out["run_id"], "checkpoint_id": cid,
                    "position": position, "path": f"{path}.checkpoints.{cid}",
                    "path_record": f"records[{i}].checkpoints[{positions[cid]}]",
                    **stored(result),
                })


def add_d2(conn: sqlite3.Connection, records: Sequence[Record], values: dict, config: DerivationConfig) -> None:
    """d2_condition_count: one row per run; d2_matching_ids and d2_excluded_items: one per item.

    An item's path_record is the first item in the run's scope with that id.
    """
    index = {record.run_id: i for i, record in enumerate(records)}
    for instance, outputs in instances(values, "condition_count"):
        for k, out in enumerate(outputs):
            path = f"derived.values.{instance}[{k}]"
            row = stored(out, "matching_ids", "excluded_items")
            insert(conn, "d2_condition_count", {"instance": instance, "path": path, **row})
            if "matching_ids" not in out:
                continue   # NOT_COMPUTABLE
            i = index[out["run_id"]]
            scope = out["scope"]
            first = first_items(records[i], scope, config)
            for position, mid in enumerate(out["matching_ids"]):
                insert(conn, "d2_matching_ids", {
                    "instance": instance, "run_id": out["run_id"], "item_id": mid, "position": position,
                    "path": f"{path}.matching_ids[{position}]",
                    "path_record": f"records[{i}].{scope}[{first[mid]}]",
                })
            for position, item in enumerate(out["excluded_items"]):
                insert(conn, "d2_excluded_items", {
                    "instance": instance, "run_id": out["run_id"], **item, "position": position,
                    "path": f"{path}.excluded_items[{position}]",
                })


def add_d3(conn: sqlite3.Connection, records: Sequence[Record], values: dict, config: DerivationConfig) -> None:
    """d3_proportion: one row per run; d3_numerator_ids: one per numerator item.

    D3's output has no scope, so path_record takes it from derivations.yaml.
    """
    index = {record.run_id: i for i, record in enumerate(records)}
    for instance, outputs in instances(values, "proportion"):
        scope = config.derivations[instance]["scope"]
        for k, out in enumerate(outputs):
            path = f"derived.values.{instance}[{k}]"
            insert(conn, "d3_proportion", {"instance": instance, "path": path, **stored(out, "numerator_ids")})
            if "numerator_ids" not in out:
                continue   # NOT_COMPUTABLE
            i = index[out["run_id"]]
            first = first_items(records[i], scope, config)
            for position, nid in enumerate(out["numerator_ids"]):
                insert(conn, "d3_numerator_ids", {
                    "instance": instance, "run_id": out["run_id"], "item_id": nid, "position": position,
                    "path": f"{path}.numerator_ids[{position}]",
                    "path_record": f"records[{i}].{scope}[{first[nid]}]",
                })


def add_grouped(
    conn: sqlite3.Connection, records: Sequence[Record], values: dict, config: DerivationConfig,
    derivation: str, prefix: str,
) -> None:
    """D4 and D5: each instance into its own <prefix>_<instance> table, one row
    per group and run, or one NOT_COMPUTABLE row for a run with no groups; each
    group's checkpoints (per zone only) into <prefix>_<instance>_checkpoints.

    D5 rows also get path_record, the source_id reading in the record, and
    tied_with stored comma separated, the way fill_slots prints a list.

    Samples with no zone are grouped under the key None; such a group's path
    ends ".groups.None", which slots.resolve follows. It is stored with
    group_key NULL, like a whole-run NOT_COMPUTABLE row; status tells them apart.
    """
    index = {record.run_id: i for i, record in enumerate(records)}
    for instance, outputs in instances(values, derivation):
        table = f"{prefix}_{instance}"   # checked by per_instance_schema when the table was made
        source = config.derivations[instance]["source"]
        for k, out in enumerate(outputs):
            path = f"derived.values.{instance}[{k}]"
            if "groups" not in out:
                insert(conn, table, {
                    "instance": instance, "run_id": out["run_id"], "group_key": None, "position": 0,
                    "status": out["status"], "reason": out.get("reason"), "path": path,
                })
                continue
            i = index[out["run_id"]]
            where = reading_paths(records[i], i, source, config) if derivation == "group_max" else {}
            for position, (key, group) in enumerate(out["groups"].items()):
                row = {
                    "instance": instance, "run_id": out["run_id"], "group_key": key, "position": position,
                    "path": f"{path}.groups.{key}", **stored(group, "checkpoints"),
                }
                if derivation == "group_max":
                    row["path_record"] = where.get(group.get("source_id"))
                    if "tied_with" in row:
                        row["tied_with"] = ", ".join(row["tied_with"])
                insert(conn, table, row)
                for j, cp in enumerate(group.get("checkpoints", [])):
                    insert(conn, f"{table}_checkpoints", {
                        "instance": instance, "run_id": out["run_id"], "group_key": key, **cp,
                        "position": j, "path": f"{path}.groups.{key}.checkpoints[{j}]",
                    })


def add_d6(conn: sqlite3.Connection, records: Sequence[Record], values: dict, config: DerivationConfig) -> None:
    """d6_<instance>: one row per run; d6_<instance>_ranking: one per ranked reading.

    field_path is left out: it is the instance's, in the table's comment.
    path_record is the ranked reading in the record.
    """
    index = {record.run_id: i for i, record in enumerate(records)}
    for instance, outputs in instances(values, "rank_top_n"):
        source = config.derivations[instance]["source"]
        for k, out in enumerate(outputs):
            path = f"derived.values.{instance}[{k}]"
            insert(conn, f"d6_{instance}", {"instance": instance, "path": path, **stored(out, "field_path", "ranking")})
            if "ranking" not in out:
                continue   # NOT_COMPUTABLE
            i = index[out["run_id"]]
            where = reading_paths(records[i], i, source, config)
            for p, entry in enumerate(out["ranking"]):
                insert(conn, f"d6_{instance}_ranking", {
                    "instance": instance, "run_id": out["run_id"], "rank": entry["rank"],
                    "item_id": entry["id"], "value": entry["value"],
                    "path": f"{path}.ranking[{p}]", "path_record": where.get(entry["id"]),
                })


def add_d7(conn: sqlite3.Connection, values: dict) -> None:
    """d7_run_set_difference: one row; d7_checkpoints: one per checkpoint in its three lists."""
    lists = ("only_in_a", "only_in_b", "in_both")
    for instance, out in instances(values, "run_set_difference"):
        path = f"derived.values.{instance}"
        insert(conn, "d7_run_set_difference", {"instance": instance, "path": path, **stored(out, *lists)})
        for membership in lists:
            for position, cid in enumerate(out.get(membership, [])):
                insert(conn, "d7_checkpoints", {
                    "instance": instance, "checkpoint_id": cid, "membership": membership,
                    "position": position, "path": f"{path}.{membership}[{position}]",
                })


def add_d8(conn: sqlite3.Connection, values: dict) -> None:
    """d8_run_date_range: one row; d8_days: one per day; d8_day_runs: one per run in a day."""
    for instance, out in instances(values, "run_date_range"):
        path = f"derived.values.{instance}"
        insert(conn, "d8_run_date_range", {"instance": instance, "path": path, **stored(out, "days")})
        for k, day in enumerate(out.get("days", [])):
            insert(conn, "d8_days", {
                "instance": instance, "date": day["date"], "weekday": day["weekday"],
                "run_count": day["run_count"], "position": k, "path": f"{path}.days[{k}]",
            })
            for j, run_id in enumerate(day["run_ids"]):
                insert(conn, "d8_day_runs", {
                    "instance": instance, "date": day["date"], "run_id": run_id,
                    "position": j, "path": f"{path}.days[{k}].run_ids[{j}]",
                })


# rec_ table -> (the Record list it reads, the item fields it copies). Each
# column is the field's own name, so a cited value is <path>.<column>.
RECORD_LISTS = {
    "rec_checkpoints": ("checkpoints", (
        "checkpoint_id", "checkpoint_name", "zone", "status", "result_status", "sequence_number", "timestamp",
        "missed_reason", "observed", "expected_text", "notes", "rule_type", "confidence",
    )),
    "rec_findings": ("findings", (
        "finding_id", "severity", "status", "timestamp", "checkpoint_id", "zone", "feature", "description",
        "recommended_action",
    )),
    "rec_sensor_alerts": ("sensor_alerts", (
        "code", "severity", "timestamp", "label", "description", "nearest_checkpoint_id",
        "nearest_checkpoint_name", "zone",
    )),
    "rec_events": ("event_log", (
        "event_id", "event_type", "timestamp", "message", "status", "result_status", "checkpoint_id",
        "checkpoint_name", "evidence_count",
    )),
}


def add_records(conn: sqlite3.Connection, extended: ExtendedRecord, config: DerivationConfig) -> None:
    """The rec_ tables: each run's lists as recorded, its readings that passed
    the exclusion rule, and what the rule left out.

    No sensor value is copied from a record directly: rec_readings takes them
    from compute_eligibility, the same rule the derivations use, so a value
    from a sensor flagged faulty is never in the database.
    """
    for i, record in enumerate(extended.records):
        for table, (attr, names) in RECORD_LISTS.items():
            for j, item in enumerate(getattr(record, attr)):
                row = {"run_id": record.run_id, "position": j, "path": f"records[{i}].{attr}[{j}]"}
                for name in names:
                    value = getattr(item, name)
                    row[name] = format_timestamp(value, config) if isinstance(value, datetime) else value
                insert(conn, table, row)
        for j, entry in enumerate(extended.derived.excluded.get(record.run_id, [])):
            insert(conn, "rec_exclusions", {
                "run_id": record.run_id, "position": j, "scope": entry["scope"], "block": entry["block"],
                "reason": entry["reason"], "path": f"derived.excluded.{record.run_id}[{j}]",
            })
    add_readings(conn, extended.records, config)


def add_readings(conn: sqlite3.Connection, records: Sequence[Record], config: DerivationConfig) -> None:
    """One rec_readings row per eligible value of every sensor field."""
    index = {record.run_id: i for i, record in enumerate(records)}
    for field_path, (values, _) in compute_eligibility(records, config).items():
        if field_path.split(".", 1)[0] not in SENSOR_BLOCKS:
            continue   # a record field a derivation reads, e.g. checkpoints.result_status
        by_run: dict[tuple, list] = {}
        for v in values:
            by_run.setdefault((v.run_id, v.kind), []).append(v)
        for (run_id, kind), run_values in by_run.items():
            i = index[run_id]
            if kind == "sample":
                where = reading_paths(records[i], i, "samples", config)
                items = [where[v.source_id] for v in run_values]
                paths = [f"{item}.sensor.{field_path}" for item in items]
            else:
                paths = visit_paths(records[i], i, run_values, field_path)
                items = [path.removesuffix(f".{field_path}") for path in paths]   # the checkpoint's sensor block
            for v, path, item in zip(run_values, paths, items):
                insert(conn, "rec_readings", {
                    "run_id": run_id, "kind": kind, "source_id": v.source_id, "zone": v.zone,
                    "timestamp": format_timestamp(v.timestamp, config), "field_path": field_path,
                    "value": v.value, "stale": int(v.stale), "path": path, "path_item": item,
                })


def visit_paths(record: Record, i: int, values: list, field_path: str) -> list[str]:
    """The path of each eligible checkpoint value to the visit it was read at.

    eligible_values keeps the run's checkpoint order and only skips, so each
    value is the next visit with its checkpoint_id and that reading. This
    keeps a checkpoint visited twice apart.
    """
    paths, j = [], 0
    for v in values:
        while not (record.checkpoints[j].checkpoint_id == v.source_id
                   and read_path(record.checkpoints[j].sensor, field_path) == v.value):
            j += 1
        paths.append(f"records[{i}].checkpoints[{j}].sensor.{field_path}")
        j += 1
    return paths


def reading_paths(record: Record, i: int, source: str, config: DerivationConfig) -> dict[str, str]:
    """Reading id -> where the reading is in records[i]: a sample by its
    position (sample_0042, named by sample_id_format), or the first checkpoint
    with that id."""
    if source == "samples":
        return {config.sample_id_format.format(index=j): f"records[{i}].sensor_samples[{j}]"
                for j in range(len(record.sensor_samples))}
    return {cid: f"records[{i}].checkpoints[{j}]" for cid, j in first_items(record, "checkpoints", config).items()}


def first_items(record: Record, scope: str, config: DerivationConfig) -> dict[str, int]:
    """Item id -> the index of the first item in record.<scope> with that id,
    named the way the derivations name it (utils.item_id)."""
    first = {}
    for j, item in enumerate(getattr(record, scope)):
        first.setdefault(item_id(scope, item, j, config), j)
    return first
