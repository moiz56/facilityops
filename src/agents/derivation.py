"""Eligibility and the derivations. The only place arithmetic happens."""

from __future__ import annotations

from collections import Counter
from typing import Sequence

from common.schema import Record
from agents.utils import *


# Eligibility

def eligible_values(
    records: Sequence[Record], field_path: str, config: DerivationConfig,
) -> tuple[list[EligibleValue], list[Exclusion]]:
    """Values of `field_path` that pass section 3.2, and the ones that don't.

    `field_path` is a sensor field (environment.temperature_c) or a record
    field (checkpoints.result_status). Samples have no device flags, so a
    sample is judged by its nearest checkpoint.
    """
    block = field_path.split(".", 1)[0]
    if block in RECORD_SCOPES:
        return record_values(records, field_path, config)

    flags = [flag for flag, governed in config.subsystem_flags.items() if governed == block]
    if not flags:
        raise ValueError(f"no device flag governs sensor block '{block}' ({field_path})")
    flag = flags[0]

    values = []
    excluded = Counter()

    for record in records:
        verdicts = {}
        for checkpoint in record.checkpoints:
            cid = checkpoint.checkpoint_id
            problem = checkpoint_problem(checkpoint, flag, config)
            if cid in verdicts and verdicts[cid] != problem:
                problem = f"checkpoint id {cid} appears more than once, with different sensor states"
            verdicts[cid] = problem

        for checkpoint in record.checkpoints:
            cid = checkpoint.checkpoint_id
            sensor = checkpoint.sensor
            value = read_path(sensor, field_path)
            problem = verdicts[cid] or field_problem(value)
            if problem:
                excluded[(record.run_id, "checkpoint", cid, checkpoint.zone, problem)] += 1
                continue
            stale = sensor.age_seconds is not None and sensor.age_seconds > config.max_age_seconds
            values.append(EligibleValue(
                record.run_id, "checkpoint", cid, checkpoint.zone, sensor.timestamp, value, stale,
            ))

        for index, sample in enumerate(record.sensor_samples):
            cid = sample.nearest_checkpoint_id
            value = read_path(sample.sensor, field_path)
            if cid is None:
                problem = "no nearest checkpoint recorded"
            elif cid not in verdicts:
                problem = f"nearest checkpoint {cid} is not in the run"
            elif verdicts[cid]:
                problem = verdicts[cid]
            elif sample.status != config.connected_status:
                problem = f"sample status is {sample.status or 'not recorded'}"
            else:
                problem = field_problem(value)
            if problem:
                excluded[(record.run_id, "sample", cid, sample.zone, problem)] += 1
                continue
            values.append(EligibleValue(
                record.run_id, "sample", config.sample_id_format.format(index=index), sample.zone,
                sample.timestamp, value, False,
            ))

    exclusions = [
        Exclusion(run_id, kind, cid, zone, block, reason, count)
        for (run_id, kind, cid, zone, reason), count in excluded.items()
    ]
    return values, exclusions


def record_values(
    records: Sequence[Record], field_path: str, config: DerivationConfig,
) -> tuple[list[EligibleValue], list[Exclusion]]:
    """Eligibility for a record field such as checkpoints.result_status.

    Only section 3.2 condition 1 applies, plus the field must be recorded.
    Condition 1 is skipped for the fields in status_exempt_fields, so a MISSED
    checkpoint still counts when the question is about its status.
    """
    scope, _, field = field_path.partition(".")
    kind = RECORD_SCOPES[scope]
    judge_status = scope == "checkpoints" and field not in config.status_exempt_fields
    values = []
    excluded = Counter()

    for record in records:
        for index, item in enumerate(getattr(record, scope)):
            cid = item.checkpoint_id if scope == "checkpoints" else None
            value = getattr(item, field, None)
            if judge_status and item.status != config.completed_status:
                problem = f"checkpoint status is {item.status or 'not recorded'}"
            elif value is None:
                problem = "field not recorded"
            else:
                problem = None
            if problem:
                excluded[(record.run_id, kind, cid, item.zone, problem)] += 1
                continue
            values.append(EligibleValue(
                record.run_id, kind, item_id(scope, item, index, config), item.zone,
                item.timestamp, value, False,
            ))

    exclusions = [
        Exclusion(run_id, kind, cid, zone, scope, reason, count)
        for (run_id, kind, cid, zone, reason), count in excluded.items()
    ]
    return values, exclusions


# D1

def threshold_compare(
    records: Sequence[Record], params: dict, eligibility: Eligibility, config: DerivationConfig,
) -> dict:
    """Compare checkpoint values in the latest run against a threshold.

    scope is a checkpoint id, or "checkpoints" for all of them.
    delta = value - threshold. Equal to the threshold does not exceed it.
    """
    scope = param(params, "scope")
    field_path = param(params, "field_path")
    threshold = param(params, "threshold")
    direction = param(params, "direction")

    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ValueError(f"threshold_compare: threshold must be a number, got {threshold!r}")
    if direction not in ("above", "below"):
        raise ValueError(f"threshold_compare: direction must be above or below, got {direction!r}")

    values, exclusions = for_run(
        *lookup(eligibility, field_path, "threshold_compare"), records[-1].run_id, "checkpoint",
    )

    def compare(cid: str) -> tuple[dict | None, str | None]:
        """(result, None) for a comparison, or (None, reason) if there isn't one."""
        matches = [v for v in values if v.source_id == cid]
        if not matches:
            reasons = [e.reason for e in exclusions if e.scope == cid]
            return None, "; ".join(reasons) if reasons else f"{cid} is not in the run"
        if len(matches) > 1:
            return None, f"{cid} has {len(matches)} eligible values in the run"

        value = matches[0].value
        delta = value - threshold
        return {
            "value": round_value(value, field_path, config),
            "exceeds": value > threshold if direction == "above" else value < threshold,
            "delta": round_value(delta, field_path, config),
            "delta_formatted": format_value(delta, field_path, config),
        }, None

    header = {
        "derivation": "threshold_compare",
        "status": "OK",
        "scope": scope,
        "field_path": field_path,
        "threshold": threshold,
        "direction": direction,
    }

    if scope != "checkpoints":
        result, reason = compare(scope)
        if result is None:
            return not_computable("threshold_compare", reason)
        excluded = sum(e.count for e in exclusions if e.scope == scope)
        return {**header, **result, "inputs_used": 1, "inputs_excluded": excluded}

    results = {}
    for cid in dict.fromkeys(cp.checkpoint_id for cp in records[-1].checkpoints):
        result, reason = compare(cid)
        results[cid] = {"status": "OK", **result} if result else {"status": "NOT_COMPUTABLE", "reason": reason}

    used = sum(1 for r in results.values() if r["status"] == "OK")
    if used == 0:
        return not_computable("threshold_compare", "no checkpoint in the run has an eligible value")

    return {
        **header,
        "checkpoints": results,
        "inputs_used": used,
        "inputs_excluded": sum(e.count for e in exclusions),
    }


# D2

def condition_count(
    records: Sequence[Record], params: dict, eligibility: Eligibility, config: DerivationConfig,
) -> dict:
    """Count items in the latest run where a named condition holds.

    Reads eligibility for "<scope>.<field>", so population and inputs_excluded
    come straight from it.
    """
    population, matching_ids, excluded = match_condition(
        records, params, eligibility, config, "condition_count",
    )

    return {
        "derivation": "condition_count",
        "status": "OK",
        "scope": params["scope"],
        "condition": params["condition"],
        "count": len(matching_ids),
        "matching_ids": matching_ids,
        "population": population,
        "inputs_excluded": excluded,
    }


# D3

def proportion(
    records: Sequence[Record], params: dict, eligibility: Eligibility, config: DerivationConfig,
) -> dict:
    """Share of items in the latest run where a named condition holds.

    Numerator is the matching items, denominator is every eligible item.
    """
    denominator, numerator_ids, excluded = match_condition(
        records, params, eligibility, config, "proportion",
    )
    if denominator == 0:
        return not_computable("proportion", "denominator is zero")

    share = len(numerator_ids) / denominator
    percentage = share * 100

    return {
        "derivation": "proportion",
        "status": "OK",
        "numerator": len(numerator_ids),
        "denominator": denominator,
        "proportion": round_value(share, "proportion", config),
        "percentage": round_value(percentage, "percentage", config),
        "percentage_formatted": format_value(percentage, "percentage", config),
        "numerator_ids": numerator_ids,
        "inputs_excluded": excluded,
    }


# D4

def group_mean(
    records: Sequence[Record], params: dict, eligibility: Eligibility, config: DerivationConfig,
) -> dict:
    """Mean of a field per group, over the latest run.

    A group whose inputs were all excluded is NOT_COMPUTABLE with no mean.
    """
    groups, exclusions = group_inputs(records, params, eligibility, "group_mean")
    field_path = params["field_path"]

    results = {}
    for key, g in groups.items():
        if not g["values"]:
            results[key] = empty_group(g)
            continue
        n = len(g["values"])
        mean = sum(v.value for v in g["values"]) / n
        results[key] = {
            "status": "OK",
            "mean": round_value(mean, field_path, config),
            "mean_formatted": format_value(mean, field_path, config),
            "n": n,
            "inputs_excluded": g["excluded"],
            "inputs_stale": sum(1 for v in g["values"] if v.stale),
        }

    return grouped_result("group_mean", params, results, records[-1], exclusions)


# D5

def group_max(
    records: Sequence[Record], params: dict, eligibility: Eligibility, config: DerivationConfig,
) -> dict:
    """Maximum of a field per group, over the latest run.

    On a tie, source_id is the earliest by timestamp and tied_with lists the rest.
    """
    groups, exclusions = group_inputs(records, params, eligibility, "group_max")
    field_path = params["field_path"]

    results = {}
    for key, g in groups.items():
        if not g["values"]:
            results[key] = empty_group(g)
            continue
        top = max(v.value for v in g["values"])
        tied = sorted((v for v in g["values"] if v.value == top), key=by_time)
        tied_with = {"tied_with": [v.source_id for v in tied[1:]]} if len(tied) > 1 else {}
        results[key] = {
            "status": "OK",
            "max": round_value(top, field_path, config),
            "max_formatted": format_value(top, field_path, config),
            "source_id": tied[0].source_id,
            "source_timestamp": format_timestamp(tied[0].timestamp, config),
            **tied_with,
            "n": len(g["values"]),
            "inputs_excluded": g["excluded"],
        }

    return grouped_result("group_max", params, results, records[-1], exclusions)


# D6

def rank_top_n(
    records: Sequence[Record], params: dict, eligibility: Eligibility, config: DerivationConfig,
) -> dict:
    """The n highest values in the latest run, highest first.

    Equal values are ordered earliest timestamp first, then route order.
    A value tied with rank n is included too, so n_returned can be more than
    n_requested, or less when fewer items are eligible.
    """
    field_path = param(params, "field_path")
    n = param(params, "n")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError(f"rank_top_n: n must be a positive whole number, got {n!r}")

    all_values, all_exclusions = lookup(eligibility, field_path, "rank_top_n")
    kind = source_kind(params, "rank_top_n")
    values, exclusions = for_run(all_values, all_exclusions, records[-1].run_id, kind)
    if not values:
        return not_computable("rank_top_n", no_inputs_reason(records[-1], all_exclusions))

    ordered = sorted(sorted(values, key=by_time), key=lambda v: v.value, reverse=True)
    cut = min(n, len(ordered))
    while cut < len(ordered) and ordered[cut].value == ordered[cut - 1].value:
        cut += 1

    ranking = [
        {
            "rank": rank,
            "id": v.source_id,
            "value": round_value(v.value, field_path, config),
            "value_formatted": format_value(v.value, field_path, config),
        }
        for rank, v in enumerate(ordered[:cut], start=1)
    ]

    return {
        "derivation": "rank_top_n",
        "status": "OK",
        "field_path": field_path,
        "n_requested": n,
        "n_returned": len(ranking),
        "ranking": ranking,
        "population": len(values),
        "inputs_excluded": sum(e.count for e in exclusions),
    }


# D7

def run_set_difference(
    records: Sequence[Record], params: dict, eligibility: Eligibility, config: DerivationConfig,
) -> dict:
    """Which checkpoints are in one of the two latest runs and not the other.

    run_a is the earlier run, run_b the latest. Lists keep route order.
    States what differs only: no trend or direction.
    """
    if len(records) < 2:
        return not_computable("run_set_difference", "requires two runs; one supplied")

    run_a, run_b = records[-2], records[-1]
    ids_a = list(dict.fromkeys(cp.checkpoint_id for cp in run_a.checkpoints))
    ids_b = list(dict.fromkeys(cp.checkpoint_id for cp in run_b.checkpoints))

    only_in_a = [cid for cid in ids_a if cid not in ids_b]
    only_in_b = [cid for cid in ids_b if cid not in ids_a]
    in_both = [cid for cid in ids_b if cid in ids_a]

    return {
        "derivation": "run_set_difference",
        "status": "OK",
        "run_a": run_a.run_id,
        "run_b": run_b.run_id,
        "only_in_a": only_in_a,
        "only_in_b": only_in_b,
        "in_both": in_both,
        "count_only_in_a": len(only_in_a),
        "count_only_in_b": len(only_in_b),
        "count_in_both": len(in_both),
    }


# D8

def run_date_range(
    records: Sequence[Record], params: dict, eligibility: Eligibility, config: DerivationConfig,
) -> dict:
    """Earliest and latest run start times, and the days between them.

    span_days compares the two calendar dates as recorded, each in its own
    offset, with no conversion to UTC. One run gives span_days 0.
    """
    dated = [r for r in records if r.start_time is not None]
    if not dated:
        return not_computable("run_date_range", "no run has a start time")

    earliest = min(r.start_time for r in dated)
    latest = max(r.start_time for r in dated)

    return {
        "derivation": "run_date_range",
        "status": "OK",
        "run_count": len(dated),
        "earliest": format_timestamp(earliest, config),
        "latest": format_timestamp(latest, config),
        "earliest_formatted": format_date(earliest, config),
        "latest_formatted": format_date(latest, config),
        "span_days": (latest.date() - earliest.date()).days,
    }


# derivations.yaml `type` -> function
DERIVATIONS = {
    "threshold_compare": threshold_compare,
    "condition_count": condition_count,
    "proportion": proportion,
    "group_mean": group_mean,
    "group_max": group_max,
    "rank_top_n": rank_top_n,
    "run_set_difference": run_set_difference,
    "run_date_range": run_date_range,
}


def derive(
    entry: dict, records: Sequence[Record], eligibility: Eligibility, config: DerivationConfig,
) -> dict:
    """Run one derivations.yaml entry. Any type outside the eight is an error."""
    kind = param(entry, "type")
    if kind not in DERIVATIONS:
        raise UnknownDerivationError(f"unknown derivation type '{kind}'")
    if not records:
        return not_computable(kind, "no runs supplied")
    return DERIVATIONS[kind](records, entry, eligibility, config)
