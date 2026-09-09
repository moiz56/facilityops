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
data/                   local run data - git-ignored, created automatically
  records/              input run records
  evidence/             input evidence images
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

WeasyPrint needs system libraries (Pango, Cairo, GDK-PixBuf) that pip does not
install; see the WeasyPrint documentation for your platform.

## Data directories

`data/`, `data/records/` and `data/evidence/` are git-ignored and created on demand
by `common.ensure_data_dirs()`, which `src/report/__init__.py` calls. No `.gitkeep`
placeholders are needed.

## Tests

```
pytest
```

Collection is limited to `tests/` via `[tool.pytest.ini_options]` in
`pyproject.toml`. `tests/test_report.py` is currently empty.
