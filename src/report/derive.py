"""Values computed from a run: the reconciliation counts.

Zone telemetry rollups and alert counts come later.

The seven counts here are the two halves of the coverage page. Declared is what
the robot reported, copied verbatim and never corrected. Computed is what the
arrays actually hold. Where a pair disagrees the report prints both and flags
it, which is gaps.py's job, not this module's.
"""

from __future__ import annotations

from dataclasses import dataclass

from common.schema import Record

__all__ = ["Counts", "declared_counts", "computed_counts", "alert_counts"]


@dataclass(frozen=True)
class Counts:
    """The seven counts of the reconciliation table, named as the manifest names them.

    Declared counts can be None, because a record is allowed to leave one out.
    Computed counts never are.
    """

    required: int | None
    completed: int | None
    passed: int | None
    failed: int | None
    missed: int | None
    warned: int | None
    findings: int | None


def declared_counts(record: Record) -> Counts:
    """What the record claims. Copied as written, never corrected."""
    return Counts(
        required=record.total_required_checkpoints,
        completed=record.total_completed_checkpoints,
        passed=record.passed_checkpoints,
        failed=record.failed_checkpoints,
        missed=record.missed_checkpoints,
        warned=record.warned_checkpoints,
        findings=record.finding_count,
    )


def computed_counts(record: Record) -> Counts:
    """What the arrays actually contain."""
    checkpoints = record.checkpoints
    return Counts(
        required=len(checkpoints),
        completed=sum(1 for c in checkpoints if c.status == "COMPLETED"),
        passed=sum(1 for c in checkpoints if c.result_status == "PASS"),
        failed=sum(1 for c in checkpoints if c.result_status == "FAIL"),
        missed=sum(1 for c in checkpoints if c.status == "MISSED"),
        warned=sum(1 for c in checkpoints if c.result_status == "WARN"),
        findings=len(record.findings),
    )


def alert_counts(record: Record) -> dict[str, int]:
    """How many sensor alerts of each severity the run logged.

    Severity is whatever the record says. An unlisted one is counted under its
    own name rather than folded into a known one.
    """
    counts: dict[str, int] = {}
    for alert in record.sensor_alerts:
        counts[alert.severity] = counts.get(alert.severity, 0) + 1
    return dict(sorted(counts.items()))
