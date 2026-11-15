"""The chain: TS-09 to TS-11, and the link itself.

TS-11 seals the real stretch3 route, three runs, into tests/output/sealed/
and checks copies of that sealed folder with verify_chain: as sealed, with
the middle run deleted, and with the last run deleted.
"""

import shutil
from pathlib import Path

import pytest

from seal.canonical import canonical_hash, load_json
from seal.cli import verify_main
from seal.create import seal_chain
from seal.verify import COULD_NOT_VERIFY, FAIL, INTACT, PASS, TAMPERED, UNVERIFIED, result, verify_chain
from tests.helpers import sealed_route

CHAIN_ROUTE = "stretch3_checkpoint_route"


def test_ts09_first_run_starts_the_chain(sealed):
    chain = load_json(sealed)["chain"]
    assert chain == {"previous_run_id": None, "previous_seal_hash": None, "sequence": 1}


def test_ts10_missing_previous_seal_is_a_break(tmp_path):
    chain = seal_chain(2, "previous_run", tmp_path / "seal_previous_run.json")
    assert chain["chain_broken"] is True
    assert chain["reason"]
    assert chain["previous_seal_hash"] is None
    # Not re-chained: it still names the run before it, not an older one.
    assert chain["previous_run_id"] == "previous_run"
    assert chain["sequence"] == 2


def test_previous_seal_found_is_linked(cases, sealed):
    chain = seal_chain(2, cases["run_id"], sealed)
    assert "chain_broken" not in chain
    assert chain["previous_seal_hash"] == canonical_hash(load_json(sealed))


def test_previous_seal_with_wrong_sequence_is_a_break(cases, sealed):
    chain = seal_chain(3, cases["run_id"], sealed)
    assert chain["chain_broken"] is True
    assert chain["previous_seal_hash"] is None


# TS-11

@pytest.fixture
def chain_copy(tmp_path):
    """A copy of the sealed stretch3 route, safe to delete runs from."""
    copy = tmp_path / CHAIN_ROUTE
    shutil.copytree(sealed_route(CHAIN_ROUTE), copy)
    return copy


def run_folder_with_sequence(sealed: Path, sequence: int) -> Path:
    """The sealed run folder whose seal has this sequence."""
    for path in sealed.glob("*/seal_*.json"):
        if not path.name.endswith(".record.json") and load_json(path)["chain"]["sequence"] == sequence:
            return path.parent
    raise AssertionError(f"no seal with sequence {sequence}")


def test_ts11_chain_as_sealed_is_intact(chain_copy):
    route, checks = verify_chain(chain_copy)
    assert route == CHAIN_ROUTE
    assert [c.name for c in checks] == ["sequence 1", "sequence 2", "sequence 3"]
    assert all(c.status == PASS for c in checks), [(c.name, c.lines) for c in checks]
    assert result(checks)[1] == INTACT


def test_ts11_run_deleted_mid_sequence_is_a_hole(chain_copy, capsys):
    deleted = run_folder_with_sequence(chain_copy, 2)
    shutil.rmtree(deleted)

    _, checks = verify_chain(chain_copy)
    by_name = {c.name: c for c in checks}
    assert [c.name for c in checks] == ["sequence 1", "sequence 2", "sequence 3"]
    assert by_name["sequence 1"].status == PASS
    # The hole is named from the sequence. The next seal was linked to it, so
    # it was there when sealed and was removed afterwards.
    assert by_name["sequence 2"].status == FAIL
    assert "no seal with this sequence" in by_name["sequence 2"].lines
    # Not re-chained past the hole: the run after it still names the deleted run.
    assert by_name["sequence 3"].status == UNVERIFIED
    assert any(deleted.name in line for line in by_name["sequence 3"].lines)
    assert result(checks)[1] == TAMPERED

    assert verify_main(["--chain", str(chain_copy)]) == TAMPERED
    printed = capsys.readouterr().out
    assert "CHAIN VERIFICATION" in printed and "sequence 2" in printed and "RESULT: TAMPERED" in printed


def test_run_deleted_from_the_end_leaves_no_hole(chain_copy):
    """The limit: with nothing after it, a deleted last run cannot be seen in the sequence."""
    shutil.rmtree(run_folder_with_sequence(chain_copy, 3))
    _, checks = verify_chain(chain_copy)
    assert [c.name for c in checks] == ["sequence 1", "sequence 2"]
    assert result(checks)[1] == INTACT


def test_chain_of_an_empty_folder_could_not_verify(tmp_path):
    assert verify_main(["--chain", str(tmp_path)]) == COULD_NOT_VERIFY
