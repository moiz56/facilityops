"""Shared types and helpers for the agent layer."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import fields
from datetime import datetime
from typing import Sequence

from common.paths import ConfigError, setting
from common.schema import Accelerometer, Checkpoint, Environment, Particulate, Record
from agents.schema import (
    DerivationConfig, Eligibility, EligibleValue, Exclusion, VerificationConfig,
)

SENSOR_BLOCKS = {
    "accelerometer": Accelerometer,
    "environment": Environment,
    "particulate": Particulate,
}

# Record lists a field path can start with, and the kind their values get.
RECORD_SCOPES = {
    "checkpoints": "checkpoint",
    "findings": "finding",
    "sensor_alerts": "sensor_alert",
}

# Values a derivation's `source` can name, and the kind they select.
SOURCES = {
    "checkpoints": "checkpoint",
    "samples": "sample",
}


# Attributes both a value and an exclusion carry, so either can be grouped by them.
GROUPABLE = {f.name for f in fields(EligibleValue)} & {f.name for f in fields(Exclusion)}


class UnknownDerivationError(ValueError):
    """A derivations.yaml entry names a type that is not one of the eight."""


# Derivation helpers

def param(params: dict, key: str) -> object:
    """A required setting from a derivation entry."""
    if key not in params:
        raise ValueError(f"derivation config is missing required key '{key}'")
    return params[key]


def not_computable(derivation: str, reason: str) -> dict:
    return {"derivation": derivation, "status": "NOT_COMPUTABLE", "reason": reason}


def lookup(
    eligibility: Eligibility, field_path: str, derivation: str,
) -> tuple[list[EligibleValue], list[Exclusion]]:
    """The eligibility main computed for `field_path`."""
    if field_path not in eligibility:
        raise ValueError(f"{derivation}: no eligibility computed for '{field_path}'")
    return eligibility[field_path]


def for_run(
    values: list[EligibleValue], exclusions: list[Exclusion], run_id: str, kind: str | None = None,
) -> tuple[list[EligibleValue], list[Exclusion]]:
    """Only one run's values and exclusions, and only one kind if given."""
    values = [v for v in values if v.run_id == run_id and kind in (None, v.kind)]
    exclusions = [e for e in exclusions if e.run_id == run_id and kind in (None, e.kind)]
    return values, exclusions


def source_kind(params: dict, derivation: str) -> str:
    """The value kind a derivation's `source` setting selects."""
    source = param(params, "source")
    if source not in SOURCES:
        raise ValueError(f"{derivation}: source must be one of {', '.join(SOURCES)}, got {source!r}")
    return SOURCES[source]


def by_time(value: EligibleValue) -> tuple:
    """Sort key: earliest timestamp first, values with no timestamp last."""
    return (value.timestamp is None, value.timestamp)


def round_value(value: float, name: str, config: DerivationConfig) -> float:
    """Round a raw output number, e.g. round_value(0.71934, "accelerometer.vibration_rms_g").

    Uses the field's report.yaml decimals, so the number matches its
    *_formatted string. A name with no decimals entry, like proportion, uses
    significant_figures instead.
    """
    name = name.rsplit(".", 1)[-1]
    if name in config.decimals:
        return round(value, config.decimals[name])
    return float(f"{value:.{config.significant_figures}g}")


def format_value(value: float, field_path: str, config: DerivationConfig) -> str:
    """Format to the decimal places report.yaml gives the field."""
    name = field_path.rsplit(".", 1)[-1]
    if name not in config.decimals:
        raise ValueError(f"report.yaml decimals has no entry for '{name}'")
    return f"{value:.{config.decimals[name]}f}"


def format_timestamp(timestamp: datetime | None, config: DerivationConfig) -> str | None:
    """In the offset it was recorded in, e.g. 2026-07-28T14:49:16-0700."""
    if timestamp is None:
        return None
    return timestamp.strftime(config.timestamp_format)


def format_date(timestamp: datetime, config: DerivationConfig) -> str:
    """The date as recorded, e.g. 28 July 2026."""
    return config.date_format.format(
        day=timestamp.day, month=timestamp.strftime("%B"), year=timestamp.year,
    )


def match_condition(
    records: Sequence[Record], params: dict, eligibility: Eligibility,
    config: DerivationConfig, derivation: str,
) -> tuple[int, list[str], int]:
    """Apply a named condition to the latest run's eligible items.

    Returns (eligible count, matching ids, excluded count).
    """
    scope = param(params, "scope")
    name = param(params, "condition")
    if scope not in RECORD_SCOPES:
        raise ValueError(f"{derivation}: scope must be one of {', '.join(RECORD_SCOPES)}, got {scope!r}")
    if name not in config.conditions:
        raise ValueError(f"{derivation}: no condition named '{name}' in the conditions table")

    condition = config.conditions[name]
    field_path = f"{scope}.{param(condition, 'field')}"
    equals = param(condition, "equals")

    values, exclusions = for_run(*lookup(eligibility, field_path, derivation), records[-1].run_id)
    matching_ids = [v.source_id for v in values if v.value == equals]
    return len(values), matching_ids, sum(e.count for e in exclusions)


def group_inputs(
    records: Sequence[Record], params: dict, eligibility: Eligibility, derivation: str,
) -> tuple[dict, list[Exclusion]]:
    """The latest run's inputs for a grouped derivation, split into groups.

    Each group is {"values": [...], "excluded": count, "reasons": [...]}, in
    the order the values come. Also returns all of the field's exclusions, for
    the no-eligible-inputs reason.
    """
    group_by = param(params, "group_by")
    if group_by not in GROUPABLE:
        raise ValueError(f"{derivation}: group_by must be one of {', '.join(sorted(GROUPABLE))}, got {group_by!r}")

    all_values, all_exclusions = lookup(eligibility, param(params, "field_path"), derivation)
    kind = source_kind(params, derivation)
    values, exclusions = for_run(all_values, all_exclusions, records[-1].run_id, kind)

    groups: dict = {}
    for v in values:
        key = getattr(v, group_by)
        groups.setdefault(key, {"values": [], "excluded": 0, "reasons": []})["values"].append(v)
    for e in exclusions:
        g = groups.setdefault(getattr(e, group_by), {"values": [], "excluded": 0, "reasons": []})
        g["excluded"] += e.count
        if e.reason not in g["reasons"]:
            g["reasons"].append(e.reason)
    return groups, all_exclusions


def empty_group(group: dict) -> dict:
    """A group whose inputs were all excluded: its reasons and nothing else."""
    return {"status": "NOT_COMPUTABLE", "reason": "; ".join(group["reasons"])}


def grouped_result(
    derivation: str, params: dict, results: dict, record: Record, exclusions: list[Exclusion],
) -> dict:
    """The output of a grouped derivation, or NOT_COMPUTABLE if no group is OK."""
    if not any(r["status"] == "OK" for r in results.values()):
        return not_computable(derivation, no_inputs_reason(record, exclusions))
    return {
        "derivation": derivation,
        "status": "OK",
        "field_path": params["field_path"],
        "group_by": params["group_by"],
        "groups": results,
    }


def no_inputs_reason(record: Record, exclusions: list[Exclusion]) -> str:
    """e.g. "no eligible inputs: sps30_ok=false at 8 of 8 checkpoints"."""
    checkpoints = {cp.checkpoint_id for cp in record.checkpoints}
    by_reason: dict[str, set] = {}
    for e in exclusions:
        if e.kind == "checkpoint" and e.run_id == record.run_id:
            by_reason.setdefault(e.reason, set()).add(e.scope)

    parts = [f"{reason} at {len(ids)} of {len(checkpoints)} checkpoints" for reason, ids in by_reason.items()]
    return "no eligible inputs: " + "; ".join(parts) if parts else "no eligible inputs"


def excluded_entries(record: Record, eligibility: Eligibility, config: DerivationConfig) -> list[dict]:
    """The extended record's `excluded` list for one run (section 5.2).

    One entry per checkpoint, block and reason, naming the derivations it
    affected. A reason that covers every checkpoint in the run becomes a
    single entry scoped to __run__.
    """
    checkpoints = set(cp.checkpoint_id for cp in record.checkpoints)
    entries: dict[tuple, list[str]] = {}

    for name, entry in config.derivations.items():
        inputs = entry_inputs(entry, config)
        if inputs is None:
            continue
        field_path, kind = inputs
        _, exclusions = for_run(*lookup(eligibility, field_path, name), record.run_id, kind)

        scopes_by_reason: dict[tuple, set] = {}
        for e in exclusions:
            scopes_by_reason.setdefault((e.block, e.reason), set()).add(e.scope)

        for (block, reason), scopes in scopes_by_reason.items():
            if checkpoints and scopes >= checkpoints:
                n = len(checkpoints)
                keys = [("__run__", block, f"{reason} at every checkpoint that reported it ({n} of {n})")]
            else:
                keys = [(scope, block, reason) for scope in sorted(scopes, key=str)]
            for key in keys:
                names = entries.setdefault(key, [])
                if name not in names:
                    names.append(name)

    return [
        {"scope": scope, "block": block, "reason": reason, "affected_derivations": names}
        for (scope, block, reason), names in entries.items()
    ]


def stale_entries(record: Record, eligibility: Eligibility, config: DerivationConfig) -> list[dict]:
    """The extended record's `stale` list for one run: stale inputs a derivation used."""
    entries: dict[tuple, list[str]] = {}

    for name, entry in config.derivations.items():
        inputs = entry_inputs(entry, config)
        if inputs is None:
            continue
        field_path, kind = inputs
        values, _ = for_run(*lookup(eligibility, field_path, name), record.run_id, kind)
        block = field_path.split(".", 1)[0]
        for v in values:
            if v.stale:
                names = entries.setdefault((v.source_id, block), [])
                if name not in names:
                    names.append(name)

    return [
        {"scope": scope, "block": block, "affected_derivations": names}
        for (scope, block), names in entries.items()
    ]


def item_id(scope: str, item: object, index: int, config: DerivationConfig) -> str:
    """Sensor alerts have no id field, so they are named by position."""
    if scope == "checkpoints":
        return item.checkpoint_id
    if scope == "findings":
        return item.finding_id
    return config.sensor_alert_id_format.format(index=index)


def checkpoint_problem(checkpoint: Checkpoint, flag: str, config: DerivationConfig) -> str | None:
    """First failing check of section 3.2 conditions 1 to 3, or None."""
    if checkpoint.status != config.completed_status:
        return f"checkpoint status is {checkpoint.status or 'not recorded'}"
    sensor = checkpoint.sensor
    if sensor is None:
        return "no sensor block"
    if sensor.ok is not True:
        return "sensor ok is not true"
    if sensor.status != config.connected_status:
        return f"sensor status is {sensor.status or 'not recorded'}"
    if sensor.sensor_hub_reachable is not True:
        return "sensor hub not reachable"
    state = getattr(sensor.raw, flag, None)
    if state is not True:
        return f"{flag}=false" if state is False else f"{flag} not recorded"
    return None


def field_problem(value: object) -> str | None:
    """Section 3.2 condition 4: the field is present and numeric."""
    if value is None:
        return "field not recorded"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "field is not a number"
    return None


def read_path(obj: object, field_path: str) -> object:
    """Follow a dotted path through attributes. None if any step is missing."""
    for name in field_path.split("."):
        if obj is None:
            return None
        obj = getattr(obj, name, None)
    return obj


# Config

def sensor_field_paths() -> list[str]:
    """Every field of every sensor block, e.g. environment.temperature_c."""
    return [
        f"{block}.{field.name}"
        for block, block_type in SENSOR_BLOCKS.items()
        for field in fields(block_type)
    ]


def entry_inputs(entry: dict, config: DerivationConfig) -> tuple[str, str | None] | None:
    """The field path and value kind a derivation entry reads, or None.

    e.g. (environment.temperature_c, "checkpoint") for D1, or
    (checkpoints.result_status, None) for D2. None for D7 and D8, which read
    the records directly.
    """
    if "condition" in entry:
        condition = config.conditions.get(entry["condition"]) or {}
        if "scope" in entry and "field" in condition:
            return f"{entry['scope']}.{condition['field']}", None
        return None
    if "field_path" in entry:
        return entry["field_path"], SOURCES.get(entry.get("source"))
    return None


def hash_configs(*configs: dict) -> str:
    """sha256 over the parsed configs, so comments and spacing don't change it."""
    text = json.dumps(configs, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def derivation_config(report: dict, derivations: dict, config_hash: str) -> DerivationConfig:
    decimals = report.get("decimals")
    if not isinstance(decimals, dict):
        raise ConfigError("missing required setting 'decimals'")

    significant_figures = derivations.get("significant_figures")
    if isinstance(significant_figures, bool) or not isinstance(significant_figures, int):
        raise ConfigError("missing required setting 'significant_figures'")

    conditions = derivations.get("conditions") or {}
    if not isinstance(conditions, dict):
        raise ConfigError("setting 'conditions' must be a table of named conditions")

    exempt = setting(derivations, "eligibility", "status_exempt_fields")
    if not isinstance(exempt, list):
        raise ConfigError("setting 'eligibility.status_exempt_fields' must be a list")

    entries = derivations.get("derivations")
    if not isinstance(entries, dict):
        raise ConfigError("missing required setting 'derivations'")

    for key in ("derivation_set_version", "extended_record_version"):
        if key not in derivations:
            raise ConfigError(f"missing required setting '{key}'")

    return DerivationConfig(
        subsystem_flags=setting(report, "sensor", "subsystem_flags"),
        max_age_seconds=setting(report, "sensor", "max_age_seconds"),
        decimals=decimals,
        significant_figures=significant_figures,
        conditions=conditions,
        completed_status=setting(derivations, "eligibility", "completed_status"),
        connected_status=setting(derivations, "eligibility", "connected_status"),
        status_exempt_fields=exempt,
        timestamp_format=setting(derivations, "formats", "timestamp"),
        date_format=setting(derivations, "formats", "date"),
        sample_id_format=setting(derivations, "formats", "sample_id"),
        sensor_alert_id_format=setting(derivations, "formats", "sensor_alert_id"),
        computed_at_format=setting(derivations, "formats", "computed_at"),
        derivations=entries,
        derivation_set_version=str(derivations["derivation_set_version"]),
        extended_record_version=str(derivations["extended_record_version"]),
        config_hash=config_hash,
    )



def verification_config(agents: dict, report: dict, derivations: dict) -> VerificationConfig:
    """agents.yaml verification settings, plus what verification needs from the other two files."""
    tolerance = setting(agents, "verification", "numeric_tolerance")
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or tolerance < 0:
        raise ConfigError("setting 'verification.numeric_tolerance' must be a number, 0 or more")

    flags = {}
    for key in ("reject_unclassifiable", "record_tolerance_anomalies", "word_numbers"):
        flags[key] = setting(agents, "verification", key)
        if not isinstance(flags[key], bool):
            raise ConfigError(f"setting 'verification.{key}' must be true or false")

    decimals = report.get("decimals")
    if not isinstance(decimals, dict):
        raise ConfigError("missing required setting 'decimals'")

    return VerificationConfig(
        numeric_tolerance=float(tolerance),
        timestamp_format=setting(derivations, "formats", "timestamp"),
        date_format=setting(derivations, "formats", "date"),
        decimals=decimals,
        engine_version=str(setting(report, "provenance", "engine_version")),
        template_version=str(setting(report, "provenance", "template_version")),
        **flags,
    )


# Printing

def print_runs(records: Sequence[Record]) -> None:
    for record in records:
        print(
            f"{record.run_id}  checkpoints={len(record.checkpoints)}"
            f"  samples={len(record.sensor_samples)}"
            f"  findings={len(record.findings)}"
        )
    print(f"{len(records)} records, oldest first")


def print_eligibility(eligibility: Eligibility) -> None:
    """Print each block with its fields side by side, then the summary."""
    by_block: dict[str, Eligibility] = {}
    for field_path, result in eligibility.items():
        block, _, name = field_path.partition(".")
        by_block.setdefault(block, {})[name] = result

    for block, results in by_block.items():
        print_block(block, results)

    # The summary counts sensor readings only.
    print_summary({b: r for b, r in by_block.items() if b in SENSOR_BLOCKS})


def print_summary(by_block: dict[str, Eligibility]) -> None:
    """One line per source (kind + block). A reading is one field of one sample or checkpoint."""
    print(f"\n{'source':<26}{'readings':>9}  status")
    total_kept = total_out = 0

    for kind in ("sample", "checkpoint"):
        for block, results in by_block.items():
            kept = out = 0
            reasons = set()
            for values, exclusions in results.values():
                kept += sum(1 for v in values if v.kind == kind)
                for e in exclusions:
                    if e.kind == kind:
                        out += e.count
                        reasons.add(e.reason)
            if kept + out == 0:
                continue

            if out == 0:
                status = "eligible"
            elif kept == 0:
                status = "excluded — " + "; ".join(sorted(reasons))
            else:
                status = f"{kept:,} eligible · {out:,} excluded"
            print(f"{kind.capitalize() + ' ' + block:<26}{kept + out:>9,}  {status}")
            total_kept += kept
            total_out += out

    total = total_kept + total_out
    if total:
        print(f"{'Total':<26}{total:>9,}  "
              f"{total_kept:,} eligible ({total_kept / total:.0%})"
              f" · {total_out:,} excluded ({total_out / total:.0%})")


def print_block(block: str, results: Eligibility) -> None:
    """One block's eligibility. A count shared by every field is printed once."""
    names = list(results)
    print(f"\n{block}  ({', '.join(names)})")

    kept: dict[tuple, Counter] = {}
    stale: dict[tuple, Counter] = {}
    excluded: dict[tuple, Counter] = {}
    for name, (values, exclusions) in results.items():
        for v in values:
            key = (v.run_id, v.kind, v.zone)
            kept.setdefault(key, Counter())[name] += 1
            stale.setdefault(key, Counter())[name] += v.stale
        for e in exclusions:
            key = (e.run_id, e.kind, e.zone, e.scope, e.reason)
            excluded.setdefault(key, Counter())[name] += e.count

    print("eligible:")
    for key in sorted(kept, key=str):
        run_id, kind, zone = key
        print(f"  {run_id}  {kind:<10}  {zone}  n={together(kept[key], names)}"
              f"  stale={together(stale[key], names)}")

    print("excluded:" if excluded else "excluded: none")
    for key in sorted(excluded, key=str):
        run_id, kind, zone, scope, reason = key
        print(f"  {run_id}  {kind:<10}  {zone}  scope={scope}"
              f"  count={together(excluded[key], names)}  reason={reason}")


def together(counts: Counter, names: list[str]) -> str:
    """One number if every field has the same count, else each field's count."""
    per_field = [counts[name] for name in names]
    if len(set(per_field)) == 1:
        return str(per_field[0])
    return " ".join(f"{name}={n}" for name, n in zip(names, per_field))
