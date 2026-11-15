# The Sealed, Comparable Record (Milestone 4)

Milestone 4 turns a run's report into a record you can trust and compare. It
adds four things on top of the report engine (Milestone 1), the agents
(Milestone 2) and the query surface (Milestone 3):

| # | Feature | In one line | Code |
|---|---|---|---|
| 1 | Seal and verify | Seals a run so any changed value, swapped image or deleted record is caught and named | `src/seal/` |
| 2 | Comparison with the previous run | What differs between two runs of a route, stated without judging it | `src/compare/rounds.py` |
| 3 | Record continuity | How much of the intended record exists for a route, as one figure | `src/compare/continuity.py` |
| 4 | Condition evidence | A run's findings set against the clauses of a standard, never certifying compliance | `src/standards/` |

One rule runs through all four: **an honest gap is better than a confident
wrong answer.** A seal says "could not verify" rather than guess. A
comparison says UNATTRIBUTED rather than invent a reason. A clause with no
findings says "not assessed", never "passed".

---

## Contents

1. [Quick start](#1-quick-start)
2. [Every command, and what it runs](#2-every-command-and-what-it-runs)
3. [How the pieces fit together](#3-how-the-pieces-fit-together)
4. [Sealing: how a seal works](#4-sealing-how-a-seal-works)
5. [The canonical form](#5-the-canonical-form)
6. [Verifying: the standalone tool](#6-verifying-the-standalone-tool)
7. [The chain](#7-the-chain)
8. [Comparison with the previous run](#8-comparison-with-the-previous-run)
9. [Record continuity](#9-record-continuity)
10. [Condition evidence (standards packs)](#10-condition-evidence-standards-packs)
11. [On and off switches](#11-on-and-off-switches)
12. [What the seal cannot catch](#12-what-the-seal-cannot-catch)
13. [Assumptions made](#13-assumptions-made)
14. [Tests](#14-tests)
15. [Open items](#15-open-items)

---

## 1. Quick start

### 1.1 Fresh setup (Ubuntu)

For someone who has just pulled the repo.

**1. System packages.** Python 3.11 or newer, and the libraries WeasyPrint
needs to write the report PDFs (pango, cairo, gdk-pixbuf):

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip git
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 fonts-liberation
python3 --version    # must be 3.11 or newer
```

Ubuntu 22.04 ships Python 3.10. On 22.04, install 3.11 first
(`sudo apt install python3.11 python3.11-venv`) and use `python3.11` in step 2.

**2. Clone and install.**

```bash
git clone <repo-url> facilityops-report
cd facilityops-report
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
```

This installs every Python library, pytest included, and creates the
`facilityops-verify` command. Run `pip install -e ".[dev]"` again whenever
`pyproject.toml` changes, or the command will be missing or out of date.
Check it with:

```bash
facilityops-verify --help
```

**3. The API key (optional for this milestone).** `.env` is not in git.
Sealing renders each run's report, and the report's B-2 and B-3 sections call
the model when they are on. With a key, they write their real text. Without
one, they fall back and the report still renders. To add one:

```bash
echo "ANTHROPIC_API_KEY=your-key-here" > .env
```

The key must match `provider.name` in `config/agents.yaml` (`claude` now).
For Gemini it is `GEMINI_API_KEY`.

**4. The tenant data.** `data_tenant/` is not in git. Get a copy and put it in
the project root, laid out as `data_tenant/<facility_id>/<run_id>/run.json`,
with each run's `evidence/` folder beside its `run.json`. Every M4 command
reads from it.

**5. The tamper test data.** `data_tenant_test/` is not in git either. It is
built from `data_tenant`, so do step 4 first:

```bash
PYTHONPATH=src python tests/seal/build_data_tenant_test.py
```

Only the tests need it ([14](#14-tests)).

**6. Seal a route and check it.**

```bash
PYTHONPATH=src python -m seal.cli seal data_tenant/stretch3_checkpoint_route
facilityops-verify --chain output/sealed/stretch3_checkpoint_route
```

The second command should end with `RESULT: INTACT`.

The router's encoder model, which Milestone 3 downloads on first use, is not
needed for anything in this milestone.

### 1.2 Already in git, so nothing to do

- all config: `report.yaml` with the M4 switches, `tenants.yaml` with
  `runs_per_day` and `standards_packs`, and `config/standards/` with the
  placeholder pack
- the tests and the brief's example pack they use (`tests/standards/packs/`)
- `output/`, `logs/` and `tests/output/`, which are made on first use

Keep `PYTHONPATH=src` on every `python -m` command, even after `pip install`,
the same as in Milestone 3. The package settings in `pyproject.toml` list only
`src/common`, `src/report` and `src/seal`, so `compare`, `standards`, `agents`
and `query` are found through `PYTHONPATH`. `facilityops-verify` is the one
command that doesn't need it.

### 1.3 Every time after that

Run everything from the project root, with the virtual environment active:

```bash
cd facilityops-report
source venv/bin/activate
export PYTHONPATH=src
```

### 1.4 The usual order

**Seal, then verify, then look at continuity and the reports.** Continuity
counts sealed runs, so it has nothing to count until something is sealed.

```bash
# 1. Seal one route. Renders each run's report and seals it, in date order.
python -m seal.cli seal data_tenant/sis_facility_checkpoint_route

# 2. Check one seal.
facilityops-verify \
    output/sealed/sis_facility_checkpoint_route/20260728_144120-sis_facility_checkpoint_route/seal_20260728_144120-sis_facility_checkpoint_route.json \
    --record data_tenant/sis_facility_checkpoint_route/20260728_144120-sis_facility_checkpoint_route/run.json \
    --evidence-root data_tenant/sis_facility_checkpoint_route

# 3. Check the whole route's chain, and find any deleted run.
facilityops-verify --chain output/sealed/sis_facility_checkpoint_route

# 4. The route's continuity score, as JSON.
python -m compare.cli data_tenant/sis_facility_checkpoint_route

# 5. One run's report on its own, with the M4 sections in it.
python -m report.cli \
    --record data_tenant/sis_racks_checkpoint_route/20260821_191548-sis_racks_checkpoint_route/run.json \
    --evidence-root data_tenant/sis_racks_checkpoint_route
```

---

## 2. Every command, and what it runs

| Command | What it does | Code it runs | Writes |
|---|---|---|---|
| `python -m seal.cli seal <path>` | Seals every run of every route at or under `<path>`, oldest first. Renders each run's report, then seals record, evidence, report and manifest together. A run that already has a seal is left alone. | `seal/cli.py` → `report/cli.py` (render) → `seal/create.py` | `output/sealed/<route>/<run_id>/` (see [3.3](#33-where-files-go)) |
| `facilityops-verify <seal> --record <run.json> --evidence-root <folder>` | Checks one seal against the record and evidence you hold now. Names exactly what moved. | `seal/verify.py` only | Nothing |
| `facilityops-verify --chain <sealed route folder>` | Checks every seal of a route in sequence order. Finds a run deleted from the middle. | `seal/verify.py` only | Nothing |
| `python -m compare.cli <route folder>` | Prints the route's continuity score as JSON. | `compare/continuity.py`, which calls `seal/verify.py` on each seal | Nothing |
| `python -m report.cli --record <run.json> --evidence-root <folder>` | Renders one run's report. The M4 sections are part of it. | `report/cli.py` → `compare/`, `standards/` | `output/report_<run_id>.pdf` and `.manifest.json` |
| `python -m pytest tests/seal tests/compare tests/standards` | Runs the M4 tests. | see [14](#14-tests) | `tests/output/` |

### Options

**`seal`**

| Option | Default | Meaning |
|---|---|---|
| `<path>` | required | `data_tenant`, or one route folder in it. Not a run folder: runs must be sealed in order. |
| `--out` | `output/sealed` | Where the sealed tree is written. |
| `--path-prefix` | `paths.path_prefix` in `config/report.yaml` (`/run-files`) | Taken off recorded evidence paths before they are joined to the evidence root. |

**`facilityops-verify`**

`python -m seal.cli verify` runs the same code with the same options, if the
project isn't installed.

| Option | Meaning |
|---|---|
| `<seal>` | The `seal_<run_id>.json` to check. |
| `--record` | The run record the seal covers. |
| `--evidence-root` | The folder the evidence paths are relative to. For `data_tenant`, that is the route folder. |
| `--chain FOLDER` | Check a whole route's sealed folder instead of one seal. Needs no other option. |

Exit codes for both forms: **0 intact, 1 tampered, 2 could not verify.**

**`compare.cli` (record continuity)**

| Option | Default | Meaning |
|---|---|---|
| `<route>` | required | `data_tenant/<route>`. |
| `--from`, `--to` | first and last run date | The window, `YYYY-MM-DD`, both days counted. |
| `--sealed` | `output/sealed/<route>` | Where the route's seals are. |
| `--tenants` | `config/tenants.yaml` | Where `runs_per_day` is read from. |

**`report.cli`** has the same options as in Milestone 1. For a run in
`data_tenant`, pass `--evidence-root data_tenant/<route>`: the default is
`data/`, so without it every photo shows as missing.

---

## 3. How the pieces fit together

### 3.1 Code layout

```
src/
├── common/            shared by everything
│   ├── runs.py          finds the run before this one, from folder names    (new)
│   └── ...
├── seal/              (new) seal and verify. Standard library + common only
│   ├── canonical.py     the canonical form: the input to every hash
│   ├── create.py        builds a seal
│   ├── verify.py        checks a seal, or a whole chain
│   └── cli.py           the seal and verify commands
├── compare/           (new)
│   ├── rounds.py        comparison with the previous run
│   ├── continuity.py    continuity score
│   └── cli.py           the record-continuity command
├── standards/         (new)
│   ├── packs.py         loads the standards packs
│   └── render.py        maps findings to clauses
├── report/            the report engine. Calls compare/ and standards/
├── agents/            Milestone 2
└── query/             Milestone 3
config/
├── report.yaml        section switches, including the three M4 ones
├── tenants.yaml       runs_per_day and standards_packs, per tenant
└── standards/         one YAML file per standards pack
```

### 3.2 Who may use what

The arrows point at what a package uses.

```
report ──► compare ──► seal ──► (standard library)
   │          │          └────► common (paths only, in cli.py)
   │          └──────────────► common, report.derive, report.gaps
   └──► standards ──► common, agents.slots (number words)
```

The rules behind this:

- **`src/seal` uses only the standard library and `src/seal` itself.**
  `verify.py`, `canonical.py` and `create.py` import nothing else. `cli.py`
  reads two paths from `common` at the top and loads the report engine only
  inside the `seal` command. So `verify` runs without the report engine
  installed. An auditor years from now should not need our pipeline.
  Test TS-08 checks this.
- **The previous run is found in `src/common/runs.py`**, not in `compare`,
  because the seal chain must name the same previous run, and `seal` may
  import only `common`.
- **The client's domain content lives in config, never in code.** Standards
  clauses and their mappings are in `config/standards/`. The code holds no
  clause number and no feature name.
- **No module reads config when it is imported.** Config is read when a
  command runs and is passed down.

### 3.3 Where files go

Input, as exported (`data_tenant`):

```
data_tenant/<route>/<run_id>/run.json
data_tenant/<route>/<run_id>/evidence/...
```

`run_id` is `<YYYYMMDD_HHMMSS>-<route>`, and the folder is named after it.
That name is how runs are put in date order.

Sealed output mirrors it:

```
output/sealed/<route>/<run_id>/
├── report_<run_id>.pdf             the report, rendered at seal time
├── report_<run_id>.manifest.json   its manifest
├── seal_<run_id>.json              the seal
└── seal_<run_id>.record.json       the record in canonical form, so verify can name changed fields
```

### 3.4 What happens when a run is sealed

```
run.json + evidence
      │
      ▼
render the report (report/cli.py run_pipeline)
  ├── comparison with the run before it          compare/rounds.py
  ├── continuity up to the run before it         compare/continuity.py
  └── findings against the enabled packs         standards/render.py
      │
      ▼
report_<run_id>.pdf + report_<run_id>.manifest.json
      │
      ▼
seal (seal/create.py)
  ├── canonical hash of the record + element counts
  ├── sha256 of every evidence file
  ├── sha256 of the PDF and the manifest
  ├── link to the previous run's seal
  └── hash of every config file the render read
      │
      ▼
seal_<run_id>.json + seal_<run_id>.record.json
```

---

## 4. Sealing: how a seal works

A seal is a JSON file of hashes. Change anything it covers, and a hash no
longer matches.

### 4.1 What a seal holds

```json
{
  "seal_version": "1.0",
  "run_id": "20260728_144120-sis_facility_checkpoint_route",
  "sealed_at": "2026-11-15T10:00:00Z",
  "sealed_by": "facilityops-report 0.1.0",
  "algorithm": "sha256",
  "record": {
    "canonical_hash": "sha256:…",
    "element_counts": {
      "checkpoints": 9, "findings": 1, "sensor_alerts": 0,
      "sensor_samples": 412, "event_log": 31
    }
  },
  "evidence": {
    "file_count": 87,
    "files": [
      { "path": "<run_id>/evidence/.../checkpoint_1_N.jpg", "sha256": "9c2e…", "bytes": 48213 }
    ],
    "aggregate_hash": "sha256:…"
  },
  "outputs": {
    "report_pdf": "sha256:…",
    "manifest_json": "sha256:…"
  },
  "chain": {
    "previous_run_id": null,
    "previous_seal_hash": null,
    "sequence": 1
  },
  "config_hash": "sha256:…"
}
```

The values above show the shape only.

| Part | What is hashed | What it catches |
|---|---|---|
| `record.canonical_hash` | The whole record in canonical form ([5](#5-the-canonical-form)) | Any changed value, added or removed key, reordered array |
| `record.element_counts` | How many checkpoints, findings, alerts, samples and events | A deleted entry, named by count ("findings: sealed 1, found 0") |
| `evidence.files` | Each evidence file the record names, by its content | A swapped or edited image, even at the same size and name |
| `evidence.aggregate_hash` | The file list itself | A file moved, added or removed from the list |
| `outputs` | The report PDF and the manifest | A re-rendered or edited report |
| `chain` | The previous run's seal | A deleted run ([7](#7-the-chain)) |
| `config_hash` | Every config file the render read: `report.yaml`, `b2_derivations.yaml`, `agents.yaml`, `action_mapping.yaml`, `tenants.yaml`, the facility's sign-off file, each enabled standards pack | Recorded for reference. `verify` does not recompute it. |

**Evidence files are checked by hash, never by size.** An image replaced by
another of exactly the same size and name is still caught.

**A file that was already missing when the run was sealed stays in the
list,** with `sha256` and `bytes` set to `null`. The gap is sealed, not
dropped. If the file appears later, verify reports it as changed.

**Evidence paths come from the record:** every checkpoint's
`evidence_images` and `annotated_images`, the `evidence_image` of every
detection, finding and live detection. The `/run-files` prefix comes off, the
same way the report does it. A file not named in the record is not sealed.

### 4.2 The sealed copy of the record

`seal_<run_id>.record.json` is the record in canonical form. Verify uses it
to name **which field** changed and **both values**. It has no hash of its
own: its canonical hash must equal `record.canonical_hash`, so one hash covers
both. If the copy itself has been changed, verify still reports the record
as changed, but says it cannot name the fields.

### 4.3 Sealing again

Running `seal` again on the same route is safe. A run with a seal is left
alone: no new render, no new seal. It still counts as the previous run for
the next one. A run with a report but no seal was never linked, so it is
rendered and sealed again.

---

## 5. The canonical form

Every hash in Milestone 4 depends on this. Changing it later breaks every
seal already written, including any an auditor holds.

**The rules:**

1. Keys sorted at every level.
2. No spaces: `,` between items, `:` between key and value.
3. UTF-8, with non-ASCII characters written as themselves, not `\u` escapes.
4. Numbers exactly as Python's `json` writes them. A float keeps full
   precision (`30.86`, `0.1`). An integer stays an integer: `1` and `1.0`
   are different.
5. **Arrays are never sorted.** Their order is part of the record, so a
   reordered array is a changed record.
6. **A missing key and a key set to `null` are different.**
7. The raw JSON is hashed, never the loaded `Record`. The loader treats
   missing and `null` alike, so hashing it would hide a difference.

**The file must be strict JSON.** It is refused, with the reason, if it:
- is not UTF-8;
- has the same key twice in one object (plain JSON parsers keep the last
  one silently, which could hide a value);
- holds `NaN`, `Infinity` or `-Infinity`;
- holds a number too large to read exactly, such as `1e400`.

**The hash** is SHA-256 of those bytes, written as `sha256:<hex>`.

### Reproducing it

Anyone can work out a record's canonical hash without our code:

```python
import hashlib, json

with open("run.json", encoding="utf-8") as f:
    record = json.load(f)
text = json.dumps(record, sort_keys=True, separators=(",", ":"),
                  ensure_ascii=False, allow_nan=False)
print("sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest())
```

For a well-formed record this prints the seal's `record.canonical_hash`.
(Plain `json.load` does not refuse a repeated key the way our loader does.)
For a tool other than Python, the float form is what to watch: it must be the
shortest form that reads back as the same number, which is what Python
writes.

Re-formatting a record doesn't change its hash. Reordering keys or changing
indentation and line breaks leaves the canonical form, and so the hash, the
same. Test TS-06 checks this.

---

## 6. Verifying: the standalone tool

```bash
facilityops-verify seal_<run_id>.json --record <run.json> --evidence-root <folder>
```

It reads the seal, the record and the evidence you give it, hashes them again
with the same functions the seal used, and compares. It writes nothing and
renders nothing.

### 6.1 The five checks

| Check | PASS | FAIL | COULD NOT VERIFY |
|---|---|---|---|
| record | canonical hash matches | it does not. The changed fields and both values are listed | the record file is missing or not readable |
| evidence | every file matches | a file's content changed, or a file missing at seal time is there now | a file is missing now |
| element counts | every array has its sealed length | a count differs, both counts named | the record cannot be read |
| outputs | PDF and manifest match | either was changed | either is missing |
| chain | the link to the previous seal holds | the previous seal changed since this one was made, or the sequence does not follow on | the previous seal is missing or unreadable, or a break was recorded when sealing |

### 6.2 The result

```
SEAL VERIFICATION — 20260728_144120-sis_facility_checkpoint_route
  record            FAIL     canonical hash mismatch
                             checkpoints[3].sensor.environment.temperature_c
                             sealed 30.86, found 28.10
  evidence          FAIL     1 of 87 files altered
                             <run_id>/evidence/.../checkpoint_2_NE.jpg
                             sealed 9c2e..., found 44a1...
  element counts    FAIL     findings: sealed 1, found 0
  outputs           PASS
  chain             PASS     sequence 14, previous seal verified

RESULT: TAMPERED — 3 of 5 checks failed
```

| Result | Exit code | Means |
|---|---|---|
| `INTACT` | 0 | every check passed |
| `TAMPERED` | 1 | at least one check failed: proof something changed |
| `COULD NOT VERIFY` | 2 | nothing failed, but at least one check could not be made: a file is missing, or the seal cannot be read |

A failed check outranks one that could not be made. "Could not verify" never
collapses into "tampered" or "intact": a missing file is not proof of
tampering, and it is not proof of nothing either.

### 6.3 How each kind of tampering is caught

| Tampering | Caught by | Shows as |
|---|---|---|
| One value edited | record hash, then the sealed copy | the field path and both values |
| One image replaced, same name and size | evidence hash | the file, with both hashes |
| One finding deleted | element counts, and the record hash | `findings: sealed N, found N-1` |
| An array reordered, nothing else changed | record hash (arrays are never sorted) | the record check fails |
| Re-formatted only (key order, indent) | nothing: canonical form absorbs it | INTACT |
| An evidence file deleted | evidence | COULD NOT VERIFY, exit 2, not TAMPERED |

---

## 7. The chain

Each seal names the run before it and holds the hash of that run's seal. The
seals of a route become a sequence, so deleting a whole run leaves a hole.

### 7.1 How a route is chained

- Runs are sealed in folder-name order, which is date order.
- `sequence` is the run's place in that order: 1, 2, 3, …
- The first run has `previous_run_id: null` and `sequence: 1`. That is a
  valid start, not a break.
- Each later seal holds `previous_seal_hash`: the canonical hash of the
  previous seal file.

### 7.2 A break is reported, never repaired

If the previous seal is missing, unreadable, belongs to another run or has
the wrong sequence, the new seal records:

```json
"chain": {
  "previous_run_id": "<the run before>",
  "previous_seal_hash": null,
  "sequence": 5,
  "chain_broken": true,
  "reason": "no seal for <the run before> at <path>"
}
```

It is **never re-linked to an older run.** That would hide the gap, which is
the opposite of the point.

### 7.3 Checking a whole route

```bash
facilityops-verify --chain output/sealed/<route>
```

```
CHAIN VERIFICATION — stretch3_checkpoint_route
  sequence 1        PASS     20260606_093832-stretch3_checkpoint_route
                             sequence 1, start of the chain
  sequence 2        FAIL     no seal with this sequence
                             the next seal was linked to it, so it was removed later
  sequence 3        COULD NOT VERIFY  20260609_103555-stretch3_checkpoint_route
                             sequence 3, previous seal for 20260609_101952-stretch3_checkpoint_route not found

RESULT: TAMPERED — 1 of 3 checks failed
```

It lists every sequence from 1 to the highest seal found:

| Sequence | Status |
|---|---|
| Has one seal | that seal's own chain check, as in [6.1](#61-the-five-checks) |
| Has no seal, and the next seal was linked to it | **FAIL.** It existed when the next run was sealed, so it was removed afterwards |
| Has no seal, and the next seal recorded the break when sealed | **COULD NOT VERIFY.** The hole is real, but nothing proves it was made afterwards (for example, that run failed to seal) |
| Has two seals | **FAIL** |

The continuity score also reports the chain, as `chain_intact`
([9](#9-record-continuity)).

---

## 8. Comparison with the previous run

Every report compares its run with the run before it on the same route. It
states what differs. It never says improved, worse, better, degraded,
deteriorated, rising, falling or concerning, in any field name, value or
sentence. "Checkpoint 5 recorded 2.19 g on the 28th and 0.61 g on the 31st"
is a statement of what was recorded. "Checkpoint 5 has improved" is a
judgement, and the comparison never makes it.

### 8.1 Which run is "previous"

The latest run folder beside this one, of the same route, whose time is
before this run's. It comes from the folder names
(`common/runs.py`). If that run cannot be read, there is no comparison and the
section says why. The run before it is never used instead.

### 8.2 Who changed what: the five attributions

Every difference carries one attribution. Where the records explain a
difference, it says so. Where they don't, it says that.

| Attribution | When |
|---|---|
| `ROUTE_CHANGE` | The checkpoint is on one run's route and not the other's. It was taken off or added, not missed. |
| `MISSED` | The checkpoint is on both routes and one run did not reach it. Comes with that run's `missed_reason`. |
| `SENSOR_UNAVAILABLE` | A sensor block can't be read in one run: its device flag was false, or the whole reading was unusable. Comes with the reason. |
| `VALUE_CHANGED` | Both runs hold a reading and they differ. Comes with both values and the signed difference. Nothing more: no cause, no likely reason. |
| `UNATTRIBUTED` | A difference exists and the records do not say why. **The default, and it is common:** the records hold values, not causes. |

### 8.3 The order they are decided in

A difference is reported **once**, at the first level where the two runs
part ways. The comparison works down this ladder for each checkpoint and
stops at the first step that differs:

```
1. Is the checkpoint on both routes?
       no  → ROUTE_CHANGE                         stop: nothing below is compared
       yes ↓
2. Did both runs reach it (status COMPLETED)?
       no  → MISSED, if the other run's status is MISSED, with its missed_reason
             UNATTRIBUTED, for any other status
                                                  stop: its results and readings are not compared
       yes ↓  (and its result_status is compared: differs → UNATTRIBUTED)
3. For each sensor block: is it usable in both runs?
       no  → SENSOR_UNAVAILABLE, with the reason   stop: that block's fields are not compared
       yes ↓
4. For each field in the block:
       both hold a value, and they differ → VALUE_CHANGED
       one holds a value, the other null  → UNATTRIBUTED
       equal, or null in both             → no difference, not listed
```

Why this order:
- **A checkpoint that wasn't reached has no readings to compare.** Its
  absence is the difference, reported once as ROUTE_CHANGE or MISSED. It
  isn't reported again as a dozen missing readings.
- **A sensor block that was down has no real values.** It is reported once,
  as SENSOR_UNAVAILABLE. A placeholder value from it never shows up as a
  changed reading.
- **Missed in both runs is not a difference,** so it isn't listed.

Findings and coverage sit outside the ladder:
- **Findings** are compared for the whole run, whether or not their
  checkpoint was reached in both.
- **Coverage** is each run's totals side by side.

Neither carries an attribution. A finding only in the previous run is listed
as `only_in_previous`, never "resolved".

### 8.4 What is compared, and how each difference is labelled

| Dimension | Difference | Label |
|---|---|---|
| Checkpoints | on one run's route and not the other's | `ROUTE_CHANGE` |
| | on both routes, reached in one, `MISSED` in the other | `MISSED`, with its `missed_reason` |
| | on both routes, reached in one, any other status in the other | `UNATTRIBUTED` |
| Results | completed in both, `result_status` differs | `UNATTRIBUTED`, both results |
| Sensors | a sensor block usable in one run only | `SENSOR_UNAVAILABLE`, with the reason |
| Readings | a value in both runs that differs | `VALUE_CHANGED`, both values and the signed difference, current minus previous |
| | a value in one run, null in the other | `UNATTRIBUTED` |
| Findings | by checkpoint and feature | `only_in_previous`, `only_in_current`, `in_both` |
| Coverage | evidence count, missed count, could-not-read count | each run's figure side by side |

**UNATTRIBUTED is the default.** A difference the records do not explain gets
no reason. A finding that is only in the previous run is "only in previous",
not "resolved": the records do not say it was fixed.

Readings are compared at the decimals they print to (`decimals` in
`config/report.yaml`), so a row never shows a difference of 0. Two identical
runs give empty lists everywhere, and the section says nothing differs.

The comparison is in the report, after the sensor alerts, and in the manifest
as the `comparison` array (one entry, or none without a previous run). The
manifest also carries `previous_run_id`.

---

## 9. Record continuity

**How much of the intended record exists for a route.** Not how healthy the
facility is.

```
continuity = (runs_completed × coverage_mean) / runs_expected
```

| Term | Meaning |
|---|---|
| `runs_expected` | `runs_per_day` (from the route's tenant in `config/tenants.yaml`) × days in the window |
| `runs_completed` | runs in the window that have a seal |
| `runs_missing` | the dates short of their scheduled runs, one entry per missing run |
| `coverage_mean` | mean, over the sealed runs, of COMPLETED checkpoints out of all checkpoints |
| `seals_verified`, `seals_broken` | each counted seal checked with `verify`: INTACT and TAMPERED. COULD NOT VERIFY is neither |
| `chain_intact` | true only when there is at least one seal and every seal's chain check passed |

```json
{
  "route_id": "sis_facility_checkpoint_route",
  "window": { "from": "2026-07-28", "to": "2026-07-30" },
  "runs_expected": 3,
  "runs_completed": 7,
  "runs_missing": [],
  "coverage_mean": 0.51,
  "continuity": 1.19,
  "continuity_formatted": "119%",
  "seals_verified": 7,
  "seals_broken": 0,
  "chain_intact": true
}
```

Example shape. Values are illustrative.

Things to know:

- **Continuity can be above 100%.** A day with more sealed runs than the
  schedule counts them all. An extra run on one day does not fill a gap on
  another.
- **Continuity and the chain are separate claims, and both are reported.** A
  complete set of runs with a broken chain still scores high, with
  `chain_intact: false` beside it. Test TC-07 checks this.
- **Continuity says nothing about condition.** A route where every scheduled
  run was sealed and every checkpoint failed has 100% continuity. The report
  says so.
- **In the report, only runs before this one count.** This run is sealed from
  its own report, so it has no seal yet when the report is made.
- The score is in the report and from `compare.cli`. It is not in the manifest.

---

## 10. Condition evidence (standards packs)

The report shows a run's findings against the clauses of a standard, such as
NFPA 70B. **It presents evidence. It never certifies compliance.** Whether a
finding breaks a standard is a qualified person's call.

### 10.1 Who supplies what

| The client supplies | We built |
|---|---|
| The standards pack: clauses, their text, which finding features map to each | The loader, the mapping, the report section, the "not mapped" list |

We never write a clause mapping, and never guess one from a clause's wording.

### 10.2 A pack file

One YAML file per pack in `config/standards/`, named `<pack_id>.yaml`:

```yaml
pack_id: nfpa_70b_2023
pack_version: "1.0"
title: "NFPA 70B — Standard for Electrical Equipment Maintenance"
clauses:
  - clause: "11.3.2"
    summary: "Infrared inspection of electrical connections"
    maps_to_features: [thermal_hotspot, blocked_airflow_path]
    evidence_required: [thermal_reading, evidence_image]
```

| Key | Required | Meaning |
|---|---|---|
| `pack_id` | yes | Must match the file name. |
| `pack_version` | yes | Printed in the report, covered by `config_hash`. |
| `title` | yes | The pack's heading in the report. |
| `clauses` | yes, at least one | |
| `clauses[].clause` | yes | The clause number, **in quotes**. |
| `clauses[].summary` | no | Printed after the clause number. |
| `clauses[].maps_to_features` | no | The finding features that map to this clause. None means it can never be assessed. |
| `clauses[].evidence_required` | no | Names of finding fields to show for each mapped finding. |

**Keys the loader doesn't know are ignored,** so a pack can carry more than
we read.

**The loader refuses a pack, naming the problem, when:**
- the file is missing;
- `pack_id` doesn't match the file name;
- a required key is missing;
- a clause is listed twice;
- a list isn't a list;
- a clause number is an unquoted decimal: YAML reads `11.10` as `11.1`.

### 10.3 Turning a pack on for a customer

In `config/tenants.yaml`, list the pack under the tenant:

```yaml
tenants:
  tenant_racks:
    facility_ids: [sis_racks_checkpoint_route]
    runs_per_day: 1
    standards_packs: [nfpa_70b_2023]
```

An empty list, or no key, means that tenant's reports have no standards
section. Each pack is read once per report.

### 10.4 Adding a standards pack

1. Put the client's file in `config/standards/<pack_id>.yaml`.
2. Add `<pack_id>` to `standards_packs` for each tenant that wants it.
3. Render a report. Nothing in the code changes.

Adding a clause, a feature or a whole pack is only ever a YAML change.

### 10.5 How a finding is matched

- Every finding in the run is checked against every clause of every enabled
  pack.
- A finding maps to a clause when its `feature` is in that clause's
  `maps_to_features`. It can map to several clauses, in several packs.
- **Every finding counts the same, whatever its status:** `logged`,
  `needs_review` and `abstained` alike. The status is shown next to it.
- A finding listed twice (same `finding_id`) counts once.

### 10.6 What the section shows

For each enabled pack:

- **Each clause, with its findings.** The table has one column per name in
  `evidence_required`. For each finding, the column holds the finding's own
  field of that name:

  | The field holds | Shown as |
  |---|---|
  | text starting with `/run-files` | the photo, resolved and embedded like checkpoint photos |
  | a path whose file is missing or broken | the reason, such as "no file at …" |
  | a number | the reading, with its unit and decimals from `config/report.yaml` |
  | any other text | the text |
  | null, or no field of that name | "Not recorded" |

- **A clause with no findings: "Not assessed."** Never "passed" or
  "compliant". No finding is not evidence that a clause is met.

- **Findings that map to no clause** are listed by feature, with a sentence
  each. They are never placed under a clause that sounds similar:

  > One finding of type airflow_obstruction at home_docking_station is not
  > mapped to a clause in the enabled standards packs.

  One checkpoint is named in the sentence. More are counted ("at forty
  checkpoints") and listed in their own column.

- **The statement, in the same words on every run:**

  > This report presents inspection evidence mapped to the referenced
  > clauses. It does not certify compliance with any standard, and no
  > statement in it should be read as a compliance determination.

  It is fixed in code (`standards/render.py`), not in a pack, so no pack edit
  can change it.

### 10.7 In the manifest

The `standards` key holds the same content, both ways round:

```json
"standards": {
  "statement": "This report presents inspection evidence…",
  "packs": [
    {
      "pack_id": "nfpa_70b_2023", "pack_version": "1.0", "title": "…",
      "clauses": [
        {
          "clause": "11.3.2", "summary": "…",
          "evidence_required": ["thermal_reading", "evidence_image"],
          "status": "evidence presented",
          "findings": [
            { "finding_id": "…",
              "evidence": [
                { "field": "thermal_reading", "kind": null, "value": null },
                { "field": "evidence_image", "kind": null, "value": null }
              ] }
          ]
        }
      ]
    }
  ],
  "findings": [ { "finding_id": "…", "clauses": [ { "pack_id": "nfpa_70b_2023", "clause": "11.3.2" } ] } ],
  "unmapped": [ { "feature": "…", "finding_ids": ["…"], "checkpoints": ["…"], "statement": "…" } ]
}
```

| Key | Direction |
|---|---|
| `packs` → `clauses` → `findings` | clause → its findings |
| `findings` | each finding → the clauses it maps to, empty when unmapped |
| `unmapped` | the grouped "not mapped" sentences |

`status` is only ever `evidence presented` or `not assessed`. `kind` is
`image`, `reading` or `null` (not recorded). It is `null` when no pack is
enabled.

---

## 11. On and off switches

All in `config/report.yaml`, under `sections:`.

| Switch | What `false` does | When the key is missing |
|---|---|---|
| `comparison` | No comparison section or contents row. Manifest `comparison` is `[]`. The comparison is not worked out. `previous_run_id` stays in the manifest. | on |
| `continuity` | No continuity section or contents row. The score is not worked out. | on |
| `standards` | No condition evidence section or contents row. Manifest `standards` is `null`. Packs are not loaded, and `config_hash` stops including them. | on |

A missing key counts as **on**, so a config that doesn't name these switches
renders as it did before they existed.

`standards: true` also needs the facility's tenant to list a pack in
`standards_packs`. So there are two ways to turn standards off:
- **for everyone:** set the switch to `false`;
- **for one customer:** give that tenant an empty `standards_packs` list.

The switches only control the report. The `seal`, `verify` and `compare.cli`
commands work the same whatever they say. The seal's `config_hash` covers
`report.yaml`, so a seal records which sections its report had.

---

## 12. What the seal cannot catch

**The seal is tamper-evident, not tamper-proof.** These are its real limits.

1. **Anyone who can run the sealing tool can re-seal.** A seal is plain
   hashes, with no signature and no secret key. Someone who edits a record
   and deletes its seal can seal it again, and the new seal will verify.
   - It is still caught if anyone holds a copy of the old seal, or if a
     later seal links to the old one: the next run's `previous_seal_hash`
     no longer matches, and its chain check fails.
   - To hide it completely, they would have to delete and re-seal every
     later run of the route too.
2. **Deleting the last run of a route leaves no hole.** Nothing comes after
   it to show it is missing. `verify --chain` reports the chain as intact.
   Continuity shows it only as a missing date, and only if the window
   covers that day.
3. **A seal proves what was sealed, not when.** `sealed_at` is written by the
   sealing machine's clock and can be set to anything. Nothing ties a seal
   to an outside clock.
4. **Re-rendering an old run changes its outputs.** If a sealed run's report
   is rendered again with newer code or config, the new PDF and manifest
   won't match the seal (outputs check FAIL). The seal protects the report
   as it was. `seal` never re-renders a sealed run, for this reason.
5. **The canonical form is fixed at version 1.0.** `verify` always uses
   today's canonical code and doesn't branch on `seal_version`. If the
   canonical form ever changes, old seals will fail, unless the old form is
   kept and chosen by `seal_version`. Changing it means doing exactly that.
6. **Someone with write access to the repository can change the tool.** They
   could change `verify.py` so it always says INTACT, or change the canonical
   form and re-seal everything. Verification is only as good as the copy of
   the tool you run. An auditor should run a copy they hold themselves.
7. **Only files the record names are sealed.** An image in the run folder that
   the record doesn't reference is not covered.
8. **`config_hash` is recorded, not checked.** `verify` does not recompute
   it, so a changed config is not reported by `verify`.

---

## 13. Assumptions made

Choices the brief left open, and why. `decisions.md` has more detail on the
comparison and continuity choices.

### Sealing and the chain
- **Run order comes from folder names** (`<YYYYMMDD_HHMMSS>-<route>`), not
  from the record's own times. Both the chain and the previous-run lookup use
  this order.
- **The evidence root for `data_tenant` is the route folder,** because
  recorded paths start with the run_id once `/run-files` is taken off.
- **A missing sequence in `verify --chain` is FAIL when the next seal was
  linked to it, and COULD NOT VERIFY when the next seal already recorded the
  break.** Only the first is proof of removal.
- **The seal's `config_hash` includes all of `tenants.yaml`,** because
  continuity's `runs_per_day` and the enabled packs come from it. This means
  an edit to any tenant's entry changes the hash for every new seal.

### Comparison and continuity
- Comparison and continuity choices are in `decisions.md` (entries 9 and
  10). The main ones: the previous run is the latest earlier folder of the
  same route; findings are matched by checkpoint and feature, not by
  `finding_id`; readings compare at their printed decimals; continuity
  counts only sealed runs; and in a report, only runs before this one count.

### Standards
- **The pack in `config/standards/nfpa_70b_2023.yaml` is a placeholder.** It
  is the brief's example pack, word for word, until the client sends the real
  one. The tests use their own copy, so replacing it changes no test.
- **Enabled packs are set per customer** in `tenants.yaml`
  (`standards_packs`). The tenant is found by the facility's `facility_id`.
- **`evidence_required` names finding fields.** Each name is looked up as a
  field of the finding. Only fields the record loader keeps (the `Finding`
  schema) can be read. A name with no such field reads "Not recorded".
- **A value is an image when it is text starting with `/run-files`.**
  Otherwise it is a reading.
- **Every finding status counts as evidence.** `needs_review` and
  `abstained` findings map like any other, and their status is shown.
- **A broken enabled pack, or an unreadable `tenants.yaml`, stops the
  report** with an error naming the file. It never quietly renders without
  the section.

### Switches
- **The three M4 switches are on when missing,** unlike the B-2/B-3
  switches, which are off when missing. Those were opt-in additions. These
  three were always on until the switches existed.

---

## 14. Tests

```bash
cd /home/moiz/FacilityOps/facilityops-report
source venv/bin/activate

# Once: build the tamper copies TS-01 to TS-10 use (see 14.2)
PYTHONPATH=src python tests/seal/build_data_tenant_test.py

# All Milestone 4 tests
PYTHONPATH=src python -m pytest tests/seal tests/compare tests/standards -v

# One test by its ID
PYTHONPATH=src python -m pytest tests -k "ts13" -v
```

**One area at a time,** quickest first:

| What | Command | Speed |
|---|---|---|
| Standards (TS-12, TS-13, the loader) | `PYTHONPATH=src python -m pytest tests/standards -v` | fast, seals nothing |
| Comparison (TC-01 to TC-05) | `PYTHONPATH=src python -m pytest tests/compare/rounds_test.py -v` | fast, seals nothing |
| Seal and verify (TS-01 to TS-08) | `PYTHONPATH=src python -m pytest tests/seal/verify_test.py -v` | seals 1 run |
| Chain (TS-09 to TS-11) | `PYTHONPATH=src python -m pytest tests/seal/chain_test.py -v` | seals 1 run, plus `stretch3` (3 runs) |
| Continuity (TC-06, TC-07) | `PYTHONPATH=src python -m pytest tests/compare/continuity_test.py -v` | seals `stretch3`, plus `sis_facility` (7 runs) |

**If TS-01 to TS-10 say SKIPPED,** `data_tenant_test` is missing or half
built. The build script writes `data_tenant_test/cases.json` last, so a
folder without it was stopped partway. The script refuses to run over an
existing folder, so delete it and build again:

```bash
rm -r data_tenant_test
PYTHONPATH=src python tests/seal/build_data_tenant_test.py
```

It ends by printing `built …/data_tenant_test from <run>` and one line per
case. `data_tenant` itself is never touched.

### 14.1 The tests and where they are

| Tests | File | Data |
|---|---|---|
| TS-01 to TS-08 | `tests/seal/verify_test.py` | `data_tenant_test` |
| TS-09, TS-10, TS-11 | `tests/seal/chain_test.py` | `data_tenant_test`, and `stretch3` sealed |
| TS-12, TS-13, the loader | `tests/standards/standards_test.py` | real runs, the brief's pack (`tests/standards/packs/`) |
| TC-01 to TC-05 | `tests/compare/rounds_test.py` | a real pair of SIS facility runs |
| TC-06, TC-07 | `tests/compare/continuity_test.py` | `stretch3` and `sis_facility` sealed |
| TC-08 | not written yet | see [15](#15-open-items) |

### 14.2 The test data

- **Real runs in `data_tenant`** are used wherever they already show the
  case: the comparison pair, `stretch3`'s two days with no run, the
  `sis_facility` route's complete run set.
- **`data_tenant_test`** holds copies of one real run, each changed in one
  way (edited value, swapped image, deleted finding, reordered array,
  re-serialised, missing image). `tests/seal/build_data_tenant_test.py`
  builds it. It refuses to run if the folder exists: delete it to rebuild.
- **TS-13's `airflow_obstruction` finding** doesn't occur in any real run. The
  test copies one real run into pytest's temporary folder and changes its one
  finding's feature. Nothing in `data_tenant` changes.
- **TS-11 and TC-07** change copies of our own sealed output (a deleted
  sealed run, a changed seal), in a temporary folder.

### 14.3 Things to know

- **Some tests seal real routes,** which renders a full report per run.
  `stretch3` is 3 runs, `sis_facility` 7. Each route is sealed once per test
  session, into `tests/output/sealed/`, and reused. If the B-2/B-3 sections
  are on and `.env` has an API key, sealing also calls the model.
- **Rendered output is kept** in `tests/output/`, so a failing case can be
  read.
- **TS-08 is a code-review test.** It also has an automatic check that
  importing the verify command loads none of the report engine.

---

## 15. Open items

| Item | State |
|---|---|
| TC-08: comparison and continuity load records through `scope.py` | **Not done.** Comparison finds the previous run from folder names (`common/runs.py`) and continuity reads the route folder directly. Neither knows which tenant is asking. |
| Comparison and continuity as query intents | **Not done.** Needed for TC-08. |
| `facilityops-verify` as an installed command | **Done** in `pyproject.toml` (`seal.cli:verify_main`); appears after `pip install -e .`. Installing the project still installs the report engine's packages: the command never loads them, but a truly separate install for auditors would need `src/seal` packaged on its own. |
| The client's standards pack | **Waiting.** `config/standards/nfpa_70b_2023.yaml` is the brief's example. |
| Results table (test ID, pass or fail, date, evidence) | **Not done.** Needs the tests run first. |
| Tamper fixtures and their verify output, committed as evidence | `data_tenant_test` is built by script. Its verify output is not saved yet. |
| `decisions.md` entry 9 | **Out of date.** It says the comparison has no switch; it now has one ([11](#11-on-and-off-switches)). |
| `decisions.md`, `licences.md` | Need entries for the seal, the chain check, standards and the switches. |
| The new tests | **Written, not yet run.** |
