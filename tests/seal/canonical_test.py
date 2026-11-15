"""The canonical form: what changes the hash and what does not.

Small JSON values only, no run records: these check the rules of the form
itself. TS-06 against a real record is in verify_test.py.
"""

import json

import pytest

from seal.canonical import CanonicalError, canonical_bytes, canonical_hash, load_json


def test_key_order_and_whitespace_do_not_change_the_hash():
    a = json.loads('{"a": 1, "b": {"c": [1, 2], "d": "x"}}')
    b = json.loads('{\n    "b": {"d": "x", "c": [1, 2]},\n    "a": 1\n}')
    assert canonical_hash(a) == canonical_hash(b)


def test_array_order_changes_the_hash():
    assert canonical_hash([1, 2, 3]) != canonical_hash([1, 3, 2])


def test_absent_and_null_differ():
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 1, "b": None})


def test_int_and_float_differ():
    assert canonical_hash({"a": 20}) != canonical_hash({"a": 20.0})


def test_form_is_sorted_compact_and_utf8():
    assert canonical_bytes({"b": "é", "a": [1, 2]}) == '{"a":[1,2],"b":"é"}'.encode("utf-8")


def test_floats_keep_full_precision():
    assert canonical_bytes(0.1 + 0.2) == b"0.30000000000000004"


@pytest.mark.parametrize("text", [
    '{"a": 1, "a": 2}',     # a second value hidden behind the first
    '{"a": NaN}',           # not JSON
    '{"a": 1e400}',         # reads as infinity
])
def test_load_json_refuses(tmp_path, text):
    path = tmp_path / "bad.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(CanonicalError):
        load_json(path)
