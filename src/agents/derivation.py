"""The derivation layer: every permitted derived value, computed by code.

This module is the only place arithmetic happens. Config is passed in as an
argument; nothing here reads a config file.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from common.schema import Checkpoint, Record


@dataclass(frozen=True)
class DerivationConfig:
    """The settings the derivation layer needs, resolved at the entry point.

    `subsystem_flags` is report.yaml's `sensor.subsystem_flags`: device flag ->
    the sensor block it governs. `max_age_seconds` is `sensor.max_age_seconds`.
    """

    subsystem_flags: dict[str, str]
    max_age_seconds: float


@dataclass(frozen=True)
class EligibleValue:
    """One value a derivation is allowed to compute over."""

    run_id: str
    kind: str                       # "checkpoint" or "sample"
    source_id: str                  # checkpoint_id, or sample_0042 by position
    zone: str | None
    timestamp: datetime | None
    value: float
    stale: bool


@dataclass(frozen=True)
class Exclusion:
    """Values left out of a computation, and why.

    One entry per (run, kind, checkpoint, zone, reason), with `count` saying how
    many values it covers. `scope` is the checkpoint that decided it: the
    checkpoint itself, or a sample's nearest checkpoint.
    """

    run_id: str
    kind: str
    scope: str | None
    zone: str | None
    block: str
    reason: str
    count: int


def eligible_values(
    records: Sequence[Record], field_path: str, config: DerivationConfig,
) -> tuple[list[EligibleValue], list[Exclusion]]:
    """Every value of `field_path` that passes the exclusion rule, and what did not.

    `field_path` is a dotted path into a sensor block, such as
    "environment.temperature_c". Values come from checkpoints and from
    telemetry samples, tagged by `kind`. A sample carries no device flags, so it
    is judged by its nearest checkpoint.
    """
    block = field_path.split(".", 1)[0]
    flags = [flag for flag, governed in config.subsystem_flags.items() if governed == block]
    if not flags:
        raise ValueError(f"no device flag governs sensor block '{block}' ({field_path})")
    flag = flags[0]

    values: list[EligibleValue] = []
    excluded: Counter = Counter()

    for record in records:
        # Judge each checkpoint once; samples look the verdict up.
        verdicts: dict[str, str | None] = {}
        for checkpoint in record.checkpoints:
            problem = _checkpoint_problem(checkpoint, flag)
            cid = checkpoint.checkpoint_id
            if cid in verdicts and verdicts[cid] != problem:
                problem = f"checkpoint id {cid} appears more than once, with different sensor states"
            verdicts[cid] = problem

        for checkpoint in record.checkpoints:
            cid = checkpoint.checkpoint_id
            sensor = checkpoint.sensor
            problem = verdicts[cid]
            value = _read(sensor, field_path)
            if problem is None:
                problem = _field_problem(value)
            if problem is not None:
                excluded[(record.run_id, "checkpoint", cid, checkpoint.zone, problem)] += 1
                continue
            stale = sensor.age_seconds is not None and sensor.age_seconds > config.max_age_seconds
            values.append(EligibleValue(
                record.run_id, "checkpoint", cid, checkpoint.zone,
                sensor.timestamp, value, stale,
            ))

        for index, sample in enumerate(record.sensor_samples):
            cid = sample.nearest_checkpoint_id
            if cid is None:
                problem = "no nearest checkpoint recorded"
            elif cid not in verdicts:
                problem = f"nearest checkpoint {cid} is not in the run"
            else:
                problem = verdicts[cid]
            if problem is None and sample.status != "connected":
                problem = f"sample status is {sample.status or 'not recorded'}"
            value = _read(sample.sensor, field_path)
            if problem is None:
                problem = _field_problem(value)
            if problem is not None:
                excluded[(record.run_id, "sample", cid, sample.zone, problem)] += 1
                continue
            values.append(EligibleValue(
                record.run_id, "sample", f"sample_{index:04d}", sample.zone,
                sample.timestamp, value, False,
            ))

    exclusions = [
        Exclusion(run_id, kind, scope, zone, block, reason, count)
        for (run_id, kind, scope, zone, reason), count in excluded.items()
    ]
    return values, exclusions


def _checkpoint_problem(checkpoint: Checkpoint, flag: str) -> str | None:
    """Why this checkpoint's reading of a block cannot be used, or None if it can.

    Checks section 3.2 conditions 1 to 3 in order and returns the first failure.
    """
    if checkpoint.status != "COMPLETED":
        return f"checkpoint status is {checkpoint.status or 'not recorded'}"
    sensor = checkpoint.sensor
    if sensor is None:
        return "no sensor block"
    if sensor.ok is not True:
        return "sensor ok is not true"
    if sensor.status != "connected":
        return f"sensor status is {sensor.status or 'not recorded'}"
    if sensor.sensor_hub_reachable is not True:
        return "sensor hub not reachable"
    state = getattr(sensor.raw, flag, None)
    if state is not True:
        return f"{flag}=false" if state is False else f"{flag} not recorded"
    return None


def _field_problem(value: object) -> str | None:
    """Section 3.2 condition 4: the field is present and numeric."""
    if value is None:
        return "field not recorded"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "field is not a number"
    return None


def _read(obj: object, field_path: str) -> object:
    """Follow a dotted path through attributes; None where any step is missing."""
    for name in field_path.split("."):
        if obj is None:
            return None
        obj = getattr(obj, name, None)
    return obj
