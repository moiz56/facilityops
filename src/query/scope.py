"""Tenant resolution 

The only module that takes a tenant_id or loads records. A QueryContext
resolves to the records of the tenant's facilities before any routing,
retrieval or generation happens.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from agents.data_agent import load_corpus
from common.schema import Record


@dataclass(frozen=True)
class QueryContext:
    """Who is asking, and over what. Arrives already authenticated"""

    tenant_id: str
    facility_ids: tuple[str, ...]           # empty = every facility the tenant owns
    run_window: tuple[str, str] | None      # ISO dates, inclusive; None = whole corpus
    asked_by: str
    asked_at: str


class ScopeError(Exception):
    """The context does not resolve to a scope, so nothing is answered."""


def tenant_facilities(tenants: dict, tenant_id: str) -> set[str]:
    """The facility_ids a tenant owns, or ScopeError.

    The whole mapping is checked, not just the tenant asked about: a facility
    listed under two tenants is ambiguous and refused.
    """
    mapping = tenants.get("tenants")
    if not isinstance(mapping, dict) or not mapping:
        raise ScopeError("tenants.yaml: 'tenants' must be a non-empty mapping")

    owner: dict[str, str] = {}
    for tenant, entry in mapping.items():
        ids = entry.get("facility_ids") if isinstance(entry, dict) else None
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
            raise ScopeError(f"tenants.yaml: '{tenant}.facility_ids' must be a non-empty list of ids")
        for facility_id in ids:
            if facility_id in owner:
                raise ScopeError(
                    f"tenants.yaml: facility '{facility_id}' is mapped to both "
                    f"'{owner[facility_id]}' and '{tenant}'"
                )
            owner[facility_id] = tenant

    if tenant_id not in mapping:
        raise ScopeError(f"unknown tenant '{tenant_id}'")
    return set(mapping[tenant_id]["facility_ids"])


def resolve(context: QueryContext, tenants: dict, root: Path) -> list[Record]:
    """The records this context may see, oldest first, and nothing else.

    Scope first: every record outside the tenant's facilities and the run
    window is dropped here, so nothing downstream ever receives it.
    """
    owned = tenant_facilities(tenants, context.tenant_id)
    foreign = set(context.facility_ids) - owned
    if foreign:
        raise ScopeError(f"tenant '{context.tenant_id}' does not own {sorted(foreign)}")
    allowed = set(context.facility_ids) or owned

    window = None
    if context.run_window is not None:
        try:
            window = tuple(date.fromisoformat(day) for day in context.run_window)
        except ValueError:
            raise ScopeError(f"run_window must be two ISO dates, got {context.run_window!r}") from None

    data_dir = tenants.get("data_dir")
    patterns = tenants.get("record_patterns")
    if not isinstance(data_dir, str) or not data_dir:
        raise ScopeError("tenants.yaml: 'data_dir' must be a path")
    if not isinstance(patterns, list) or not patterns:
        raise ScopeError("tenants.yaml: 'record_patterns' must be a non-empty list")

    return [
        record
        for record in load_corpus(root / data_dir, patterns)
        if record.facility_id in allowed and in_window(record, window)
    ]


def in_window(record: Record, window: tuple[date, date] | None) -> bool:
    """Whether the run started inside the window, by its recorded local date.

    A run with no start_time cannot be placed in a window, so it is outside one.
    """
    if window is None:
        return True
    if record.start_time is None:
        return False
    return window[0] <= record.start_time.date() <= window[1]
