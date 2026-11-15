"""Check a seal against a record and its evidence.

Names exactly what moved: the field path and both values, the file, the
element count. Three answers: intact, tampered, could not verify.

Nothing is written and nothing is rendered. The files on disk are hashed
again with the functions seal used, and compared with the hashes the seal
holds. The report PDF and manifest are only read as bytes.

Uses only the standard library and the rest of src/seal, so it runs
without the report engine or the repo's config.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from seal.canonical import CanonicalError, canonical_bytes, canonical_hash, load_json
from seal.create import hash_file, record_copy_path, seal_path, seal_record

PASS = "PASS"
FAIL = "FAIL"
UNVERIFIED = "COULD NOT VERIFY"

INTACT, TAMPERED = 0, 1
COULD_NOT_VERIFY = 2

#: How much of a file hash is printed. The seal holds the whole of it.
SHORT_HASH = 8

#: The most lines diff_records prints, and the longest value it shows whole.
MAX_DIFF_LINES = 20
MAX_VALUE_CHARS = 60

#: A key one side has and the other does not. Kept apart from None, which is null.
_ABSENT = object()


class VerifyError(Exception):
    """The seal itself is missing, unreadable or incomplete. Nothing can be checked."""


@dataclass
class Check:
    """One of the five checks: its name, its status, and what it found."""

    name: str
    status: str
    lines: list[str] = field(default_factory=list)


def _short(sha256: str) -> str:
    return sha256.removeprefix("sha256:")[:SHORT_HASH] + "..."


# --- reading the seal -------------------------------------------------------


def _need(seal: dict, *keys: str) -> Any:
    """seal[keys[0]][keys[1]]..., or VerifyError naming the key that is missing."""
    value: Any = seal
    for depth, key in enumerate(keys):
        if not isinstance(value, dict) or key not in value:
            raise VerifyError(f"seal has no '{'.'.join(keys[:depth + 1])}'")
        value = value[key]
    return value


def read_seal(path: Path) -> dict:
    """The seal, with every part the checks read, or VerifyError."""
    try:
        seal = load_json(path)
    except CanonicalError as error:
        raise VerifyError(str(error)) from None
    if not isinstance(seal, dict):
        raise VerifyError(f"{path}: root is {type(seal).__name__}, expected a JSON object")

    for keys in (
        ("run_id",), ("record", "canonical_hash"), ("record", "element_counts"),
        ("evidence", "files"), ("evidence", "aggregate_hash"),
        ("outputs", "report_pdf"), ("outputs", "manifest_json"),
        ("chain", "previous_run_id"), ("chain", "sequence"),
    ):
        _need(seal, *keys)
    if not isinstance(seal["evidence"]["files"], list):
        raise VerifyError("seal's 'evidence.files' is not a list")
    return seal


# --- naming what changed in the record --------------------------------------


def _show(value: Any) -> str:
    """A value as JSON writes it, cut short if long. Absent is 'absent'."""
    if value is _ABSENT:
        return "absent"
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS - 3] + "..."


def _same(sealed: Any, found: Any) -> bool:
    """Equal in canonical form, so 20 and 20.0 differ, and so do 1 and true."""
    if sealed is _ABSENT or found is _ABSENT:
        return sealed is found
    return canonical_bytes(sealed) == canonical_bytes(found)


def _walk(sealed: Any, found: Any, path: str, out: list[str]) -> None:
    """Add a line to out for every place sealed and found differ."""
    if _same(sealed, found):
        return
    if isinstance(sealed, dict) and isinstance(found, dict):
        for key in sorted(set(sealed) | set(found)):
            _walk(sealed.get(key, _ABSENT), found.get(key, _ABSENT),
                  f"{path}.{key}" if path else key, out)
    elif isinstance(sealed, list) and isinstance(found, list):
        _walk_array(sealed, found, path, out)
    else:
        out.append(f"{path}: sealed {_show(sealed)}, found {_show(found)}")


def _walk_array(sealed: list, found: list, path: str, out: list[str]) -> None:
    """Name what differs between two arrays.

    Same items in a new order is one 'reordered' line: order is content. A
    change of length names the items removed and added, matched by canonical
    form, rather than walking positions that have all shifted. Same length
    with different items is walked position by position, down to the field.
    """
    sealed_items = Counter(canonical_bytes(item) for item in sealed)
    found_items = Counter(canonical_bytes(item) for item in found)

    if len(sealed) == len(found) and sealed_items == found_items:
        out.append(f"{path}: reordered, same {len(sealed)} items in a different order")
        return

    if len(sealed) != len(found):
        out.append(f"{path}: sealed {len(sealed)} items, found {len(found)}")
        removed = sealed_items - found_items
        added = found_items - sealed_items
        for index, item in enumerate(sealed):
            key = canonical_bytes(item)
            if removed[key]:
                removed[key] -= 1
                out.append(f"{path}[{index}]: sealed {_show(item)}, found absent")
        for index, item in enumerate(found):
            key = canonical_bytes(item)
            if added[key]:
                added[key] -= 1
                out.append(f"{path}[{index}]: sealed absent, found {_show(item)}")
        return

    for index, (before, after) in enumerate(zip(sealed, found)):
        _walk(before, after, f"{path}[{index}]", out)


def diff_records(sealed: Any, found: Any) -> list[str]:
    """Every difference between the sealed copy and the record found now.

    One line each, as 'path: sealed X, found Y'. At most MAX_DIFF_LINES, then
    a count of the rest.
    """
    out: list[str] = []
    _walk(sealed, found, "", out)
    if len(out) > MAX_DIFF_LINES:
        return out[:MAX_DIFF_LINES] + [f"... and {len(out) - MAX_DIFF_LINES} more"]
    return out


# --- the five checks --------------------------------------------------------


def _sealed_copy(folder: Path, run_id: str, sealed_hash: str) -> tuple[Any, str | None]:
    """The sealed copy of the record, or None and why it cannot be used.

    The copy is trusted only if its canonical hash is the one the seal holds.
    """
    path = record_copy_path(folder, run_id)
    if not path.is_file():
        return None, f"sealed copy {path.name} not found"
    try:
        copy = load_json(path)
    except CanonicalError as error:
        return None, f"sealed copy cannot be read: {error}"
    if canonical_hash(copy) != sealed_hash:
        return None, f"sealed copy {path.name} does not match the seal"
    return copy, None


def check_record(seal: dict, record: Any, record_error: str | None, folder: Path) -> Check:
    """The record's canonical hash against the sealed one."""
    if record_error is not None:
        return Check("record", UNVERIFIED, [record_error])

    sealed_hash = seal["record"]["canonical_hash"]
    if canonical_hash(record) == sealed_hash:
        return Check("record", PASS)

    lines = ["canonical hash mismatch"]
    copy, problem = _sealed_copy(folder, seal["run_id"], sealed_hash)
    if copy is None:
        lines.append(f"{problem}; the changed fields cannot be named")
    else:
        lines += diff_records(copy, record)
    return Check("record", FAIL, lines)


def check_evidence(seal: dict, evidence_root: Path) -> Check:
    """Each sealed evidence file against the file on disk.

    Decided by hash, never by size. A file missing now cannot be checked; a
    file missing at seal time that exists now has changed.
    """
    files = seal["evidence"]["files"]
    total = len(files)
    problems: list[str] = []     # wrong with the seal's own list
    altered: list[str] = []      # two lines per file: its path, then what differs
    missing: list[str] = []

    if canonical_hash(files) != seal["evidence"]["aggregate_hash"]:
        problems.append("the seal's file list does not match its aggregate_hash")

    for entry in files:
        path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(path, str):
            problems.append(f"the seal has an evidence entry with no path: {entry!r}")
            continue
        sealed = entry.get("sha256")
        found = hash_file(evidence_root / path)

        if sealed is None:
            if found is not None:
                altered += [path, f"missing at seal time, found {_short(found[0])}"]
        elif found is None:
            missing.append(path)
        elif found[0] != sealed:
            altered += [path, f"sealed {_short(sealed)}, found {_short(found[0])}"]

    missing_lines = [f"{len(missing)} of {total} files missing"] + missing if missing else []
    if problems or altered:
        lines = problems + ([f"{len(altered) // 2} of {total} files altered"] + altered if altered else [])
        return Check("evidence", FAIL, lines + missing_lines)
    if missing:
        return Check("evidence", UNVERIFIED, missing_lines)
    return Check("evidence", PASS)


def check_counts(seal: dict, record: Any, record_error: str | None) -> Check:
    """The five array counts against the sealed ones."""
    if record_error is not None:
        return Check("element counts", UNVERIFIED, [record_error])
    if not isinstance(record, dict):
        return Check("element counts", FAIL, [f"record is a {type(record).__name__}, not an object"])

    sealed = seal["record"]["element_counts"]
    found = seal_record(record)["element_counts"]
    lines = [
        f"{key}: sealed {sealed.get(key)}, found {found.get(key)}"
        for key in found if sealed.get(key) != found.get(key)
    ]
    return Check("element counts", FAIL, lines) if lines else Check("element counts", PASS)


def check_outputs(seal: dict, folder: Path) -> Check:
    """The report PDF and manifest beside the seal against their sealed hashes."""
    run_id = seal["run_id"]
    files = {
        "report_pdf": folder / f"report_{run_id}.pdf",
        "manifest_json": folder / f"report_{run_id}.manifest.json",
    }
    altered: list[str] = []
    missing: list[str] = []
    for key, path in files.items():
        found = hash_file(path)
        if found is None:
            missing.append(f"{path.name} not found")
        elif "sha256:" + found[0] != seal["outputs"][key]:
            altered += [path.name, f"sealed {_short(seal['outputs'][key])}, found {_short(found[0])}"]

    if altered:
        return Check("outputs", FAIL, altered + missing)
    if missing:
        return Check("outputs", UNVERIFIED, missing)
    return Check("outputs", PASS)


def find_previous_seal(folder: Path, run_id: str) -> Path | None:
    """The previous run's seal: beside this one, or in a sibling run folder."""
    beside = seal_path(folder, run_id)
    if beside.is_file():
        return beside
    for candidate in sorted(folder.parent.glob(f"*/{beside.name}")):
        if candidate.is_file():
            return candidate
    return None


def check_chain(seal: dict, folder: Path) -> Check:
    """The link to the previous run's seal."""
    chain = seal["chain"]
    previous_run_id = chain["previous_run_id"]
    sequence = chain["sequence"]

    if previous_run_id is None:
        if sequence != 1:
            return Check("chain", FAIL, [f"no previous run, but sequence is {sequence!r}, not 1"])
        return Check("chain", PASS, ["sequence 1, start of the chain"])

    if chain.get("chain_broken"):
        return Check("chain", UNVERIFIED, [f"sequence {sequence}, broken when sealed: {chain.get('reason')}"])

    path = find_previous_seal(folder, previous_run_id)
    if path is None:
        return Check("chain", UNVERIFIED, [f"sequence {sequence}, previous seal for {previous_run_id} not found"])
    try:
        previous = load_json(path)
    except CanonicalError as error:
        return Check("chain", UNVERIFIED, [f"sequence {sequence}, previous seal cannot be read: {error}"])

    if canonical_hash(previous) != chain.get("previous_seal_hash"):
        return Check("chain", FAIL, [f"sequence {sequence}, previous seal {path.name} has changed since this seal was made"])
    previous_chain = previous.get("chain") if isinstance(previous, dict) else None
    previous_sequence = previous_chain.get("sequence") if isinstance(previous_chain, dict) else None
    if previous_sequence != sequence - 1:
        return Check("chain", FAIL, [f"sequence {sequence}, previous seal has sequence {previous_sequence!r}"])
    return Check("chain", PASS, [f"sequence {sequence}, previous seal verified"])


def verify_chain(sealed_route: Path) -> tuple[str, list[Check]]:
    """Every seal of one route, in sequence order. Returns the route and one check per sequence.

    sealed_route is the route's sealed folder, <route>/<run_folder>/seal_<run_id>.json.
    Sequences run from 1 to the highest found, so a run deleted from the
    middle is a sequence with no seal. That hole is FAIL when the next seal
    was linked to the missing one, since the seal was there when that link
    was made. It is COULD NOT VERIFY when the next seal already recorded the
    break when it was sealed: the hole is real, but nothing proves it was
    made afterwards. Two seals with one sequence are FAIL.

    A run deleted from the end leaves no hole, so it cannot be seen here.

    Raises VerifyError when the folder holds no seal at all.
    """
    found: dict[int, list[tuple[Path, dict]]] = {}
    checks = []
    for path in sorted(sealed_route.glob("*/seal_*.json")):
        if path.name.endswith(".record.json"):
            continue
        try:
            seal = read_seal(path)
        except VerifyError as error:
            checks.append(Check(path.parent.name, UNVERIFIED, [f"seal cannot be read: {error}"]))
            continue
        sequence = seal["chain"]["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            checks.append(Check(path.parent.name, FAIL, [f"sequence is {sequence!r}, not a whole number of 1 or more"]))
            continue
        found.setdefault(sequence, []).append((path, seal))
    if not found and not checks:
        raise VerifyError(f"no seals in {sealed_route}")

    last = max(found, default=0)
    for sequence in range(1, last + 1):
        name = f"sequence {sequence}"
        seals = found.get(sequence, [])
        if len(seals) > 1:
            checks.append(Check(name, FAIL, ["more than one seal has this sequence"] + [p.parent.name for p, _ in seals]))
        elif seals:
            path, seal = seals[0]
            link = check_chain(seal, path.parent)
            checks.append(Check(name, link.status, [seal["run_id"], *link.lines]))
        else:
            following = next(found[n][0][1] for n in range(sequence + 1, last + 1) if n in found)
            if following["chain"].get("chain_broken"):
                checks.append(Check(name, UNVERIFIED, [
                    "no seal with this sequence", "the next seal recorded the break when it was sealed",
                ]))
            else:
                checks.append(Check(name, FAIL, [
                    "no seal with this sequence", "the next seal was linked to it, so it was removed later",
                ]))
    return sealed_route.name, checks


# --- the result -------------------------------------------------------------


def verify_seal(seal_file: Path, record_path: Path, evidence_root: Path) -> tuple[str, list[Check]]:
    """Run the five checks. Returns the run_id and the checks.

    Raises VerifyError when the seal itself cannot be used.
    """
    seal = read_seal(seal_file)
    folder = seal_file.parent

    record: Any = None
    record_error = None
    if not record_path.is_file():
        record_error = f"record {record_path} not found"
    else:
        try:
            record = load_json(record_path)
        except CanonicalError as error:
            record_error = f"record cannot be read: {error}"

    checks = [
        check_record(seal, record, record_error, folder),
        check_evidence(seal, evidence_root),
        check_counts(seal, record, record_error),
        check_outputs(seal, folder),
        check_chain(seal, folder),
    ]
    return seal["run_id"], checks


def result(checks: list[Check]) -> tuple[str, int]:
    """The overall result line and its exit code.

    A failed check is proof something changed, so it outranks one that could
    not be checked.
    """
    failed = sum(1 for check in checks if check.status == FAIL)
    unverified = sum(1 for check in checks if check.status == UNVERIFIED)
    if failed:
        return f"TAMPERED — {failed} of {len(checks)} checks failed", TAMPERED
    if unverified:
        return f"COULD NOT VERIFY — {unverified} of {len(checks)} checks could not be verified", COULD_NOT_VERIFY
    return "INTACT", INTACT


def format_report(run_id: str, checks: list[Check], result_line: str, title: str = "SEAL VERIFICATION") -> str:
    """The verification report, in the layout an auditor reads."""
    lines = [f"{title} — {run_id}"]
    for check in checks:
        first, *rest = check.lines or [""]
        lines.append(f"  {check.name:<18}{check.status:<7}  {first}".rstrip())
        lines += [f"{'':<29}{line}" for line in rest]
    lines += ["", f"RESULT: {result_line}"]
    return "\n".join(lines)
