"""could_not_read: things that were captured but could not be read.

Not a gap - the evidence is there - and not a finding - nothing was called.
Each reason in config lists the finding statuses that produce it. A reason
code is used exactly as config writes it, so a new one is reported as
written rather than bucketed into a known one.

needs_review is not a producer: it means nobody has looked yet. Images that
are empty or will not decode stay MISSING_IMAGE gaps.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from common.paths import ConfigError, setting
from common.schema import Record


@dataclass(frozen=True)
class CouldNotRead:
    """One thing that was attempted and could not be read."""

    item_id: str | None        # the checkpoint_id, None when the finding names none
    subject: str | None        # what was being read: the finding's feature
    reason: str                # a reason code from config, as written there
    detail: str                # the sentence the reader sees
    capture_id: str | None     # the evidence image's file name without its extension
    attempted_at: str | None   # ISO timestamp of the finding


def reason_by_status(config: dict) -> dict[str, str]:
    """Map each producing finding status to its reason code.

    Raises ConfigError when the reasons block is missing or malformed, or when
    two reasons claim the same status.
    """
    reasons = setting(config, "could_not_read", "reasons")
    if not isinstance(reasons, dict) or not reasons:
        raise ConfigError("'could_not_read.reasons' must be a non-empty mapping")

    by_status: dict[str, str] = {}
    for code, entry in reasons.items():
        statuses = entry.get("finding_status") if isinstance(entry, dict) else None
        if not isinstance(statuses, list):
            raise ConfigError(f"'could_not_read.reasons.{code}.finding_status' must be a list")
        for status in statuses:
            if status in by_status:
                raise ConfigError(
                    f"finding status {status!r} is claimed by both {by_status[status]} and {code}"
                )
            by_status[status] = code
    return by_status


def could_not_read(record: Record, config: dict) -> list[CouldNotRead]:
    """Every run-level finding whose status produces a could_not_read reason.

    Checkpoint detections are not read. If they carry the same finding,
    reading both would count one abstention twice.
    """
    by_status = reason_by_status(config)
    entries = []
    for finding in record.findings:
        reason = by_status.get(finding.status)
        if reason is None:
            continue
        image = finding.evidence_image
        entries.append(CouldNotRead(
            item_id=finding.checkpoint_id,
            subject=finding.feature,
            reason=reason,
            detail=finding.description or "no description recorded",
            capture_id=PurePosixPath(image).stem if image else None,
            attempted_at=finding.timestamp.isoformat() if finding.timestamp else None,
        ))
    return entries
