"""Version stamping: what produced a report, and when.

5.6 requires the same four facts in every page footer and in the manifest. Both
read them from here, so neither can claim a different engine version from the
other.

The two versions are config values rather than constants in this file. A
release changes one line of config/report.yaml and the footer, the manifest and
the config hash all follow it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from common.paths import setting

__all__ = ["Provenance", "stamp"]


@dataclass(frozen=True)
class Provenance:
    """The four facts 5.6 requires of every report."""

    engine_version: str
    template_version: str
    generated_at: str       # ISO 8601, UTC
    source_run_id: str


def stamp(run_id: str, config: dict) -> Provenance:
    """Record what is producing this report, and when.

    The versions come from the config that was actually applied, so a report
    cannot carry one engine version in its footer and another in its manifest.
    A version the config does not declare is an error naming the key, never a
    silent default that would stamp a wrong number onto every page (5.7).

    The timestamp is the moment of rendering, in UTC, so two reports of the same
    run are told apart by when they were made rather than by when the run was.
    """
    return Provenance(
        engine_version=setting(config, "provenance", "engine_version"),
        template_version=setting(config, "provenance", "template_version"),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        source_run_id=run_id,
    )
