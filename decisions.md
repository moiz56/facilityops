# Decisions

## Build backend: Hatchling

We use Hatchling as the build backend, declared in `pyproject.toml`:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

Reasons:

- Configuration only. No `setup.py` or `setup.cfg`; everything lives in
  `pyproject.toml` alongside the PEP 621 `[project]` metadata.
- Clean src-layout support. The code sits under `src/`, but is imported as
  `common.*` and `report.*`. Hatchling handles this with two short blocks:

  ```toml
  [tool.hatch.build.targets.wheel]
  packages = ["src/common", "src/report"]

  [tool.hatch.build.targets.wheel.sources]
  "src" = ""
  ```

  `packages` selects exactly the two trees to ship; `sources` strips the `src/`
  prefix so `src/report/cli.py` installs as `report/cli.py`.
- Template files under `src/report/templates/` (`.j2`, `.css`) are picked up as part
  of the package tree without a separate package-data declaration.

Dependencies are pinned exactly (`==`) rather than with ranges, so resolution is
reproducible across machines. Bumps are deliberate and manual.
