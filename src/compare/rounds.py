"""What differs between two rounds, and who changed what.

States the difference, never judges it. UNATTRIBUTED when the records do
not say why.

The previous run is the run before this one on the same route
(common.runs.previous_run), the current run is the run being reported.
"""

from __future__ import annotations

from dataclasses import fields
from enum import StrEnum

from common.paths import setting
from common.readability import could_not_read
from common.schema import Checkpoint, Record
from report.derive import computed_counts
from report.gaps import unavailable_reason


class Attribution(StrEnum):
    """Why a difference exists, as far as the records say. A closed set.

    Not a GapType: a gap is something one run lacks, an attribution is about
    a difference between two runs.
    """

    #: The checkpoint is in one run's checkpoints and not the other's.
    ROUTE_CHANGE = "ROUTE_CHANGE"

    #: The checkpoint is in both runs and one of them has status MISSED,
    #: with that run's missed_reason.
    MISSED = "MISSED"

    #: A sensor block is not usable because its device flag was false, or
    #: because the whole reading was unusable (report.gaps.unavailable_reason).
    SENSOR_UNAVAILABLE = "SENSOR_UNAVAILABLE"

    #: Both runs hold a reading and they differ.
    VALUE_CHANGED = "VALUE_CHANGED"

    #: A difference exists and the records do not say why.
    UNATTRIBUTED = "UNATTRIBUTED"


def compare_rounds(previous: Record, current: Record, config: dict) -> dict:
    """The comparison of the previous run with the current one.

    run_date is the date the run started on, as recorded, with no conversion to
    UTC. It and days_between are None when a run has no start_time.
    """
    date_previous = previous.start_time.date() if previous.start_time else None
    date_current = current.start_time.date() if current.start_time else None
    return {
        "comparison_version": setting(config, "comparison", "version"),
        "previous_run": {
            "run_id": previous.run_id,
            "run_date": date_previous.isoformat() if date_previous else None,
        },
        "current_run": {
            "run_id": current.run_id,
            "run_date": date_current.isoformat() if date_current else None,
        },
        "days_between": (
            (date_current - date_previous).days if date_previous and date_current else None
        ),
        "checkpoints": checkpoint_changes(previous, current),
        "result_changes": result_changes(previous, current),
        "reading_changes": reading_changes(previous, current, config),
        "finding_changes": finding_changes(previous, current),
        "coverage_changes": coverage_changes(previous, current, config),
        "sensors": sensor_changes(previous, current, config),
    }


def checkpoint_changes(previous: Record, current: Record) -> dict:
    """Checkpoints reached in one run and not the other, and how many in both.

    In one run's checkpoints and not the other's: ROUTE_CHANGE. In both, and
    reached in one run only: MISSED when the other run's status is MISSED,
    UNATTRIBUTED for any other status. Missed in both runs is no difference,
    so it is in no list. in_both counts the checkpoints reached in both.

    Lists keep each run's route order. A checkpoint id recorded twice is
    judged by its first entry.
    """
    before = first_entries(previous)
    now = first_entries(current)
    return {
        "only_in_previous": unmatched(before, now),
        "only_in_current": unmatched(now, before),
        "in_both": len(reached_in_both(before, now)),
    }


def result_changes(previous: Record, current: Record) -> list[dict]:
    """Checkpoints reached in both runs whose result_status differs.

    Only checkpoints COMPLETED in both: a missed checkpoint's result is not a
    verdict on anything, and it is already reported as MISSED. Always
    UNATTRIBUTED: the records do not say why a result changed. In the current
    run's route order.
    """
    before = first_entries(previous)
    now = first_entries(current)
    return [
        {
            "checkpoint_id": cid, "previous": before[cid].result_status,
            "current": now[cid].result_status, "attribution": Attribution.UNATTRIBUTED,
        }
        for cid in reached_in_both(before, now)
        if before[cid].result_status != now[cid].result_status
    ]


def sensor_changes(previous: Record, current: Record, config: dict) -> dict:
    """Sensor blocks usable in one run and not the other, at the checkpoints
    reached in both.

    A block is not usable when the whole reading is not (unavailable_reason, the
    same rule as the SENSOR_UNAVAILABLE gap) or its device flag is false (the
    SUBSYSTEM_OFFLINE gap). An absent flag claims nothing, so the block counts
    as usable. Each entry is SENSOR_UNAVAILABLE, with the reason the block was
    not usable in the other run. Usable in both, or in neither, is no difference.

    Entries keep the current run's route order, then the order of
    sensor.subsystem_flags.
    """
    flags = setting(config, "sensor", "subsystem_flags")
    before = first_entries(previous)
    now = first_entries(current)
    only_in_previous, only_in_current = [], []
    for cid in reached_in_both(before, now):
        for flag, block in flags.items():
            not_in_previous = block_problem(before[cid], flag)
            not_in_current = block_problem(now[cid], flag)
            if not_in_current and not not_in_previous:
                only_in_previous.append(sensor_entry(cid, block, not_in_current))
            elif not_in_previous and not not_in_current:
                only_in_current.append(sensor_entry(cid, block, not_in_previous))
    return {"only_in_previous": only_in_previous, "only_in_current": only_in_current}


def reading_changes(previous: Record, current: Record, config: dict) -> list[dict]:
    """Measurements that differ, at the checkpoints reached in both runs and in
    the sensor blocks usable in both.

    A block not usable in one run is in sensors, not here, so its fields are
    never compared and a placeholder value never appears. Every numeric field
    of a usable block is compared:

    - a value in both runs that differs: VALUE_CHANGED, with both values and
      the difference, current minus previous;
    - a value in one run and null in the other: UNATTRIBUTED, difference null;
    - equal, or null in both: no difference, not listed.

    Values are compared at the places they print to (decimals in the config),
    so two values that print the same are not a difference, and the difference
    is between the printed values. previous and current are as recorded. A
    field with no decimals entry is compared as recorded. Entries keep the current run's
    route order, then the order of sensor.subsystem_flags, then field order.
    """
    flags = setting(config, "sensor", "subsystem_flags")
    places = config.get("decimals") or {}
    before = first_entries(previous)
    now = first_entries(current)
    entries = []
    for cid in reached_in_both(before, now):
        for flag, block in flags.items():
            if block_problem(before[cid], flag) or block_problem(now[cid], flag):
                continue
            old = getattr(before[cid].sensor, block, None)
            new = getattr(now[cid].sensor, block, None)
            if old is None and new is None:
                continue
            for field in fields(old or new):
                entry = reading_entry(
                    cid, f"{block}.{field.name}",
                    getattr(old, field.name, None), getattr(new, field.name, None),
                    places.get(field.name),
                )
                if entry is not None:
                    entries.append(entry)
    return entries


def reading_entry(
    cid: str, field: str, previous: float | None, current: float | None, places: int | None,
) -> dict | None:
    """One field's entry, or None when the two runs do not differ."""
    if previous is None and current is None:
        return None
    if previous is None or current is None:
        return {
            "checkpoint_id": cid, "field": field, "previous": previous, "current": current,
            "difference": None, "difference_formatted": None,
            "attribution": Attribution.UNATTRIBUTED,
        }
    if places is None:
        if previous == current:
            return None
        difference = current - previous
        formatted = f"{difference:+}"
    else:
        # The difference of the printed values, so it always matches the two
        # figures beside it and never prints as 0.
        printed_previous, printed_current = round(previous, places), round(current, places)
        if printed_previous == printed_current:
            return None
        difference = round(printed_current - printed_previous, places)
        formatted = f"{difference:+.{places}f}"
    return {
        "checkpoint_id": cid, "field": field, "previous": previous, "current": current,
        "difference": difference, "difference_formatted": formatted,
        "attribution": Attribution.VALUE_CHANGED,
    }


def finding_changes(previous: Record, current: Record) -> dict:
    """Findings in one run and not the other, and in both, by checkpoint_id and
    feature.

    Every run-level finding is compared, whether or not its checkpoint was
    reached in both runs: findings are independent of the checkpoint rules.
    A finding only in the previous run is absent from the current one, not
    resolved. Severity and status are not compared. Lists keep each run's
    findings order; a checkpoint_id and feature recorded twice is one entry.
    """
    before = finding_keys(previous)
    now = finding_keys(current)
    return {
        "only_in_previous": [finding_entry(key) for key in before if key not in now],
        "only_in_current": [finding_entry(key) for key in now if key not in before],
        "in_both": [finding_entry(key) for key in now if key in before],
    }


def finding_keys(record: Record) -> dict[tuple[str | None, str | None], None]:
    """Each finding's (checkpoint_id, feature), once, in findings order."""
    return dict.fromkeys((f.checkpoint_id, f.feature) for f in record.findings)


def finding_entry(key: tuple[str | None, str | None]) -> dict:
    """The entry for one finding."""
    return {"checkpoint_id": key[0], "feature": key[1]}


def coverage_changes(previous: Record, current: Record, config: dict) -> dict:
    """Each run's coverage figures side by side, over the whole run.

    evidence is how many evidence_images every checkpoint entry lists, a
    checkpoint id recorded twice counted twice (the manifest's
    images_referenced keeps only the last entry's). missed is the checkpoints with
    status MISSED, as the reconciliation table counts them. could_not_read is
    the could_not_read entries, the same list the report prints.
    """
    figures = {}
    for name, count in (
        ("evidence", evidence_count),
        ("missed", lambda record: computed_counts(record).missed),
        ("could_not_read", lambda record: len(could_not_read(record, config))),
    ):
        figures[f"{name}_previous"] = count(previous)
        figures[f"{name}_current"] = count(current)
    return figures


def evidence_count(record: Record) -> int:
    """How many evidence images the run's checkpoints list."""
    return sum(len(checkpoint.evidence_images) for checkpoint in record.checkpoints)


def block_problem(checkpoint: Checkpoint, flag: str) -> str | None:
    """Why the block governed by `flag` is not usable at this checkpoint, or None."""
    reason = unavailable_reason(checkpoint.sensor)
    if reason is not None:
        return reason
    if getattr(checkpoint.sensor.raw, flag, None) is False:
        return f"{flag}=false"
    return None


def sensor_entry(cid: str, block: str, reason: str) -> dict:
    """The entry for a block the other run could not use."""
    return {
        "checkpoint_id": cid, "block": block,
        "attribution": Attribution.SENSOR_UNAVAILABLE, "reason": reason,
    }


def reached_in_both(before: dict[str, Checkpoint], now: dict[str, Checkpoint]) -> list[str]:
    """The checkpoint ids reached in both runs, in the current run's route order."""
    return [cid for cid, checkpoint in now.items() if cid in before and reached(before[cid]) and reached(checkpoint)]


def first_entries(record: Record) -> dict[str, Checkpoint]:
    """Each checkpoint id's first entry in the run, in route order."""
    by_id: dict[str, Checkpoint] = {}
    for checkpoint in record.checkpoints:
        by_id.setdefault(checkpoint.checkpoint_id, checkpoint)
    return by_id


def unmatched(mine: dict[str, Checkpoint], theirs: dict[str, Checkpoint]) -> list[dict]:
    """The checkpoints in `mine` that `theirs` does not have, or did not reach
    when `mine` did."""
    entries = []
    for cid, checkpoint in mine.items():
        other = theirs.get(cid)
        if other is None:
            entries.append({"id": cid, "attribution": Attribution.ROUTE_CHANGE})
        elif reached(checkpoint) and not reached(other):
            entries.append(not_reached(cid, other))
    return entries


def not_reached(cid: str, checkpoint: Checkpoint) -> dict:
    """The entry for a checkpoint the other run did not reach."""
    if checkpoint.status == "MISSED":
        return {"id": cid, "attribution": Attribution.MISSED, "missed_reason": checkpoint.missed_reason}
    return {"id": cid, "attribution": Attribution.UNATTRIBUTED}


def reached(checkpoint: Checkpoint) -> bool:
    """Whether the robot got to the checkpoint."""
    return checkpoint.status == "COMPLETED"
