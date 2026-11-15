"""Shared setup for the seal tests.

Needs data_tenant_test, built by build_data_tenant_test.py. Its original run
is sealed once per session with the seal command's own code, which renders
the report first: the B-2 and B-3 agents run if their sections are on, so a
valid API key in .env gives the real text and none gives their fallbacks.

The report, manifest, seal and record copy are written under
tests/output/seal/, so a failing case can be read. Every case is verified
against that one seal, with its own record and evidence.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from common import PROJECT_ROOT
from common.paths import DEFAULT_CONFIG, PATH_PREFIX, setting
from seal.cli import seal_route
from seal.create import seal_path
from seal.verify import result, verify_seal

TEST_DATA = PROJECT_ROOT / "data_tenant_test"
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output" / "seal"


@pytest.fixture(scope="session")
def cases() -> dict:
    """What build_data_tenant_test.py changed in each case."""
    path = TEST_DATA / "cases.json"
    if not path.is_file():
        pytest.skip("data_tenant_test is not built: "
                    "PYTHONPATH=src python tests/seal/build_data_tenant_test.py")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def sealed(cases) -> Path:
    """The original run, sealed once. Returns the seal's path."""
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    route = TEST_DATA / "original" / cases["route"]
    sealed_by = f"facilityops-report {setting(DEFAULT_CONFIG, 'provenance', 'engine_version')}"

    counts = seal_route(route, OUTPUT_DIR, PATH_PREFIX, sealed_by)
    assert counts == {"sealed": 1, "already sealed": 0, "failed": 0}, counts
    return seal_path(OUTPUT_DIR / cases["run_folder"], cases["run_id"])


@pytest.fixture(scope="session")
def case_paths(cases):
    """case_paths(case) is that case's record and evidence root."""
    def paths(case: str) -> tuple[Path, Path]:
        route = TEST_DATA / case / cases["route"]
        return route / cases["run_folder"] / "run.json", route
    return paths


@pytest.fixture
def verify(sealed, case_paths):
    """verify(case) checks that case against the seal.

    Returns the checks by name, the result line and the exit code.
    """
    def run(case: str):
        record, evidence_root = case_paths(case)
        _, checks = verify_seal(sealed, record, evidence_root)
        line, code = result(checks)
        return {check.name: check for check in checks}, line, code
    return run
