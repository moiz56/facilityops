# Facilityops-report

Turns one facility inspection run into a PDF report and a JSON manifest.

A robot drives a route, stops at each checkpoint, photographs what it sees,
reads its sensors, and writes everything to one JSON file. This tool
reads that file, finds the photos on disk, works out what is missing or
contradictory, and produces a document a facility manager can read — plus a
machine-readable manifest describing what the document contains.

The guiding rule is that the report never hides a problem. A missing photo, a
sensor that was offline, a count the robot got wrong: each of these is printed
as a gap rather than quietly smoothed over.

```
run.json  +  evidence photos   ->   report_<run_id>.pdf
                                    report_<run_id>.manifest.json
```

---

## Quick start

Python 3.11 or newer.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
```

Render a run:

```bash
python -m report.cli --record "data/20260728_144120-sis_facility_checkpoint_route/records/run.json"
```

The PDF and manifest land in `output/`. Logs go to stderr, so stdout stays
clean for piping.

If you would rather not install the package, run from the project root with
`PYTHONPATH=src` in front of every command.

> A virtualenv remembers its own absolute path. If you move or rename the
> project folder, the venv stops working and `pip` falls back to the system
> Python — on Debian/Ubuntu that shows up as
> `error: externally-managed-environment`. Delete `venv/` and make it again.

---

## The pipeline

Seven stages, run in order by `run_pipeline()` in `src/report/cli.py`:

```
load -> validate -> resolve_images -> detect_gaps -> derive -> render -> manifest
```

| Stage | What happens | Where |
| --- | --- | --- |
| **load** | Read the JSON file into Python objects. Nothing is corrected; problems are collected as "anomalies" attached to the record. | `common/loader.py` |
| **validate** | Log every anomaly the loader collected. Nothing is rejected — a bad record still produces a report that says what is wrong with it. | `report/cli.py` |
| **resolve_images** | Turn each recorded photo path into a real local file, check it opens, and pair each view's photos into a grid cell. | `common/paths.py`, `report/images.py` |
| **detect_gaps** | Decide everything that is missing, stale or contradictory. This is the only place such decisions are made. | `report/gaps.py` |
| **derive** | Work out every number the report prints: the reconciliation counts, per-zone telemetry, alert counts. | `report/derive.py` |
| **render** | Fill the Jinja2 templates, then hand the HTML to WeasyPrint to produce the PDF. | `report/render.py` + `templates/` |
| **manifest** | Write the JSON sidecar describing the PDF. | `report/manifest.py` |

Two design rules hold the whole thing together:

- **Decisions happen once.** Whether a sensor reading can be trusted is decided
  in `gaps.py`; whether a number is right is decided in `derive.py`. Templates
  only display what those two already worked out. That is why the PDF and the
  manifest can never disagree.
- **Formatting happens once.** How a missing value is worded ("Not recorded"),
  how many decimal places a number gets, what unit it carries — all of that
  lives in `render.py` and the config, not scattered through the templates.

---

## Input 1: the run record JSON

This is the file you point `--record` at. One file describes one run.

It must be a JSON **object** with a non-empty `run_id` string. Those two things
are hard requirements — without them the tool stops, because `run_id` is what
the output files are named after. Everything else is best-effort.

### How the loader treats your data

| Situation | What happens |
| --- | --- |
| Key absent, or `null`, or `""`, or `[]`, or `{}` | Treated as "not recorded". No complaint unless the field is required. |
| Value is the wrong type | Left unset, and an anomaly is recorded quoting the value. Nothing is converted or guessed. |
| A required field is absent | An anomaly is recorded saying so. The record still loads. |
| An integer where a decimal was expected | Widened to a float. `20` and `20.0` are the same reading written two ways. |
| `true`/`false` where a number was expected | Refused. In Python a bool *is* an int, and a flag written where a count belongs is a real mistake. |
| `confidence: 0.0` | Read as "no confidence score was produced", not as a score of zero. |
| Strings | Leading and trailing whitespace stripped. Spaces inside names survive. |
| File unreadable, not UTF-8, not JSON, not an object, or no `run_id` | This is the only case that aborts the run (`RecordParseError`, exit 1). |

Anomalies end up in `record.anomalies` and are printed to the log. The ones
caused by a wrong type also become `INCORRECT_DATATYPE` gaps in the report.

### Timestamps

Two formats, parsed separately. An unparseable timestamp becomes an anomaly and
the field is left empty — it never crashes the run.

| Where | Format | Example |
| --- | --- | --- |
| Most timestamps (`start_time`, `end_time`, checkpoint/finding/alert/sample/event `timestamp`, sensor `timestamp`) | ISO 8601 **with a UTC offset** | `2026-07-28T14:41:20-0700` |
| `received_at`, inside a sensor block and inside its `raw` block | `YYYY-MM-DD HH:MM:SS`, no timezone | `2026-07-28 14:41:26` |

The offset is mandatory on the first kind. Without one you get a "naive"
datetime, and mixing naive and offset-aware times raises an error the moment
alerts are sorted by time.

### Top level

```jsonc
{
  "run_id": "20260728_144120-sis_facility_checkpoint_route",  // required
  "facility_id": "sis_facility_checkpoint_route",
  "facility_name": "SIS Facility",
  "run_status": "COMPLETED",        // RUNNING | COMPLETED | ABORTED
  "final_status": "PASS",           // PASS | FAIL | WARN
  "start_time": "...",
  "end_time": "...",                // absent if the run never finished
  "duration": "00:14:32",           // text, as recorded — not recomputed
  "progress_percentage": 100,
  "locked": true,
  "current_checkpoint_id": null,    // only set mid-run

  // Seven declared counts: what the robot *claims*. Never corrected.
  "total_required_checkpoints": 8,
  "total_completed_checkpoints": 8,
  "passed_checkpoints": 7,
  "failed_checkpoints": 1,
  "missed_checkpoints": 0,
  "warned_checkpoints": 0,
  "finding_count": 1,

  // The arrays themselves.
  "checkpoints":      [ ... ],
  "findings":         [ ... ],
  "sensor_alerts":    [ ... ],
  "sensor_samples":   [ ... ],
  "event_log":        [ ... ],
  "live_detections":  [ ... ]
}
```

The seven counts are **claims, not facts**. The tool counts the arrays itself
and prints both figures side by side on the coverage page. Where a pair
disagrees it raises a `COUNT_MISMATCH` gap. Neither number is ever silently
preferred over the other.

### `checkpoints[]` — one stop on the route

```jsonc
{
  "checkpoint_id": "checkpoint_1",       // required; entry is dropped without it
  "checkpoint_name": "Loading Bay",
  "zone": "Bay A",
  "status": "COMPLETED",                 // COMPLETED | MISSED — did the robot get there?
  "result_status": "FAIL",               // PASS | FAIL | WARN — what was the verdict?
  "sequence_number": 1,
  "timestamp": "2026-07-28T14:41:20-0700",
  "missed_reason": "path blocked",       // set when status is MISSED
  "observed": "normal_scene",            // or "no_evidence", or a label
  "expected_text": "...",
  "notes": "free text, printed as written",
  "rule_type": "...",
  "confidence": 0.94,
  "coordinates": { "x": 1.2, "y": 3.4, "z": 0.0, "yaw": 1.57 },
  "evidence_images":  [ "/run-files/.../checkpoint_1_N.jpg", ... ],
  "annotated_images": [ ... ],
  "detections": [ /* same shape as a finding */ ],
  "sensor": { /* see below */ }
}
```

`status` and `result_status` are **independent**. `COMPLETED` + `FAIL` is a
normal outcome: the robot arrived and did not like what it saw. `COMPLETED`
with no evidence at all is different again from `MISSED`.

Duplicate `checkpoint_id` values are kept, not merged, and recorded as an
anomaly. Merging them would hide the fact that the route was recorded twice.

### `checkpoints[].sensor` — a checkpoint's sensor reading

```jsonc
{
  "ok": true,
  "status": "connected",            // anything else disqualifies the reading
  "source": "...",
  "sensor_hub_reachable": true,
  "age_seconds": 3.2,               // how stale the reading was when recorded
  "timestamp": "2026-07-28T14:41:20-0700",
  "received_at": "2026-07-28 14:41:26",

  "accelerometer": { "accel_x": 0.157, "accel_y": ..., "accel_z": ...,
                     "vibration_peak": ..., "vibration_rms_g": ... },
  "environment":   { "temperature_c": 30.81, "humidity_pct": ..., "pressure_hpa": ... },
  "particulate":   { "pm1_0": ..., "pm2_5": ..., "pm4_0": ..., "pm10": ... },

  "warnings": [ { "code": "...", "label": "...",
                  "description": "...", "severity": "..." } ],

  "raw": {
    "device": "...",
    "adxl345_ok": true,             // governs the accelerometer block
    "bme680_ok": true,              // governs the environment block
    "sps30_ok": false,              // governs the particulate block
    "air_status": "...",
    "vibration_status": "...",
    "gas_kohms": 12.3,
    "ts_ms": 1753738880000,
    "received_at": "2026-07-28 14:41:26"
    // any other keys here are kept but never read as measurements
  }
}
```

Two layers of trust, and both matter:

1. **The whole reading.** If `sensor` is absent, or `ok` is `false`, or
   `status` is anything but `"connected"`, or `sensor_hub_reachable` is
   `false` — nothing inside is printed. That is a `SENSOR_UNAVAILABLE` gap.
2. **One block at a time.** Each of the three `raw.*_ok` flags governs one
   measurement block. A `false` flag means that block holds placeholders, not
   measurements, so its numbers are never printed. That is a
   `SUBSYSTEM_OFFLINE` gap.

The values are still *read* when a flag is false — the decision about whether
they may be *shown* belongs to `gaps.py`, not to the loader. And a reading
older than `sensor.max_age_seconds` is still printed, but marked stale
(`STALE_READING`).

The `raw` block repeats the three structured blocks in flat form. Those flat
copies are kept under `raw.extra` so nothing is lost, but no measurement is
ever taken from them.

### `findings[]` — something the run found

The same shape appears in three places: the run's own `findings`, a
checkpoint's `detections`, and `live_detections`.

```jsonc
{
  "finding_id": "finding_1",      // required; entry is dropped without it
  "severity": "warning",          // info | warning | fail — lowercase here
  "status": "logged",             // logged | acknowledged | abstained
  "timestamp": "...",
  "checkpoint_id": "checkpoint_3",
  "run_id": "...",
  "zone": "...",
  "feature": "...",
  "description": "free text",
  "evidence_image": "/run-files/...",
  "coordinates": { "x": ..., "y": ..., "z": ..., "yaw": ... },
  "recommended_action": "free text"
}
```

An `abstained` finding is one the detector would not commit to. It is shown in
its own block as needing human review — never as a confirmed finding, and never
dropped.

### `sensor_alerts[]` — a threshold breach during the run

```jsonc
{
  "code": "VIBRATION_HIGH",       // required; entry is dropped without it
  "severity": "critical",         // critical | warning
  "timestamp": "...",
  "label": "...",
  "description": "...",
  "coordinates": { "x": ..., "y": ... },     // x and y only — no z, no yaw
  "nearest_checkpoint_id": "checkpoint_4",
  "nearest_checkpoint_name": "...",
  "zone": "..."
}
```

`nearest_checkpoint_id` is **proximity, not blame**. An alert near checkpoint 4
was not necessarily caused by anything at checkpoint 4. The report always
labels that column "Nearest checkpoint" and says what it means.

### `sensor_samples[]` — continuous telemetry

Roughly one every couple of seconds. Individual samples are never printed; they
are only rolled up per zone into min/mean/max.

```jsonc
{
  "timestamp": "...",
  "status": "...",
  "coordinates": { "x": ..., "y": ... },
  "nearest_checkpoint_id": "...",
  "nearest_checkpoint_name": "...",
  "zone": "Bay A",
  "sensor": {
    "accelerometer": { ... },
    "environment":   { ... },
    "particulate":   { ... }
  }
}
```

A sample's `sensor` is deliberately a **different shape** from a checkpoint's:
no `ok`, no `age_seconds`, no `raw`, no device flags. Since a sample carries no
flags of its own, whether its numbers may be shown is decided by the flags on
the checkpoints in the same zone.

Samples are never dropped, even if malformed — they are only ever aggregated.

### `event_log[]` — the run's own log

```jsonc
{
  "event_id": "evt_1",            // required
  "event_type": "checkpoint_completed",   // run_started | checkpoint_completed | run_completed
  "timestamp": "...",
  "message": "...",
  "status": "...",
  "result_status": "...",
  "checkpoint_id": "checkpoint_1",
  "checkpoint_name": "...",
  "evidence_count": 11
}
```

Run-level events (`run_started`, `run_completed`) simply omit the four
checkpoint fields. That is normal and is not recorded as a problem.

`evidence_count` is a **claim**, checked against the checkpoint's actual
`evidence_images`. A mismatch is reported as a `COUNT_MISMATCH` gap against the
checkpoint the event names.

### Which entries get dropped

An entry is discarded only when it has no identity at all:

| Array | Dropped when it has no… |
| --- | --- |
| `checkpoints` | `checkpoint_id` |
| `findings`, `detections`, `live_detections` | `finding_id` |
| `sensor_alerts` | `code` |
| `event_log` | `event_id` or `event_type` |
| `sensor_samples` | *never dropped* |

Every drop is recorded as an anomaly, so the report can say the entry existed.

---

## Input 2: the evidence folder

The record does not contain photos — it contains paths as the robot saw them,
which start with a prefix that does not exist on your machine:

```
/run-files/20260728_144120-sis_facility_checkpoint_route/evidence/SIS Facility Checkpoint Route/checkpoint_1/checkpoint_1_N.jpg
```

### The rewrite rule

1. Strip `paths.path_prefix` (default `/run-files`) from the front.
2. Strip any remaining leading `/`.
3. Join what is left to `--evidence-root` (default: the project's `data/`).

So the path above is looked for at:

```
data/20260728_144120-sis_facility_checkpoint_route/evidence/SIS Facility Checkpoint Route/checkpoint_1/checkpoint_1_N.jpg
```

A recorded path that does not start with the prefix is used as-is, relative to
the evidence root.

### The layout this implies

Nothing in the code hardcodes this shape — it falls out of whatever paths the
record carries. But for records written with the `/run-files/<run_id>/...`
convention, the tree under the evidence root looks like:

```
data/                                        <- --evidence-root points here
  <run_id>/
    records/
      run.json                               <- --record points here
    evidence/
      <Route Name>/                          <- often contains spaces
        <checkpoint_id>/
          <checkpoint_id>_<view>.jpg         <- or _<view>_rgb.jpg
          <checkpoint_id>_<view>_thermal.jpg <- optional thermal counterpart
```

Three real shapes, all handled:

```
checkpoint_1/   checkpoint_1_N.jpg    checkpoint_1_N_thermal.jpg   ... 8 compass views
ac_2/           ac_2_h000_rgb.jpg     ac_2_h000_thermal.jpg        ... 3 heading views
a4_back/        a4_back_rgb.jpg       a4_back_thermal.jpg          ... 1 unlabelled view
```

Files the record does not list — `.npy` thermal arrays, for instance — are
never touched. The engine resolves only the paths `evidence_images` names and
never scans a directory.

Each run keeps its record and its photos together. `--record` and
`--evidence-root` are separate arguments, so the photos can live anywhere:

```bash
python -m report.cli \
  --record "data/<run_id>/records/run.json" \
  --evidence-root "/mnt/handover/SIS Facility Route"
```

### How a filename is read

A filename is `<checkpoint_id>[_<view>][_<modality>].<ext>`. It is read in that
order, from the back:

1. Strip the modality marker — `_thermal` (`evidence.thermal_suffix`) means
   thermal, `_rgb` (`evidence.rgb_suffix`) means RGB. **No marker also means
   RGB**, since some routes mark it and some don't.
2. Strip the `checkpoint_id`.
3. Whatever is left is the **view label**, taken exactly as recorded.

No vocabulary is imposed. The engine does not care whether a route names its
views after compass points, camera headings, or nothing at all:

| Filename | Checkpoint | View | Thermal? |
| --- | --- | --- | --- |
| `checkpoint_1_N.jpg` | `checkpoint_1` | `N` | no |
| `checkpoint_1_NE_thermal.jpg` | `checkpoint_1` | `NE` | yes |
| `ac_2_h120_rgb.jpg` | `ac_2` | `h120` | no |
| `a4_back_rgb.jpg` | `a4_back` | *none* | no |
| `a4_back_thermal.jpg` | `a4_back` | *none* | yes |

Both modalities of one view come back with the same label, and that is what
pairs them into a single cell. Pairing ignores case, so `checkpoint_1_n.jpg`
shares a cell with `checkpoint_1_N_thermal.jpg` rather than opening a second
cell and a spurious `MISSING_THERMAL`; the label prints as the first file
spelled it.

### What "resolved" means

For each recorded path the tool records: the original URI, the local path it
rewrote to, the view label, whether it is thermal, whether the file exists,
whether it actually opens as an image, and — if not — the reason why. Four
outcomes, each producing a different cell in the report:

- file is there and decodes → the photo is embedded
- no file at that path → `MISSING_IMAGE`, "no file at …"
- file is zero bytes → `MISSING_IMAGE`, "file is empty"
- file will not decode → `MISSING_IMAGE`, "file is not a readable image"

### The evidence grid

One cell per view the checkpoint recorded, in the order it recorded them. A
cell holds the RGB with its thermal counterpart beneath it, so a pair draws as
one cell rather than two.

The grid fans out to at most `evidence.grid_columns` (4) cells per row, and
uses fewer when the checkpoint recorded fewer:

```
1 view      2 views     3 views     4 views     7 views
[      ]    [  ][  ]    [ ][ ][ ]   [][][][]    [][][][]
                                                [][][]
```

Every cell in one checkpoint is the same size; only how many there are changes.

**Nothing is expected and nothing is invented.** Without a fixed vocabulary
there is no way to know a view was meant to exist, so a view nobody
photographed has no cell rather than an empty placeholder. What *was* captured
is still checked against what the record claimed — an empty `evidence_images`
is `NO_EVIDENCE`, a path that does not resolve is `MISSING_IMAGE`, a
disagreeing `evidence_count` is `COUNT_MISMATCH`, and an RGB with no thermal
beside it is `MISSING_THERMAL`.

If two photos claim the same view and modality, the first one wins.

Before embedding, any image longer than `report.max_image_dimension` on its
longest edge is copied down to that size into a temporary directory. The
temporary copies are deleted when the run ends — `output/` only ever holds the
PDF and the manifest.

---

## Outputs

Both are written to `--output-dir` (default `output/`) and named after the run.

### `report_<run_id>.pdf`

Sections in fixed order. Each can be switched off in config, except the
contents page, which is front matter:

| # | Section | What it shows |
| --- | --- | --- |
| 1 | Cover | Facility, run id, times, duration, final status badge, headline counts |
| — | Contents | Section list with real, measured page numbers |
| 2 | Coverage and reconciliation | Declared vs computed counts side by side; missed checkpoints with reasons; checkpoints with no evidence |
| — | All gaps | Every gap in the run, in one table |
| 3 | Findings | All run-level findings, grouped: confirmed, needing review, other |
| 4 | Sensor alerts | All alerts, grouped by severity (worst first), in time order within each group |
| 5 | Zone telemetry | One row per zone: sample count, min/mean/max of temperature, humidity and vibration RMS, alert count |
| 6 | Checkpoints | One section each: badges, detail table, evidence grid, sensor table, warnings, findings, notes — grouped by zone |
| 7 | Run summary | One row per checkpoint across the whole run |

Every page carries a running head (facility name, run id) and a footer with the
engine version, template version, generation time and run id.

**Page numbers are measured, not estimated.** The document is laid out, the
page each section landed on is read back, and it is laid out again with those
numbers filled in. A third pass settles the rare case where filling the numbers
in pushed something over a page boundary. Usually it takes two passes.

### `report_<run_id>.manifest.json`

```jsonc
{
  "manifest_version": "1.0",
  "generated_at": "2026-09-16T06:41:29Z",
  "source_run_id": "20260728_144120-sis_facility_checkpoint_route",
  "facility_id": "sis_facility_checkpoint_route",
  "engine_version": "0.1.0",
  "template_version": "full_report_v1",
  "config_hash": "sha256:d0614b12...",
  "report_file": "report_<run_id>.pdf",

  "page_count": 23,
  "sections": [
    { "id": "cover",    "page_start": 1, "page_end": 1 },
    { "id": "coverage", "page_start": 3, "page_end": 5 },
    ...
  ],

  "checkpoints_rendered": 8,
  "checkpoints_with_gaps": 8,     // distinct checkpoints with ≥1 gap, not a gap count
  "images_referenced": 87,
  "images_rendered": 86,

  "gaps": [
    { "item_id": "home_docking_station",
      "gap_type": "NO_EVIDENCE",
      "detail": "evidence_images empty; observed = no_evidence" }
  ],

  "counts": {
    "declared": { "required": 8, "completed": 8, "passed": 7, ... },
    "computed": { "required": 8, "completed": 8, "passed": 7, ... }
  },
  "alerts": { "critical": 8, "warning": 12 }
}
```

`sections` only lists sections that were switched on. `config_hash` is a
SHA-256 of the config **as applied** — the parsed values, sorted — so
reordering keys or editing a comment does not change it, but changing a value
does.

---

## Gaps

A gap is one thing missing, stale, or contradictory. Every gap in the PDF and
every gap in the manifest comes from the same place, so the two can never
disagree. Each gap carries an `item_id` (a `checkpoint_id`, or `__run__` for
the run itself), a type, and a sentence naming what is actually wrong.

| Type | Raised when |
| --- | --- |
| `MISSING_IMAGE` | A listed photo does not resolve, is empty, or will not decode. One gap per broken file. |
| `MISSING_THERMAL` | A view has an RGB photo with no thermal counterpart. |
| `NO_EVIDENCE` | `evidence_images` is empty, or `observed` is `"no_evidence"`. Both causes are listed if both apply. |
| `MISSED_CHECKPOINT` | `status` is `MISSED`. Carries the recorded reason. |
| `SENSOR_UNAVAILABLE` | The whole sensor reading is disqualified (see the two trust layers above). |
| `SUBSYSTEM_OFFLINE` | A `raw.*_ok` flag is false, so that one block holds placeholders. |
| `STALE_READING` | `age_seconds` exceeds `sensor.max_age_seconds`. Values still print, marked stale. |
| `NO_FINDINGS` | No finding references this checkpoint. |
| `COUNT_MISMATCH` | A declared count disagrees with the data it describes — including an event's `evidence_count`. |
| `EMPTY_RECORD` | The `checkpoints` array is empty. |
| `INCORRECT_DATATYPE` | A field arrived as the wrong type and was refused. |

---

## What each file does

```
config/
  report.yaml        all settings — see "What you can tweak"
  assets/logo.png    cover logo

data/                local run data, git-ignored; created on demand
output/              the PDF and manifest; created on demand

src/common/          shared layer — imports nothing from src/report
  __init__.py        project paths, ensure_runtime_dirs(), logging setup
  schema.py          the dataclasses one run is read into; the data contract
  loader.py          JSON -> Record, collecting anomalies instead of raising
  paths.py           config loading, and evidence path rewriting/checking
  provenance.py      the four version facts stamped on every report

src/report/          the report application
  cli.py             argument parsing, run_pipeline(), logging, exit codes
  images.py          the evidence grid, and sizing images down
  gaps.py            every gap rule, one function each
  derive.py          every number the report prints
  render.py          formatting, view-building, HTML, and the PDF
  manifest.py        the JSON sidecar, and its config hash
  templates/         Jinja2 partials and the stylesheets

tests/
  test_acceptance.py the 30 acceptance tests of section 7
  test_data/         one run per test group: record plus evidence tree
  output/            what a run rendered - generated, git-ignored
decisions.md         design decisions and open questions for the client
licences.md          dependency licences
```

### `src/common` — the shared layer

**`schema.py`** — frozen dataclasses describing one run in memory. This is the
data contract. The nesting:

```
Record
 |- checkpoints[]      Checkpoint -> Pose, Finding[], SensorBlock
 |                                     SensorBlock -> Accelerometer, Environment,
 |                                                    Particulate, SensorWarning[], RawSensor
 |- findings[]         Finding -> Pose
 |- sensor_alerts[]    SensorAlert -> Point2D
 |- sensor_samples[]   SensorSample -> Point2D, SampleSensor
 |- event_log[]        Event
 |- live_detections[]  Finding
 `- anomalies[]        FieldAnomaly       (added by the engine, not read from the file)
```

Two things are worth a second look. `Pose` (x, y, z, yaw) and `Point2D` (x, y)
are separate types because alerts and samples record two keys where checkpoints
record four, and the missing two are not invented. And `SensorBlock` and
`SampleSensor` are separate for the same reason — only the checkpoint's block
carries the flags that say whether to trust it.

**`loader.py`** — reads the JSON. Every builder function takes the same two
extra arguments: who an anomaly belongs to, and the one list the whole record's
anomalies accumulate into. Nothing is raised on the way up; problems are
carried, not thrown.

**`paths.py`** — two jobs. It loads and validates `config/report.yaml`
(`load_config`, `setting`, `units`, `decimals`), and it does the evidence path
rewriting (`parse_filename`, `resolve_evidence`, `resolve_checkpoint_images`,
`resolve_record_images`). `resolve_evidence` never raises — a path that does not
resolve comes back carrying the reason.

**`provenance.py`** — one `stamp()` call per run produces the engine version,
template version, generation time and run id. The page footer and the manifest
both read that single stamp, so they cannot disagree about when a report was
made.

### `src/report` — the application

**`cli.py`** — the command line, `run_pipeline()`, the summary logging, and the
`--dump-*` outputs.

**`images.py`** — `build_view_grid()` pairs each view's photos into a cell;
`resize_to_fit()` and `prepare_image()` size an image down before embedding.
Resizing never raises: an image PIL refuses to handle is embedded at its
recorded size and a warning is logged, because a large page is a better failure
than no page.

**`gaps.py`** — one function per gap rule, plus `detect_gaps()` which runs them
in route order and then the run-level ones. `unavailable_reason()` is the shared
helper the sensor rules ask before looking inside a reading.

**`derive.py`** — `declared_counts()` and `computed_counts()` are the two halves
of the coverage page. `zone_rows()` builds the telemetry table, including a row
for zones that recorded nothing and a final row for samples that named no zone
at all. A stat that could not be worked out carries *why* — no samples, device
offline, or samples without that value — because a blank cell cannot tell those
three apart.

**`render.py`** — the largest module, and the only place formatting happens. It
holds the "Not recorded" wording, number formatting, badge styles, the view
objects each template reads, and the HTML→PDF step. Autoescaping is on for every
template, so free text containing `<script>` or Jinja syntax prints as written.

**`manifest.py`** — builds and writes the manifest, and hashes the config.

### `templates/`

| File | Renders |
| --- | --- |
| `full_report.html.j2` | The shell; includes each partial when config turns it on |
| `_cover.html.j2` | Cover page |
| `_contents.html.j2` | Contents page with measured page numbers |
| `_coverage.html.j2` | Declared vs computed counts, missed checkpoints, no-evidence list |
| `_findings.html.j2` | Findings summary table |
| `_alerts.html.j2` | Alerts grouped by severity |
| `_zone.html.j2` | Zone telemetry summary |
| `_checkpoint.html.j2` | One checkpoint section, and the zone group headers |
| `_summary.html.j2` | Run summary table |
| `styles.css` | The current stylesheet |
| `style_default.css` | The earlier stylesheet, kept for comparison |

All page-break control lives in the stylesheet, nowhere else.

---

## What you can tweak

Everything is in `config/report.yaml`.

### `report`

| Key | Default | Effect |
| --- | --- | --- |
| `type` | `full` | Currently not read by anything. |
| `page_size` | `A4` | Goes straight into the CSS `@page` rule. |
| `max_image_dimension` | `1200` | Longest edge in pixels before an image is copied down. Raising it makes sharper, much larger PDFs. |

### `paths`

| Key | Default | Effect |
| --- | --- | --- |
| `path_prefix` | `/run-files` | Stripped from recorded evidence paths before they are joined to the evidence root. Change this when records come from a robot with a different mount point. |

### `evidence`

| Key | Default | Effect |
| --- | --- | --- |
| `grid_columns` | `4` | The *most* cells on a row. A checkpoint with fewer views uses fewer. |
| `thermal_suffix` | `_thermal` | Filename marker for a thermal image. |
| `rgb_suffix` | `_rgb` | Filename marker for an RGB image. A file with neither marker is RGB too. |

### `sensor`

| Key | Default | Effect |
| --- | --- | --- |
| `max_age_seconds` | `120` | Above this, a reading renders but is flagged stale. |
| `subsystem_flags` | `adxl345_ok: accelerometer`, `bme680_ok: environment`, `sps30_ok: particulate` | Maps each device health flag to the measurement block it governs. The block names must match the field names in the sensor blocks. |

### `units` and `decimals`

`units` maps a measurement field to the unit printed beside it. It currently
ships **empty**, so figures print bare — no unit is invented for a field the
record does not label. Fill it in when the instrument units are confirmed:

```yaml
units:
  temperature_c: "°C"
  humidity_pct: "%"
```

`decimals` maps a field to a fixed number of decimal places. A field with no
entry falls back to 4 significant figures. Fixed places are what keeps a column
lined up; the trade-off is that a very small value prints as `0.000` rather
than in scientific notation.

### `sections`

Seven booleans: `cover`, `coverage`, `findings`, `alerts`, `zone_telemetry`,
`checkpoints`, `summary`. Set one to `false` and the section is left out of the
PDF and out of the manifest's `sections` array.

All seven keys are **required**. A missing one is an error naming the key, not
a silent `false` — dropping a section out of a report without saying so is
exactly the failure this tool exists to avoid.

### `branding`

| Key | Default | Effect |
| --- | --- | --- |
| `stylesheet` | `styles.css` | Which stylesheet in `templates/` to lay the report out with. `style_default.css` is the earlier design. Must be a bare filename in that folder. |
| `logo_path` | `config/assets/logo.png` | Relative to the project root. A missing file is logged and the report renders without it. |
| `primary_colour` | `#1F3864` | Injected as the `--primary-colour` CSS variable. |
| `footer_text` | `Prepared by FacilityOps.AI` | Opens the footer line on every page. |

### `provenance` and `manifest`

`engine_version`, `template_version` and `manifest.version` are stamped into
every report and manifest. Bump them when the engine or the template shape
changes.

### One thing to know about `--config`

Seven settings are read from `config/report.yaml` **when the modules are first
imported**, not from the file you pass to `--config`:

- `evidence.thermal_suffix`, `evidence.rgb_suffix`, `evidence.grid_columns`
- `paths.path_prefix`
- `report.max_image_dimension`
- `sensor.max_age_seconds`, `sensor.subsystem_flags`

Everything else — the sections, the branding, units, decimals, page size,
versions, and the config hash — comes from the file `--config` names. So
`--config` is useful for swapping branding or turning sections off, but to
change path rewriting or the filename markers, edit `config/report.yaml` itself.

---

## Command line

```
python -m report.cli --record PATH [options]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--record PATH` | *required* | The run record JSON to render |
| `--evidence-root PATH` | `data/` | Root that recorded evidence paths are rewritten against |
| `--config PATH` | `config/report.yaml` | Report configuration |
| `--output-dir PATH` | `output/` | Where the PDF and manifest go |
| `--dump-record [PATH]` | | Write the parsed record as JSON; no PATH, or `-`, means stdout |
| `--dump-manifest [PATH]` | | Write the manifest as JSON |
| `--dump-artifacts [PATH]` | | Write the resolved images and grids as JSON |
| `-v`, `--verbose` | | Log at debug level |

Only `--record` is resolved against your current directory. The other three
default to absolute paths derived from the package location, so they point at
the project wherever you happen to be standing.

| Exit code | Meaning |
| --- | --- |
| `0` | Finished |
| `1` | Failed for a reported reason — unreadable record, or a config missing a required setting |
| `2` | The command line was wrong |

### Examples

```bash
# Render a run
python -m report.cli --record "data/<run_id>/records/run.json"

# See every anomaly and parse decision
python -m report.cli --record "data/<run_id>/records/run.json" -v

# See what the loader made of the file
python -m report.cli --record "data/<run_id>/records/run.json" --dump-record output/parsed.json

# Pipe the anomalies out — logs go to stderr, so this stays clean
python -m report.cli --record "data/<run_id>/records/run.json" --dump-record | jq .anomalies

# Inspect the resolved images and the eight-cell grids
python -m report.cli --record "data/<run_id>/records/run.json" --dump-artifacts -

# Photos somewhere else, output somewhere else
python -m report.cli \
    --record "data/<run_id>/records/run.json" \
    --evidence-root "/mnt/handover/SIS Facility Route" \
    --output-dir out/
```

### Paths with spaces

Route directory names contain spaces, and so may the paths you pass in. **Quote
every path argument.** No path is ever passed through a shell, split on
whitespace, or interpolated into a command — each is a `pathlib.Path` from
argparse onwards.

### Using it as a library

```python
from pathlib import Path
from report.cli import run_pipeline

artifacts = run_pipeline(
    Path("data/<run_id>/records/run.json"),
    Path("data"),
    Path("output"),
)

artifacts.record                      # the parsed Record
artifacts.images["checkpoint_1"]      # that checkpoint's resolved images
artifacts.grids["checkpoint_1"]       # its ViewCells, in recorded order
artifacts.gaps                        # every gap found
artifacts.derived                     # every number the report prints
artifacts.pdf_path                    # where the PDF was written
artifacts.manifest                    # the manifest, as a dict
```

---

## Tests

```bash
pytest
```

One command runs everything. Collection is limited to `tests/` by
`pyproject.toml`, and everything lives in `tests/test_acceptance.py`.

### What the suite is

The tests are section 7 of the brief — the 30 acceptance tests the client runs
on their own machine, from the handover, to decide whether the milestone is
accepted. **One test per id**, so `pytest -v` reads as the results table
deliverable 10 asks for and a failure names the id that failed:

```
tests/test_acceptance.py::test_acceptance[TA-01] PASSED
tests/test_acceptance.py::test_acceptance[TA-02] PASSED
tests/test_acceptance.py::test_acceptance[TA-06] FAILED
...
28 passed, 1 skipped, 1 failed in 92s
```

Each id has a `check_taNN(artifacts)` function carrying that id's pass
condition verbatim in its docstring, so a test can be held against the brief
without translating. The `ACCEPTANCE` table names, for every id, the run it is
asked of and the check it is.

Run one id while shaping a fixture for it:

```bash
pytest -k TA-13 -v
pytest "tests/test_acceptance.py::test_acceptance[TA-13]"
```

**Each run is rendered once, not once per id.** Ids share a run wherever their
inputs do not conflict — one record can carry a checkpoint the robot never
reached, one whose sensor hub was degraded and one with a confidence recorded
as a string, all at once. The `rendered` fixture is session-scoped and caches
by run, so the six sensor-trust ids ask six questions of one document. That is
the difference between 90 seconds and ten minutes; rendering is nearly all the
runtime.

**TA-08 is listed and skipped.** It is "fresh Linux container, following your
README only" — a thing to do rather than a thing to assert. It is in the table
with its reason so the suite accounts for all thirty rather than quietly
covering twenty-nine.

**TA-26 has no rendered document**, which is the whole of that test: the run
stops at the loader, and the check takes the paths instead of an `Artifacts`.

### Fixtures

`tests/test_data/` holds one directory per group, each a whole run — the record
and the evidence tree its recorded paths point at:

```
tests/test_data/
  TA01-TA05/   TA06/   TA07/   TA08/   TA09-TA14/   TA15-TA22/
  TA23-TA29/   TA26/   TA30/
```

The directory is also the evidence root: a run directory sits directly under
it, so stripping `paths.path_prefix` off a recorded path leaves
`<run_id>/evidence/…`. Three of the groups in 7.4 cannot share a run — TA-26's
file does not parse, and TA-30 has no checkpoints, which is the one thing the
other six need.

The tests find their subject by the condition the brief states rather than by
naming a checkpoint, so they hold for whatever record supplies them:

```python
missed = [c for c in artifacts.record.checkpoints if c.status == "MISSED"]
assert missed, "needs a checkpoint with status MISSED"
```

A fixture that is not yet shaped therefore fails with a sentence saying what it
needs, not with a puzzle.

### Reading what a test rendered

Every run leaves its PDF and manifest under `tests/output/`, named after the
fixture directory it came from, so a run can be read rather than only passed or
failed:

```bash
xdg-open tests/output/TA23-TA29/*.pdf
```

A directory per *run* rather than per test, because several ids share one
document and output files are named after the `run_id` — a directory per test
would have the last id to run overwrite the others. Each is emptied as it is
rendered, so what is in there afterwards is what this run produced.

`tests/output/` is generated and git-ignored. The project's own `output/` is
untouched — that belongs to the CLI.

### What the tests read a PDF with

`pypdf`, in process, rather than the `pdftotext` binary: the client runs this
suite on their machine, and TA-08 is a fresh container following the README
only, so `pip install -e ".[dev]"` has to be enough.

A PDF is glyphs at positions, not lines, so extraction breaks text wherever the
layout did. Three helpers deal with that, and which one a test uses is a real
decision:

- `flat(text)` collapses whitespace to single spaces — for prose, where a
  sentence wrapped inside a narrow table cell must still match
- `squashed(text)` removes whitespace entirely — for identifiers, where a
  `finding_id` broken mid-word comes back as `…:general_c ondition`
- `page_holding(pages, marker)` returns the one page carrying a marker, so a
  test can say "this checkpoint's page says that" rather than "the document
  says it somewhere"

---

## Dependencies

Pinned exactly in `pyproject.toml`:

| Package | Version | For |
| --- | --- | --- |
| `jinja2` | 3.1.4 | Templates |
| `weasyprint` | 62.3 | HTML → PDF |
| `pydyf` | 0.10.0 | Pinned explicitly — WeasyPrint 62.3 calls an API that pydyf 0.11 changed, and an unpinned install breaks at `write_pdf` |
| `pyyaml` | 6.0.2 | Config |
| `pillow` | 10.4.0 | Image checking and resizing |
| `pytest` | 8.3.3 | Tests (dev extra) |
| `pypdf` | 6.18.1 | Reading text and embedded images back out of a rendered PDF (dev extra) |

WeasyPrint needs system libraries (pango, cairo, gdk-pixbuf). On Debian/Ubuntu:

```bash
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0
```

---

## Notes and known edges

- **`data/` and `output/` are created on demand** by `ensure_runtime_dirs()`.
  No `.gitkeep` files needed.
- **Supplied runs arrived with inconsistent layouts** — `Evidence/` against
  `evidence/`, `Record/` against `Records/`, one record at the top level, one
  evidence tree nested twice under its run id. They were normalised to the
  lowercase form the records themselves reference, so the rewrite rule resolves
  against `data/` with `path_prefix: /run-files` unchanged.
- **`MISSING_THERMAL` fires whether or not the RGB photo itself resolves.** A
  zero-byte RGB therefore reports both `MISSING_IMAGE` and `MISSING_THERMAL`.
  Read the other way, one broken file would suppress a second, unrelated gap.
- **`warned_checkpoints` is cross-checked against sensor warnings**, not only
  against `result_status: WARN`. Without that the reference run reconciled
  cleanly while seven of its eight checkpoints carried warnings — the
  contradiction 2.9 names by hand. Decision 176.
- **A run with no checkpoints says so in the checkpoints section**, not only on
  the cover and the coverage page. Decision 177.
- **The compass grid is gone but not deleted.** The old `parse_filename`, the
  fixed-width grid builder and the `DIRECTIONS` constant are kept verbatim in
  comments beside their replacements, so that behaviour can be restored. See
  decisions 174 and 175.
- **One supplied racks run has no thermal images at all**, which raises 33
  `MISSING_THERMAL` gaps. Whether that route has no thermal camera or that run
  lost its capture is open — question 35 in `decisions.md`.
- **The `units` block ships empty**, so the report prints bare figures. See
  `decisions.md` for which units are safe to infer and which need confirming.
- **`decisions.md`** records every design decision and every open question for
  the client. Read it before changing behaviour — several things that look like
  bugs are deliberate, with the reasoning written down.
