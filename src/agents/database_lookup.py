"""The database B-1's lookups query: eligibility by run, zone and checkpoint, in memory.

Built from eligibility_by_run, the view --eligibility-json writes, so a
reading here is one eligibility kept, and one it left out is here with its
reason instead of a value. Samples are left out: lookups are about the
checkpoints. Nothing is computed. Each row that holds a record value carries
`path`, where it sits in the extended record, in the form fill_slots takes.
Kept apart from the derivation database: a query on one never reads the other.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from agents.database_derivation import insert, open_saved
from agents.schema import DerivationConfig
from agents.utils import SENSOR_BLOCKS, format_timestamp

RUNS = """
-- One row per run, oldest first.
CREATE TABLE runs (
    run_id      TEXT PRIMARY KEY,
    run_order   INTEGER NOT NULL UNIQUE,     -- 0 = oldest
    is_latest   INTEGER NOT NULL,            -- 1 for the most recent run
    path        TEXT NOT NULL                -- records[i]
);
"""

ZONES = """
-- The zones of each run, in the order the route first reaches them.
CREATE TABLE zones (
    run_id      TEXT NOT NULL REFERENCES runs,
    zone        TEXT NOT NULL,
    position    INTEGER NOT NULL,            -- 0 = first zone on the route
    path        TEXT NOT NULL,               -- the zone's first checkpoint, records[i].checkpoints[j],
                                             -- whose zone field holds the zone's name
    PRIMARY KEY (run_id, zone)
);
"""

CHECKPOINTS = """
-- The checkpoints of each run, each under its zone, in route order. A
-- checkpoint visited twice is one row, for its first visit. status and
-- result_status are NULL when eligibility left them out; *_reason says why.
CREATE TABLE checkpoints (
    run_id               TEXT NOT NULL REFERENCES runs,
    zone                 TEXT NOT NULL,
    checkpoint_id        TEXT NOT NULL,
    checkpoint_name      TEXT,
    position             INTEGER NOT NULL,   -- route order in the run, 0 = first
    status               TEXT,               -- COMPLETED or MISSED: whether the robot got there
    status_reason        TEXT,
    result_status        TEXT,               -- PASS, FAIL or WARN: the verdict
    result_status_reason TEXT,               -- e.g. checkpoint status is MISSED
    path                 TEXT NOT NULL,      -- records[i].checkpoints[j]
    PRIMARY KEY (run_id, checkpoint_id),
    FOREIGN KEY (run_id, zone) REFERENCES zones
);
"""

READINGS = """
-- Each checkpoint's own sensor readings: one row per visit and field. A
-- field eligibility kept is 'included' with its value; one it left out is
-- 'excluded' with the reason and no value. A value is never both.
CREATE TABLE checkpoint_readings (
    run_id          TEXT NOT NULL REFERENCES runs,
    zone            TEXT NOT NULL,
    checkpoint_id   TEXT NOT NULL,
    block           TEXT NOT NULL,           -- accelerometer, environment or particulate
    field           TEXT NOT NULL,           -- e.g. temperature_c, accel_x, pm2_5
    status          TEXT NOT NULL,           -- included or excluded
    value           REAL,                    -- NULL when excluded
    reason          TEXT,                    -- NULL when included, e.g. sps30_ok=false
    count           INTEGER NOT NULL,        -- readings the row covers: 1 per visit
    timestamp       TEXT,                    -- when read; NULL when excluded
    stale           INTEGER,                 -- 1 when older than max_age_seconds; NULL when excluded
    path            TEXT NOT NULL,           -- where the field sits: records[i].checkpoints[j].sensor.<block>.<field>;
                                             -- the value itself when included, a value not used when excluded
    path_item       TEXT NOT NULL,           -- where the timestamp is: records[i].checkpoints[j].sensor,
                                             -- or the checkpoint when excluded
    FOREIGN KEY (run_id, checkpoint_id) REFERENCES checkpoints
);
"""

EVIDENCE = """
-- The evidence each checkpoint captured, one row per image path, as recorded.
CREATE TABLE evidence (
    run_id          TEXT NOT NULL REFERENCES runs,
    zone            TEXT NOT NULL,
    checkpoint_id   TEXT NOT NULL,
    kind            TEXT NOT NULL,           -- evidence_image, annotated_image or finding_image
    finding_id      TEXT,                    -- the finding the image belongs to; finding_image only
    image_path      TEXT NOT NULL,           -- as recorded, e.g. /run-files/<run>/evidence/<route>/<checkpoint>/<file>.jpg
    position        INTEGER NOT NULL,        -- order within the checkpoint and kind, 0 = first
    path            TEXT NOT NULL,           -- the image: records[i].checkpoints[j].evidence_images[k], ...
    FOREIGN KEY (run_id, checkpoint_id) REFERENCES checkpoints
);
"""

SCHEMA = RUNS + ZONES + CHECKPOINTS + READINGS + EVIDENCE

# The tables the lookup router can name, each one route. runs is not one: every
# route joins it for the run a row is about.
TABLES = ("zones", "checkpoints", "checkpoint_readings", "evidence")

# eligibility_by_run's evidence lists -> the kind their rows get.
EVIDENCE_KINDS = {
    "evidence_images": "evidence_image",
    "annotated_images": "annotated_image",
    "findings": "finding_image",
}

# The record fields on the checkpoints row, from eligibility's "checkpoints" block.
CHECKPOINT_FIELDS = ("status", "result_status")


def build_lookup_database(view: dict, config: DerivationConfig) -> sqlite3.Connection:
    """view is eligibility_by_run's output. config gives the timestamp format."""
    runs = view["runs"]
    if not runs:
        raise ValueError("no runs to build the lookup database from")
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    last = len(runs) - 1
    for order, (run_id, run) in enumerate(runs.items()):
        insert(conn, "runs", {"run_id": run_id, "run_order": order, "is_latest": int(order == last), "path": run["path"]})
        for z, (zone, zone_entry) in enumerate(run["zones"].items()):
            first = next(iter(zone_entry["checkpoints"].values()))
            insert(conn, "zones", {"run_id": run_id, "zone": zone, "position": z, "path": first["path"]})
            for cid, cp in zone_entry["checkpoints"].items():
                add_checkpoint(conn, run_id, zone, cid, cp)
                add_readings(conn, run_id, zone, cid, cp, config)
                add_evidence(conn, run_id, zone, cid, cp)
    conn.commit()
    # Built once, then read-only.
    conn.execute("PRAGMA query_only = ON")
    return conn


def add_checkpoint(conn: sqlite3.Connection, run_id: str, zone: str, cid: str, cp: dict) -> None:
    """The checkpoints row: status and result_status from the first visit, or why they were left out."""
    block = cp["checkpoint"].get("checkpoints", {"included": [], "excluded": []})
    first = block["included"][0] if block["included"] else {}
    row = {"run_id": run_id, "zone": zone, "checkpoint_id": cid, "checkpoint_name": cp["checkpoint_name"],
           "position": cp["position"], "path": cp["path"]}
    for name in CHECKPOINT_FIELDS:
        row[name] = first.get(name)
        row[f"{name}_reason"] = next((e["reason"] for e in block["excluded"] if name in e["fields"]), None)
    insert(conn, "checkpoints", row)


def add_readings(conn: sqlite3.Connection, run_id: str, zone: str, cid: str, cp: dict, config: DerivationConfig) -> None:
    """One checkpoint_readings row per kept field of each visit, and per field left out."""
    for block, entries in cp["checkpoint"].items():
        if block not in SENSOR_BLOCKS:
            continue   # the record fields, on the checkpoints row
        where = {"run_id": run_id, "zone": zone, "checkpoint_id": cid, "block": block}
        for reading in entries["included"]:
            if reading["path"] is None:
                continue   # a revisit no visit matched: nothing to cite
            for field in (f for f in reading if f not in ("source_id", "zone", "timestamp", "stale", "path")):
                insert(conn, "checkpoint_readings", {
                    **where, "field": field, "status": "included", "value": reading[field], "reason": None,
                    "count": 1, "timestamp": format_timestamp(reading["timestamp"], config),
                    "stale": int(reading["stale"]), "path": f"{reading['path']}.{field}",
                    "path_item": reading["path"].rsplit(".", 1)[0],
                })
        for excluded in entries["excluded"]:
            for field in excluded["fields"]:
                insert(conn, "checkpoint_readings", {
                    **where, "field": field, "status": "excluded", "value": None, "reason": excluded["reason"],
                    "count": excluded["count"], "timestamp": None, "stale": None,
                    "path": f"{cp['path']}.sensor.{block}.{field}", "path_item": cp["path"],
                })


def add_evidence(conn: sqlite3.Connection, run_id: str, zone: str, cid: str, cp: dict) -> None:
    """One evidence row per image path the checkpoint recorded."""
    for key, kind in EVIDENCE_KINDS.items():
        for position, item in enumerate(cp["evidence"][key]):
            insert(conn, "evidence", {
                "run_id": run_id, "zone": zone, "checkpoint_id": cid, "kind": kind,
                "finding_id": item.get("finding_id"), "image_path": item["image"],
                "position": position, "path": item["path"],
            })


def open_lookup_database(view: dict, config: DerivationConfig, path: Path) -> tuple[sqlite3.Connection, bool]:
    """The lookup database for view, in memory, and whether its saved copy was reused (see open_saved)."""
    return open_saved(path, lookup_fingerprint(view, config), lambda: build_lookup_database(view, config))


def lookup_fingerprint(view: dict, config: DerivationConfig) -> str:
    """sha256 of everything the lookup database is built from: the view, the
    configs (their hash) and this module's code."""
    text = json.dumps({
        "view": view,
        "config_hash": config.config_hash,
        "code": Path(__file__).read_text(encoding="utf-8"),
    }, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
