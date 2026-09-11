# facilityops-report

PDF report engine for facility inspection run records. Reads a run record, renders
it through Jinja2 templates, and writes a PDF plus a manifest.

Status: in progress. `common/schema.py`, `common/loader.py`, `common/paths.py`,
`report/images.py` and the `report.cli` entry point are implemented.
`report/gaps.py` detects all ten gap types, one function each.
`report/derive.py` has the reconciliation counts; its zone telemetry rollups
and alert counts are still to come. The remaining modules are docstring stubs.

## Layout

```
config/                 YAML configuration (not code)
  report.yaml           report-level settings
  branding.yaml         logo, colours, headers
  assets/logo.png
data/                   local run data - git-ignored
  records/              input run records
  <run_id>/evidence/    that run's evidence tree, laid out as its paths record it
output/                 generated PDFs and manifests
src/
  common/               shared layer - imported by report, imports nothing from it
    __init__.py         path constants, ensure_runtime_dirs(), logging setup
    loader.py           parse + validate supplied run JSON
    schema.py           dataclasses for the run record
    paths.py            evidence path rewriting
    provenance.py       version stamping
  report/               the report application
    cli.py              entry point
    derive.py           computed counts, zone telemetry rollups
    gaps.py             gap detection and classification
    images.py           direction parsing, pairing, resize
    manifest.py         manifest.json construction
    render.py           template -> HTML -> PDF
    templates/          Jinja2 partials (_cover, _summary, _zone, ...) + styles.css
tests/
  data/                 supplied runs, one directory each
```

`src/common` is the shared layer: `src/report` imports from it, and it never
imports from `src/report`. Later components are expected to share `common`, so it
stays free of import-time side effects.

## Install

Requires Python 3.11+.

```
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
```
The editable install puts `common` and `report` on the path, so `python -m
report.cli` works from any directory without `PYTHONPATH`. Without installing,
prefix each command with `PYTHONPATH=src` and run from the project root.

A virtual environment records its own absolute path, so renaming or moving the
project directory breaks it: `pip` then falls through to the system one and
Debian answers `error: externally-managed-environment`. Recreate the venv after
a move, or repoint it.

## Data directories

`data/` and `output/` are created on demand by
`common.ensure_runtime_dirs()`, which `src/report/__init__.py` calls. No `.gitkeep`
placeholders are needed.

A supplied run keeps the layout its recorded paths imply, so that the rewrite rule
in section 2.3 of the brief resolves with `path_prefix: /run-files` unchanged:

```
data/
  20260728_144120-sis_facility_checkpoint_route/
    records/run.json
    evidence/SIS Facility Checkpoint Route/checkpoint_1/checkpoint_1_N.jpg
```

Each run keeps its own record and evidence together, in the shape its recorded
paths imply. The record path and the evidence root are two separate arguments;
the evidence root is `data/`, and the run id in the recorded path selects the
run beneath it.

## Running

The pipeline runs as the seven stages section 5.2 of the brief fixes:

```
load -> validate -> resolve_images -> detect_gaps -> derive -> render -> manifest
```

Built so far: `load`, `validate` and `resolve_images`. They are plain calls in
`run_pipeline()`, in order; each later stage is one more call as its module is
written.

```python
from pathlib import Path
from report.cli import run_pipeline

artifacts = run_pipeline(Path("data/<run_id>/records/run.json"), Path("data"))
artifacts.record                      # the parsed Record
artifacts.images["checkpoint_1"]      # that checkpoint's ResolvedImages
artifacts.grids["checkpoint_1"]       # its eight DirectionCells, in compass order
```

`run_pipeline` returns an `Artifacts` object; later stages add their outputs to
it rather than changing the call.

### Commands

Run from the project root. The relative `--record` path below is resolved against
the current directory; the other three paths are not - `--evidence-root`,
`--config` and `--output-dir` default to absolute paths derived from the package
location, so they point at the project wherever you stand.

```
# parse a record and report what it disagrees with section 2 about
python -m report.cli --record data/20260728_144120-sis_facility_checkpoint_route/records/run.json

# every anomaly and parse decision
python -m report.cli --record data/20260728_144120-sis_facility_checkpoint_route/records/run.json -v

# write the parsed record as JSON, to see what the loader made of the file
python -m report.cli --record data/20260728_144120-sis_facility_checkpoint_route/records/run.json --dump-record output/parsed.json

# the resolved images and the eight-cell grids
python -m report.cli --record data/20260728_144120-sis_facility_checkpoint_route/records/run.json --dump-artifacts output/artifacts.json

# the same, to stdout: logs go to stderr, so this pipes cleanly
python -m report.cli --record data/20260728_144120-sis_facility_checkpoint_route/records/run.json --dump-record | jq .anomalies

# a run whose evidence lives somewhere else
python -m report.cli \
    --record "data/20260728_144120-sis_facility_checkpoint_route/records/run.json" \
    --evidence-root "/mnt/handover/SIS Facility Route" \
    --output-dir out/
```

Not installed, or running from elsewhere:

```
PYTHONPATH=src python -m report.cli --record data/20260728_144120-sis_facility_checkpoint_route/records/run.json

PYTHONPATH=/path/to/facilityops-report/src python -m report.cli \
    --record /path/to/facilityops-report/data/20260728_144120-sis_facility_checkpoint_route/records/run.json
```

### Options

| Option | Default | Meaning |
| --- | --- | --- |
| `--record PATH` | required | the run record JSON to render |
| `--evidence-root PATH` | `data/` | root that recorded evidence paths are rewritten against |
| `--config PATH` | `config/report.yaml` | report configuration |
| `--output-dir PATH` | `output/` | where the PDF and manifest are written |
| `--dump-record [PATH]` | | write the parsed record as JSON to PATH; with no PATH, to stdout |
| `--dump-artifacts [PATH]` | | write the resolved `images` and the `grids` as JSON |
| `-v`, `--verbose` | | log at debug level |

### Exit status

| Code | Meaning |
| --- | --- |
| 0 | finished |
| 1 | the run failed for a reported reason, such as an unreadable record |
| 2 | the command line was wrong |

Logs go to stderr, so stdout stays clear for redirected output.

### Paths with spaces

Recorded evidence paths contain spaces - `SIS Facility Checkpoint Route` is a real
directory name - and so may the paths you pass in. Quote every path argument.

No path is passed through a shell, split on whitespace, or interpolated into a
command anywhere in the engine; each is a `pathlib.Path` from argparse onwards.
Recorded paths are stripped of surrounding whitespace only, so spaces inside a
name survive: all 87 references in the reference run contain spaces, and all 87
resolve.

## Configuration

`config/report.yaml` holds the report settings. `common/paths.py` reads three of
them at import - the compass directions, the thermal filename suffix, and the
path prefix stripped from recorded evidence paths:

```
paths:
  path_prefix: /run-files
evidence:
  directions: [N, NE, E, SE, S, SW, W, NW]
  thermal_suffix: _thermal
```

Change `directions` or `thermal_suffix` there and the filename parsing follows.

### Not read yet

The `report`, `sensor`, `sections` and `branding` sections are in the file but
nothing consumes them yet; they are wired in as the modules that need them are
written. Config validation - a clear error for a missing key, a warning for an
unknown one - is not implemented, and neither is the `config_hash` the manifest
needs.

## Tests

```
pytest
```

Collection is limited to `tests/` via `[tool.pytest.ini_options]` in
`pyproject.toml`. Everything lives in `tests/test_report.py`, and test names
carry the section 7 test id where a test covers one, so the results table in
deliverable 10 can be read off the pytest output.

### Fixtures

Evidence trees are built inside pytest's `tmp_path` at test time; there are no
committed binary fixtures. A zero-byte file, a text file wearing a `.jpg`
extension, a directory whose name carries spaces and mixed case, and a real
JPEG written by Pillow are a few lines each, so the suite runs on a clean
machine with nothing to fetch, and every failure case is legible in the test
source instead of hidden in a file nobody can open.

Two tests read the supplied reference run at
`data/20260728_144120-sis_facility_checkpoint_route/records/run.json` and check
that all 87 recorded evidence paths resolve and that thermal coverage matches
section 2.3. `data/` is git-ignored local run data, so both skip when it is
absent rather than failing.

### Supplied runs

Each supplied run keeps the layout its own recorded paths imply:

```
data/<run_id>/
  records/run.json
  evidence/SIS Facility Checkpoint Route/<checkpoint_id>/<checkpoint_id>_<DIR>.jpg
```

They arrived with inconsistent layouts - `Evidence/` against `evidence/`,
`Record/` against `Records/`, one record at the top level, one evidence tree
nested a second time under its run id - and were normalised to the above. The
lowercase form is what the records themselves reference, so the rewrite rule of
section 2.3 resolves against `data/` as the evidence root with
`path_prefix: /run-files` unchanged.
