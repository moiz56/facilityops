"""Version stamping: what produced a report, and when.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from common.paths import setting

@dataclass(frozen=True)
class Provenance:
    """The four facts 5.6 requires of every report."""

    engine_version: str
    template_version: str
    generated_at: str       # ISO 8601, UTC
    source_run_id: str


def stamp(run_id: str, config: dict) -> Provenance:
    """Record what is producing this report, and when.
    """
    return Provenance(
        engine_version=setting(config, "provenance", "engine_version"),
        template_version=setting(config, "provenance", "template_version"),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        source_run_id=run_id,
    )
