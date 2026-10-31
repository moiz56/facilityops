"""Row sign-off: how much of each row a run can actually tell us about.

confidence = readable_subjects / expected_subjects. It is coverage, not
condition: a row where everything was read and everything failed has
confidence 1.0.

A subject is one checkpoint the row lists. Each one is counted in exactly one
place, the first of these that applies:

    missed_checkpoints    not in the run, or the robot did not complete it
    unresolved_captures   reached, but not one of its images is a readable file
    unscoreable_captures  its images are fine, but it has no verdict
    could_not_read        a could_not_read entry names it
    abstained             one of its own detections abstained
    readable              none of the above

so expected - readable always equals the shortfall total. could_not_read is
checked before abstained: an abstained run-level finding is already a
could_not_read entry, and a checkpoint is counted once.

The settings and the statuses each rule reads are the signoff block of
report.yaml; the order of the checks is fixed here. Rows are read from one
file per facility. A facility with no file is not signed off.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from common.paths import ConfigError, ResolvedImage, load_config, setting
from common.readability import CouldNotRead
from common.schema import Checkpoint, Record

SHORTFALL = (
    "missed_checkpoints", "unresolved_captures", "unscoreable_captures", "could_not_read", "abstained",
)


@dataclass(frozen=True)
class Settings:
    """The signoff block of report.yaml."""

    threshold: float
    rows_dir: Path
    completed_status: str               # a checkpoint with any other status is missed
    verdicts: tuple[str, ...]           # a result_status outside these is unscoreable
    abstained_statuses: tuple[str, ...]  # a detection status that is an abstention


@dataclass(frozen=True)
class Row:
    """One row from a facility's file."""

    row_id: str
    checkpoints: tuple[str, ...]
    expected_subjects: int


@dataclass(frozen=True)
class Uncovered:
    """One subject that was not readable, and why. For the report's wording."""

    checkpoint_id: str
    category: str              # one of SHORTFALL
    reason: str


@dataclass(frozen=True)
class RowSignOff:
    """One row's sign-off for one run."""

    row_id: str
    run_id: str
    signed_off_at: str
    confidence: float
    confidence_formatted: str
    expected_subjects: int
    readable_subjects: int
    shortfall: dict[str, int]
    below_threshold: bool
    threshold: float
    uncovered: tuple[Uncovered, ...]

    def output(self) -> dict:
        """The sign-off as it is written out: every field but uncovered."""
        return {
            "row_id": self.row_id,
            "run_id": self.run_id,
            "signed_off_at": self.signed_off_at,
            "confidence": self.confidence,
            "confidence_formatted": self.confidence_formatted,
            "expected_subjects": self.expected_subjects,
            "readable_subjects": self.readable_subjects,
            "shortfall": dict(self.shortfall),
            "below_threshold": self.below_threshold,
            "threshold": self.threshold,
        }


def settings(config: dict, root: Path) -> Settings:
    """The signoff block of report.yaml, or ConfigError naming what is wrong.

    rows_dir is relative to root. shortfall must name exactly the five
    categories, so none is left without its meaning and none is invented.
    """
    threshold = setting(config, "signoff", "threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold <= 1:
        raise ConfigError(f"'signoff.threshold' must be a number above 0 and at most 1, got {threshold!r}")

    rows_dir = setting(config, "signoff", "rows_dir")
    if not isinstance(rows_dir, str) or not rows_dir:
        raise ConfigError(f"'signoff.rows_dir' must be a path, got {rows_dir!r}")

    shortfall = setting(config, "signoff", "shortfall")
    if not isinstance(shortfall, dict) or set(shortfall) != set(SHORTFALL):
        raise ConfigError(f"'signoff.shortfall' must name exactly {', '.join(SHORTFALL)}")

    completed = rule(shortfall, "missed_checkpoints", "completed_status")
    if not isinstance(completed, str) or not completed:
        raise ConfigError("'signoff.shortfall.missed_checkpoints.completed_status' must be a status")
    verdicts = rule(shortfall, "unscoreable_captures", "verdicts")
    if not isinstance(verdicts, list) or not verdicts or not all(isinstance(v, str) and v for v in verdicts):
        raise ConfigError("'signoff.shortfall.unscoreable_captures.verdicts' must be a non-empty list")
    abstained = rule(shortfall, "abstained", "detection_status")
    if not isinstance(abstained, list) or not all(isinstance(s, str) and s for s in abstained):
        raise ConfigError("'signoff.shortfall.abstained.detection_status' must be a list")

    return Settings(float(threshold), root / rows_dir, completed, tuple(verdicts), tuple(abstained))


def rule(shortfall: dict, category: str, key: str):
    """One required value of a shortfall category, or ConfigError naming it."""
    entry = shortfall[category]
    if not isinstance(entry, dict) or key not in entry:
        raise ConfigError(f"missing required setting 'signoff.shortfall.{category}.{key}'")
    return entry[key]


def facility_rows(rows_dir: Path, facility_id: str) -> list[Row] | None:
    """The rows of one facility, or None when it has no file.

    Raises ConfigError when the file is malformed. expected_subjects must equal
    the number of checkpoints listed: one subject is one checkpoint, and any
    other count would leave subjects that no shortfall category accounts for.
    A checkpoint listed in two rows is refused, never guessed.
    """
    path = rows_dir / f"{facility_id}.yaml"
    if not path.exists():
        return None
    config = load_config(path)

    if config.get("facility_id") != facility_id:
        raise ConfigError(f"{path}: 'facility_id' must be {facility_id!r}, got {config.get('facility_id')!r}")
    rows = config.get("rows")
    if not isinstance(rows, dict) or not rows:
        raise ConfigError(f"{path}: 'rows' must be a non-empty mapping")

    owner: dict[str, str] = {}
    result = []
    for row_id, entry in rows.items():
        checkpoints = entry.get("checkpoints") if isinstance(entry, dict) else None
        expected = entry.get("expected_subjects") if isinstance(entry, dict) else None
        if not isinstance(checkpoints, list) or not checkpoints or not all(isinstance(c, str) and c for c in checkpoints):
            raise ConfigError(f"{path}: '{row_id}.checkpoints' must be a non-empty list of ids")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
            raise ConfigError(f"{path}: '{row_id}.expected_subjects' must be a whole number above 0")
        if expected != len(checkpoints):
            raise ConfigError(
                f"{path}: '{row_id}.expected_subjects' is {expected} but the row lists "
                f"{len(checkpoints)} checkpoints"
            )
        for checkpoint_id in checkpoints:
            if checkpoint_id in owner:
                raise ConfigError(
                    f"{path}: checkpoint '{checkpoint_id}' is listed in both '{owner[checkpoint_id]}' and '{row_id}'"
                )
            owner[checkpoint_id] = row_id
        result.append(Row(row_id, tuple(checkpoints), expected))
    return result


def sign_off(
    record: Record, rows: list[Row], rules: Settings, images: dict[str, list[ResolvedImage]],
    unreadable: list[CouldNotRead], signed_off_at: str,
) -> list[RowSignOff]:
    """Every row's sign-off for this run, in the order the file lists them.

    images is the run's resolved evidence, as the report resolved it, and
    unreadable is the run's could_not_read entries. Both are passed in so the
    sign-off agrees with the report sections built from the same values.

    confidence is rounded down to two places, so the figure shown is never
    higher than the true one, and the threshold is checked against the figure
    shown, so the two always agree.
    """
    # A checkpoint visited twice is judged by its last visit, as resolve_record_images does.
    visits = {checkpoint.checkpoint_id: checkpoint for checkpoint in record.checkpoints}
    unread: dict[str, str] = {}
    for entry in unreadable:
        if entry.item_id is not None:
            unread.setdefault(entry.item_id, f"{entry.reason}: {entry.detail}")

    results = []
    for row in rows:
        uncovered = []
        for checkpoint_id in row.checkpoints:
            found = shortfall_of(
                visits.get(checkpoint_id), images.get(checkpoint_id, []), unread.get(checkpoint_id), rules,
            )
            if found is not None:
                uncovered.append(Uncovered(checkpoint_id, *found))

        readable = row.expected_subjects - len(uncovered)
        percent = readable * 100 // row.expected_subjects
        confidence = percent / 100
        results.append(RowSignOff(
            row_id=row.row_id,
            run_id=record.run_id,
            signed_off_at=signed_off_at,
            confidence=confidence,
            confidence_formatted=f"{percent}%",
            expected_subjects=row.expected_subjects,
            readable_subjects=readable,
            shortfall={category: sum(1 for u in uncovered if u.category == category) for category in SHORTFALL},
            below_threshold=confidence < rules.threshold,
            threshold=rules.threshold,
            uncovered=tuple(uncovered),
        ))
    return results


def shortfall_of(
    checkpoint: Checkpoint | None, images: list[ResolvedImage], unread: str | None, rules: Settings,
) -> tuple[str, str] | None:
    """The shortfall category and reason for one subject, or None when it is readable."""
    if checkpoint is None:
        return "missed_checkpoints", "not in this run"
    if checkpoint.status != rules.completed_status:
        return "missed_checkpoints", checkpoint.missed_reason or f"status is {checkpoint.status}"
    if not checkpoint.evidence_images:
        return "unresolved_captures", "no images recorded"
    if checkpoint.observed == "no_evidence":
        return "unresolved_captures", "observed = no_evidence"
    if not any(image.readable for image in images):
        return "unresolved_captures", f"none of its {len(images)} images is a readable file"
    if checkpoint.result_status not in rules.verdicts:
        return "unscoreable_captures", f"result_status is {checkpoint.result_status or 'empty'}"
    if unread is not None:
        return "could_not_read", unread
    for detection in checkpoint.detections:
        if detection.status in rules.abstained_statuses:
            return "abstained", f"detection {detection.finding_id} abstained"
    return None
