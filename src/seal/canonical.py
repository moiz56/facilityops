"""Canonical form of a record, the input to every hash.

Keys sorted at every level, no spaces after separators, UTF-8 not escaped,
floats at full repr, arrays never sorted, absent kept apart from null.

The raw JSON is hashed, never a loaded Record: the loader reads absent and
null as the same thing, and here they are different.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class CanonicalError(Exception):
    """The file cannot be put in canonical form. The message names the problem."""


def _refuse_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build an object, refusing a key that appears twice.

    json.loads keeps the last value of a repeated key without a word, so a
    second value could hide behind the first.
    """
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalError(f"key {key!r} appears more than once in one object")
        result[key] = value
    return result


def _refuse_constant(name: str) -> None:
    """NaN, Infinity and -Infinity are not JSON."""
    raise CanonicalError(f"{name} is not a JSON number")


def _parse_float(text: str) -> float:
    """Read a JSON number with a fraction or exponent.

    A number too large for a float, like 1e400, reads as infinity. Refuse it.
    """
    value = float(text)
    if value in (float("inf"), float("-inf")):
        raise CanonicalError(f"the number {text} is too large to read exactly")
    return value


def load_json(path: Path) -> Any:
    """Read a JSON file strictly, or raise CanonicalError naming the problem."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise CanonicalError(f"{path}: cannot be read: {error.strerror or error}") from None
    except UnicodeDecodeError as error:
        raise CanonicalError(f"{path}: is not UTF-8 text: {error}") from None

    try:
        return json.loads(
            text,
            object_pairs_hook=_refuse_duplicates,
            parse_constant=_refuse_constant,
            parse_float=_parse_float,
        )
    except json.JSONDecodeError as error:
        raise CanonicalError(
            f"{path}: is not valid JSON: {error.msg} at line {error.lineno} column {error.colno}"
        ) from None
    except CanonicalError as error:
        raise CanonicalError(f"{path}: {error}") from None


def canonical_bytes(value: Any) -> bytes:
    """The canonical form of parsed JSON, as UTF-8 bytes.

    Anything that is not plain JSON, like a date read from YAML, is refused
    rather than turned into text here. The caller decides how to write it.
    """
    try:
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise CanonicalError(f"not plain JSON: {error}") from None
    return text.encode("utf-8")


def canonical_hash(value: Any) -> str:
    """sha256 of the canonical form, written as 'sha256:<hex>'."""
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()
