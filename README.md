# facilityops-report

PDF report engine for facility inspection run records. Reads a run record, renders
it through Jinja2 templates, and writes a PDF plus a manifest.

Status: skeleton. The layout below is in place; most modules are docstring stubs.

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
    __init__.py         path constants + ensure_data_dirs()
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
## Data directories

`data/` and `output/` are created on demand by
`common.ensure_runtime_dirs()`, which `src/report/__init__.py` calls. No `.gitkeep`
placeholders are needed.

A supplied run keeps the layout its recorded paths imply, so that the rewrite rule
in section 2.3 of the brief resolves with `path_prefix: /run-files` unchanged:

```
data/
  records/run.json
  20260728_144120-sis_facility_checkpoint_route/
    evidence/SIS Facility Checkpoint Route/checkpoint_1/checkpoint_1_N.jpg
```

Records and evidence are separate inputs - the engine takes a record path and an
evidence root as two arguments - so records collect in one folder while each run's
evidence keeps the shape its recorded paths imply. The evidence root is `data/`.

## Tests

```
pytest
```

Collection is limited to `tests/` via `[tool.pytest.ini_options]` in
`pyproject.toml`. `tests/test_report.py` is currently empty.
