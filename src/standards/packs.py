"""Load the enabled standards packs from config/standards/.

The packs are supplied by the client. A clause mapping is never authored
or inferred here: a clause maps the features its pack lists and no others.

One file per pack, named <pack_id>.yaml. A pack is refused, never guessed,
when a required key is missing or a value has the wrong type. Keys this
loader does not know are ignored, so a pack can carry more than it reads.
A clause with no maps_to_features is kept: no finding can map to it, so it
always reads not assessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from common import PROJECT_ROOT
from common.paths import ConfigError, load_config

STANDARDS_DIR = PROJECT_ROOT / "config" / "standards"


@dataclass(frozen=True)
class Clause:
    """One clause of a pack, as the client wrote it."""

    clause: str                             # the clause reference, e.g. "11.3.2"
    summary: str | None
    maps_to_features: tuple[str, ...]       # finding features that map to it
    evidence_required: tuple[str, ...]      # evidence kinds it asks for


@dataclass(frozen=True)
class Pack:
    """One standards pack, with its clauses in file order."""

    pack_id: str
    pack_version: str
    title: str
    clauses: tuple[Clause, ...]


def text(value, where: str) -> str:
    """A non-empty string, or ConfigError naming where.

    A whole number is taken as its digits. A decimal is refused: YAML reads
    11.10 as 11.1, so the reference would already be wrong.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} must be text in quotes, got {value!r}")
    return value.strip()


def names(value, where: str) -> tuple[str, ...]:
    """A list of non-empty strings, empty when absent, or ConfigError naming where."""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{where} must be a list of names, got {value!r}")
    return tuple(v.strip() for v in value)


def load_pack(path: Path) -> Pack:
    """Read one pack file, or raise ConfigError saying what is wrong with it.

    pack_id must match the file name, so the file a customer enables is the
    pack they get. A clause listed twice is refused.
    """
    config = load_config(path)

    pack_id = text(config.get("pack_id"), f"{path}: 'pack_id'")
    if pack_id != path.stem:
        raise ConfigError(f"{path}: 'pack_id' is {pack_id!r} but the file is named {path.name}")

    clauses = config.get("clauses")
    if not isinstance(clauses, list) or not clauses:
        raise ConfigError(f"{path}: 'clauses' must be a non-empty list")

    result, seen = [], set()
    for k, entry in enumerate(clauses):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: clauses[{k}] must be a mapping, got {entry!r}")
        ref = text(entry.get("clause"), f"{path}: clauses[{k}].clause")
        if ref in seen:
            raise ConfigError(f"{path}: clause {ref!r} is listed twice")
        seen.add(ref)
        summary = entry.get("summary")
        result.append(Clause(
            clause=ref,
            summary=None if summary is None else text(summary, f"{path}: clause {ref!r} summary"),
            maps_to_features=names(entry.get("maps_to_features"), f"{path}: clause {ref!r} maps_to_features"),
            evidence_required=names(entry.get("evidence_required"), f"{path}: clause {ref!r} evidence_required"),
        ))

    return Pack(
        pack_id=pack_id,
        pack_version=text(config.get("pack_version"), f"{path}: 'pack_version'"),
        title=text(config.get("title"), f"{path}: 'title'"),
        clauses=tuple(result),
    )


def pack_path(pack_id: str, standards_dir: Path = STANDARDS_DIR) -> Path:
    """Where one pack's file lives."""
    return standards_dir / f"{pack_id}.yaml"


def enabled_packs(tenants: dict, facility_id: str) -> list[str]:
    """The standards_packs of the tenant whose facility_ids list this facility.

    A tenant with no standards_packs, or a facility no tenant lists, has none
    enabled.
    """
    for tenant, entry in (tenants.get("tenants") or {}).items():
        if isinstance(entry, dict) and facility_id in (entry.get("facility_ids") or []):
            pack_ids = entry.get("standards_packs") or []
            if not isinstance(pack_ids, list):
                raise ConfigError(f"tenants.yaml: '{tenant}.standards_packs' must be a list, got {pack_ids!r}")
            return pack_ids
    return []


def load_packs(pack_ids: list[str], standards_dir: Path = STANDARDS_DIR) -> list[Pack]:
    """The enabled packs, in the order given, or ConfigError.

    Each is read once from standards_dir/<pack_id>.yaml. A pack enabled twice
    or with no file is refused. No packs enabled is an empty list: the report
    then has no standards section.
    """
    if not isinstance(pack_ids, list):
        raise ConfigError(f"the enabled standards packs must be a list, got {pack_ids!r}")

    packs, seen = [], set()
    for pack_id in pack_ids:
        pack_id = text(pack_id, "an enabled standards pack")
        if pack_id in seen:
            raise ConfigError(f"standards pack {pack_id!r} is enabled twice")
        seen.add(pack_id)
        path = pack_path(pack_id, standards_dir)
        if not path.exists():
            raise ConfigError(f"standards pack {pack_id!r} is enabled but {path} does not exist")
        packs.append(load_pack(path))
    return packs
