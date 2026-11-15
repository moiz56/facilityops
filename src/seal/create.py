"""Seal a run: write seal_<run_id>.json next to the report and manifest.

Hashes the record, every evidence file and the outputs, and links to the
previous run's seal. A missing previous seal is a chain break, reported,
never re-chained.

Everything here reads the raw record JSON, as load_json returns it, never a
loaded Record.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from seal.canonical import CanonicalError, canonical_bytes, canonical_hash, load_json

SEAL_VERSION = "1.0"

#: The arrays element_counts covers.
COUNTED_ARRAYS = ("checkpoints", "findings", "sensor_alerts", "sensor_samples", "event_log")


class SealError(Exception):
    """The run cannot be sealed. The message names the problem."""


def _list(value: Any) -> list:
    """The value if it is an array, else an empty one. The canonical hash covers the rest."""
    return value if isinstance(value, list) else []


def _objects(value: Any) -> list[dict]:
    """The objects in an array, skipping anything that is not one."""
    return [entry for entry in _list(value) if isinstance(entry, dict)]


def seal_record(record: dict) -> dict:
    """The seal's record block: the canonical hash and the element counts.

    An absent array counts as 0. Absent against empty is still caught by the
    canonical hash.
    """
    return {
        "canonical_hash": canonical_hash(record),
        "element_counts": {key: len(_list(record.get(key))) for key in COUNTED_ARRAYS},
    }


def evidence_paths(record: dict, prefix: str) -> list[str]:
    """Every evidence file the record names, relative to the evidence root.

    The prefix comes off the recorded path the same way common/paths.py does
    it. A path named twice is kept once, at its first place. A blank or
    non-text entry is skipped; the canonical hash covers it.
    """
    recorded: list[Any] = []
    for checkpoint in _objects(record.get("checkpoints")):
        recorded += _list(checkpoint.get("evidence_images"))
        recorded += _list(checkpoint.get("annotated_images"))
        recorded += [d.get("evidence_image") for d in _objects(checkpoint.get("detections"))]
    for key in ("findings", "live_detections"):
        recorded += [f.get("evidence_image") for f in _objects(record.get(key))]

    relative = []
    for uri in recorded:
        if not isinstance(uri, str) or not uri.strip():
            continue
        uri = uri.strip()
        uri = uri[len(prefix):] if uri.startswith(prefix) else uri
        relative.append(uri.lstrip("/"))
    return list(dict.fromkeys(relative))


def hash_file(path: Path) -> tuple[str, int] | None:
    """The file's sha256 and size in bytes, or None if it is missing or cannot be read."""
    try:
        with path.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256")
        return digest.hexdigest(), path.stat().st_size
    except OSError:
        return None


def seal_evidence(record: dict, evidence_root: Path, prefix: str) -> dict:
    """The seal's evidence block: one entry per file, and a hash over the list.

    A file missing at seal time stays in the list with sha256 and bytes null,
    so the gap is sealed rather than dropped. The list keeps record order, so
    the aggregate hash changes if files move between checkpoints.
    """
    files = []
    for path in evidence_paths(record, prefix):
        hashed = hash_file(evidence_root / path)
        sha256, size = hashed if hashed else (None, None)
        files.append({"path": path, "sha256": sha256, "bytes": size})

    return {
        "file_count": len(files),
        "files": files,
        "aggregate_hash": canonical_hash(files),
    }


def run_id_of(record: dict) -> str:
    """The record's run_id, which names the seal file."""
    run_id = record.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise SealError(f"run_id is {run_id!r}; without it the seal file cannot be named")
    return run_id.strip()


def seal_path(output_dir: Path, run_id: str) -> Path:
    """Where the seal for run_id is written."""
    return output_dir / f"seal_{run_id}.json"


def record_copy_path(output_dir: Path, run_id: str) -> Path:
    """Where the sealed copy of run_id's record is written."""
    return output_dir / f"seal_{run_id}.record.json"


def seal_outputs(pdf_path: Path, manifest_path: Path) -> dict:
    """The seal's outputs block: the hash of the report PDF and of its manifest.

    Both are made just before sealing, so a missing one is an error, not a gap.
    """
    outputs = {}
    for key, path in (("report_pdf", pdf_path), ("manifest_json", manifest_path)):
        hashed = hash_file(path)
        if hashed is None:
            raise SealError(f"{path}: {key} is missing or cannot be read")
        outputs[key] = "sha256:" + hashed[0]
    return outputs


def _broken(chain: dict, reason: str) -> dict:
    """A chain block marked broken, with the reason."""
    return {**chain, "chain_broken": True, "reason": reason}


def seal_chain(sequence: int, previous_run_id: str | None, previous_seal: Path | None) -> dict:
    """The seal's chain block: the link to the previous run's seal.

    The first run of a route has no previous run and sequence 1, a valid start.
    A previous seal that is missing, unreadable or does not match is a break,
    recorded with its reason. It is never re-linked to an older seal: that
    would hide the gap.
    """
    chain = {"previous_run_id": previous_run_id, "previous_seal_hash": None, "sequence": sequence}
    if previous_run_id is None:
        return chain

    if previous_seal is None or not previous_seal.is_file():
        return _broken(chain, f"no seal for {previous_run_id} at {previous_seal}")
    try:
        previous = load_json(previous_seal)
    except CanonicalError as error:
        return _broken(chain, f"previous seal cannot be read: {error}")

    if not isinstance(previous, dict) or previous.get("run_id") != previous_run_id:
        return _broken(chain, f"{previous_seal} is not the seal for {previous_run_id}")
    previous_chain = previous.get("chain")
    previous_sequence = previous_chain.get("sequence") if isinstance(previous_chain, dict) else None
    if previous_sequence != sequence - 1:
        return _broken(
            chain, f"previous seal has sequence {previous_sequence!r}, expected {sequence - 1}",
        )

    chain["previous_seal_hash"] = canonical_hash(previous)
    return chain


def build_seal(
    record: dict, evidence_root: Path, prefix: str, sealed_by: str,
    pdf_path: Path, manifest_path: Path, chain: dict, configs: dict,
) -> dict:
    """The seal for one run, in the brief's key order.

    configs maps each config file the render read to its parsed values. The
    file names are hashed with the values, so a value moved from one file to
    another changes config_hash.
    """
    return {
        "seal_version": SEAL_VERSION,
        "run_id": run_id_of(record),
        "sealed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sealed_by": sealed_by,
        "algorithm": "sha256",
        "record": seal_record(record),
        "evidence": seal_evidence(record, evidence_root, prefix),
        "outputs": seal_outputs(pdf_path, manifest_path),
        "chain": chain,
        "config_hash": canonical_hash(configs),
    }


def write_record_copy(record: dict, output_dir: Path) -> Path:
    """Write the record in canonical form beside the seal and return its path.

    Only verify reads it, to name the fields that changed. It needs no hash of
    its own: its canonical hash must equal record.canonical_hash in the seal,
    so that one hash covers the record and the copy.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = record_copy_path(output_dir, run_id_of(record))
    path.write_bytes(canonical_bytes(record))
    return path


def write_seal(seal: dict, output_dir: Path) -> Path:
    """Write seal_<run_id>.json into output_dir and return its path.

    Indented for reading. Whatever later hashes this file hashes its
    canonical form, so the layout does not matter.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = seal_path(output_dir, seal["run_id"])
    path.write_text(json.dumps(seal, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
