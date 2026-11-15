"""Verification: TS-01 to TS-08, each against a case in data_tenant_test.

Each case is one real run altered in one way after the original was sealed.
cases.json says exactly what changed, so nothing here hard-codes a value
from the data.
"""

import json
import os
import subprocess
import sys

import pytest

from common import PROJECT_ROOT
from seal.cli import verify_main
from seal.verify import FAIL, PASS, UNVERIFIED, VerifyError, verify_seal


def test_ts01_intact_pack(verify):
    checks, line, code = verify("original")
    assert code == 0
    assert line == "INTACT"
    assert all(check.status == PASS for check in checks.values())


def test_ts02_edited_value_names_field_and_both_values(cases, verify):
    edit = cases["edited_value"]
    checks, line, code = verify("edited_value")
    assert code == 1
    assert line.startswith("TAMPERED")
    assert checks["record"].status == FAIL
    expected = f"{edit['path']}: sealed {json.dumps(edit['sealed'])}, found {json.dumps(edit['found'])}"
    assert expected in checks["record"].lines


def test_ts03_swapped_image_same_size_is_caught_by_hash(cases, case_paths, verify):
    swap = cases["swapped_image"]
    _, original_root = case_paths("original")
    _, swapped_root = case_paths("swapped_image")
    # The size is the same, so only the hash can have caught it.
    assert (original_root / swap["path"]).stat().st_size == (swapped_root / swap["path"]).stat().st_size

    checks, _, code = verify("swapped_image")
    assert code == 1
    assert checks["evidence"].status == FAIL
    assert swap["path"] in checks["evidence"].lines
    assert checks["record"].status == PASS


def test_ts04_deleted_finding_names_the_count(cases, verify):
    deleted = cases["deleted_finding"]
    checks, _, code = verify("deleted_finding")
    assert code == 1
    assert checks["element counts"].status == FAIL
    assert f"findings: sealed {deleted['sealed']}, found {deleted['found']}" in checks["element counts"].lines


def test_ts05_reordered_array_is_tampered(cases, verify):
    checks, _, code = verify("reordered_array")
    assert code == 1
    assert checks["record"].status == FAIL
    path = cases["reordered_array"]["path"]
    assert any(line.startswith(f"{path}: reordered") for line in checks["record"].lines)
    # Every element is intact, so the counts still agree.
    assert checks["element counts"].status == PASS


def test_ts06_reserialised_record_is_intact(verify):
    _, line, code = verify("reserialised")
    assert code == 0
    assert line == "INTACT"


def test_ts07_missing_image_could_not_verify(cases, verify):
    checks, line, code = verify("missing_image")
    assert code == 2
    assert line.startswith("COULD NOT VERIFY")
    assert checks["evidence"].status == UNVERIFIED
    assert cases["missing_image"]["path"] in checks["evidence"].lines
    assert not any(check.status == FAIL for check in checks.values())


def test_unreadable_seal_could_not_verify(tmp_path, case_paths, capsys):
    seal = tmp_path / "seal_broken.json"
    seal.write_text("not json", encoding="utf-8")
    record, evidence_root = case_paths("original")

    with pytest.raises(VerifyError):
        verify_seal(seal, record, evidence_root)
    assert verify_main([str(seal), "--record", str(record), "--evidence-root", str(evidence_root)]) == 2


@pytest.mark.parametrize("case, expected", [("original", 0), ("edited_value", 1), ("missing_image", 2)])
def test_command_exit_codes(sealed, case_paths, case, expected, capsys):
    record, evidence_root = case_paths(case)
    code = verify_main([str(sealed), "--record", str(record), "--evidence-root", str(evidence_root)])
    assert code == expected


def test_ts08_verify_loads_no_report_engine():
    """Importing the verify command loads none of the report engine's packages."""
    script = (
        "import sys, seal.cli, seal.verify\n"
        "print(','.join(m for m in ('report', 'agents', 'yaml', 'PIL', 'weasyprint', 'jinja2') "
        "if m in sys.modules))"
    )
    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
    loaded = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert loaded == ""
