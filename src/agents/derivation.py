"""Eligibility and the derivations. The only place arithmetic happens."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Sequence

from common.paths import parse_filename, resolve_evidence
from common.schema import Checkpoint, Record
from agents.schema import (
    DerivationConfig, DerivedValues, Eligibility, EligibleValue, Exclusion, ExtendedRecord,
)
from agents.utils import (
    RECORD_SCOPES, UnknownDerivationError, add_zone_checkpoints, by_time, checkpoint_problem, count_by_value,
    empty_group,
    entry_inputs, excluded_entries, field_problem, for_run, format_date, format_timestamp, format_value,
    group_inputs, grouped_result, item_id, lookup, match_condition, no_inputs_reason, not_computable, param,
    read_path, round_value, sensor_field_paths, source_kind, stale_entries,
)


# Extended record

def extend_record(
    records: Sequence[Record], config: DerivationConfig, eligibility: Eligibility | None = None,
) -> ExtendedRecord:
    """Run every derivation in config and wrap the results with the records.

    records are oldest first. eligibility is computed here unless the caller
    already has it (main computes it first so it can print it).
    Raises UnknownDerivationError for a type outside the eleven.
    """
    if eligibility is None:
        eligibility = compute_eligibility(records, config)

    values = {
        name: derive(entry, records, eligibility, config)
        for name, entry in config.derivations.items()
    }
    excluded = {record.run_id: excluded_entries(record, eligibility, config) for record in records}
    stale = [s for record in records for s in stale_entries(record, eligibility, config)]

    return ExtendedRecord(
        records=tuple(records),
        derived=DerivedValues(values=values, excluded=excluded, stale=stale),
        computed_at=datetime.now(timezone.utc).strftime(config.computed_at_format),
        derivation_set_version=config.derivation_set_version,
        extended_record_version=config.extended_record_version,
        config_hash=config.config_hash,
    )


# Eligibility

def compute_eligibility(records: Sequence[Record], config: DerivationConfig) -> Eligibility:
    """eligible_values for every sensor field and every field a derivation reads."""
    field_paths = sensor_field_paths()
    for entry in config.derivations.values():
        inputs = entry_inputs(entry, config)
        if inputs:
            field_paths.append(inputs[0])
    return {path: eligible_values(records, path, config) for path in dict.fromkeys(field_paths)}


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
    record: Record, params: dict, eligibility: Eligibility, config: DerivationConfig,
) -> dict:
    """Compare checkpoint values in one run against a threshold.

    scope is a checkpoint id, or "checkpoints" for all of them.
    delta = value - threshold. Equal to the threshold does not exceed it.
    checkpoint_name is the name on the checkpoint's first entry in the run.
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
        *lookup(eligibility, field_path, "threshold_compare"),
        record.run_id,
        source_kind(params, "threshold_compare"),
    )
    names = {}
    for cp in record.checkpoints:
        names.setdefault(cp.checkpoint_id, cp.checkpoint_name)

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
            "checkpoint_name": names[cid],
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
    for cid in dict.fromkeys(cp.checkpoint_id for cp in record.checkpoints):
        result, reason = compare(cid)
        results[cid] = (
            {"status": "OK", **result} if result
            else {"status": "NOT_COMPUTABLE", "checkpoint_name": names[cid], "reason": reason}
        )

    used = sum(1 for r in results.values() if r["status"] == "OK")
    if used == 0:
        return not_computable("threshold_compare", "no checkpoint in the run has an eligible value")

    exceeded = sum(1 for r in results.values() if r["status"] == "OK" and r["exceeds"])
    return {
        **header,
        "checkpoints": results,
        "exceeds_count": exceeded,               # OK checkpoints that exceed the threshold
        "not_exceeds_count": used - exceeded,    # OK checkpoints that do not
        "inputs_used": used,
        "inputs_excluded": sum(e.count for e in exclusions),
    }


# D2

def condition_count(
    record: Record, params: dict, eligibility: Eligibility, config: DerivationConfig,
) -> dict:
    """Count items in one run where a named condition holds.

    Reads eligibility for "<scope>.<field>", so population, inputs_excluded
    and excluded_items come straight from it. excluded_items has one entry per
    item and reason eligibility left out, in record order: item_id is the
    checkpoint_id (None for findings and sensor alerts, which eligibility does
    not name), reason is eligibility's own, and count how many items it covers
    (a checkpoint visited twice can be 2). The counts sum to inputs_excluded.

    With `by_value` set instead of a condition, it counts every value of that
    field (count_by_value): the output has field and by_value in place of
    condition, count and matching_ids.
    """
    if "by_value" in params:
        population, by_value, exclusions = count_by_value(record, params, eligibility, "condition_count")
        return {
            "derivation": "condition_count",
            "status": "OK",
            "scope": params["scope"],
            "field": params["by_value"],
            "by_value": by_value,
            "population": population,
            "inputs_excluded": sum(e.count for e in exclusions),
            "excluded_items": [
                {"item_id": e.scope, "reason": e.reason, "count": e.count} for e in exclusions
            ],
        }

    population, matching_ids, exclusions = match_condition(
        record, params, eligibility, config, "condition_count",
    )

    return {
        "derivation": "condition_count",
        "status": "OK",
        "scope": params["scope"],
        "condition": params["condition"],
        "count": len(matching_ids),
        "matching_ids": matching_ids,
        "population": population,
        "inputs_excluded": sum(e.count for e in exclusions),
        "excluded_items": [
            {"item_id": e.scope, "reason": e.reason, "count": e.count} for e in exclusions
        ],
    }


# D3

def proportion(
    record: Record, params: dict, eligibility: Eligibility, config: DerivationConfig,
) -> dict:
    """Share of items in one run where a named condition holds.

    Numerator is the matching items, denominator is every eligible item.
    """
    denominator, numerator_ids, exclusions = match_condition(
        record, params, eligibility, config, "proportion",
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
        "inputs_excluded": sum(e.count for e in exclusions),
    }


# D4

def group_mean(
    record: Record, params: dict, eligibility: Eligibility, config: DerivationConfig,
) -> dict:
    """Mean of a field per group, over one run (settings: derivations.yaml).

    A group whose inputs were all excluded is NOT_COMPUTABLE with no mean.
    """
    groups, exclusions = group_inputs(record, params, eligibility, "group_mean")
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

    add_zone_checkpoints(record, params, results)
    return grouped_result("group_mean", params, results, record, exclusions)


# D5

def group_max(
    record: Record, params: dict, eligibility: Eligibility, config: DerivationConfig,
) -> dict:
    """Maximum of a field per group, over one run (settings: derivations.yaml).

    On a tie, source_id is the earliest by timestamp and tied_with lists the rest.
    """
    groups, exclusions = group_inputs(record, params, eligibility, "group_max")
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
            "inputs_stale": sum(1 for v in g["values"] if v.stale),
        }

    add_zone_checkpoints(record, params, results)
    return grouped_result("group_max", params, results, record, exclusions)


# D6

def rank_top_n(
    record: Record, params: dict, eligibility: Eligibility, config: DerivationConfig,
) -> dict:
    """The n highest values in one run, highest first.

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
    values, exclusions = for_run(all_values, all_exclusions, record.run_id, kind)
    if not values:
        return not_computable("rank_top_n", no_inputs_reason(record, all_exclusions))

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
    """Which checkpoints, findings and sensor blocks are in one of the two latest
    runs and not the other.

    run_a is the earlier run, run_b the latest. Checkpoint lists keep route
    order; finding lists keep their run's findings order and hold the ids as
    recorded, run_b's for a finding in both. Block lists are keyed by block,
    each with the checkpoints where it was available, of those on both routes,
    in route order, and counted per block. States what differs only: no trend or direction, so a
    finding only in run_a is absent, not resolved.
    """
    if len(records) < 2:
        return not_computable("run_set_difference", "requires two runs; one supplied")

    run_a, run_b = records[-2], records[-1]
    ids_a = list(dict.fromkeys(cp.checkpoint_id for cp in run_a.checkpoints))
    ids_b = list(dict.fromkeys(cp.checkpoint_id for cp in run_b.checkpoints))

    only_in_a = [cid for cid in ids_a if cid not in ids_b]
    only_in_b = [cid for cid in ids_b if cid not in ids_a]
    in_both = [cid for cid in ids_b if cid in ids_a]

    keys_a = {finding_key(run_a, f.finding_id) for f in run_a.findings}
    keys_b = {finding_key(run_b, f.finding_id) for f in run_b.findings}
    findings_only_in_a = [f.finding_id for f in run_a.findings if finding_key(run_a, f.finding_id) not in keys_b]
    findings_only_in_b = [f.finding_id for f in run_b.findings if finding_key(run_b, f.finding_id) not in keys_a]
    findings_in_both = [f.finding_id for f in run_b.findings if finding_key(run_b, f.finding_id) in keys_a]

    blocks_a = available_blocks(run_a, in_both, config)
    blocks_b = available_blocks(run_b, in_both, config)
    blocks_only_in_a = {block: [cid for cid in cids if cid not in blocks_b[block]] for block, cids in blocks_a.items()}
    blocks_only_in_b = {block: [cid for cid in cids if cid not in blocks_a[block]] for block, cids in blocks_b.items()}
    blocks_in_both = {block: [cid for cid in cids if cid in blocks_a[block]] for block, cids in blocks_b.items()}

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
        "findings_only_in_a": findings_only_in_a,
        "findings_only_in_b": findings_only_in_b,
        "findings_in_both": findings_in_both,
        "count_findings_only_in_a": len(findings_only_in_a),
        "count_findings_only_in_b": len(findings_only_in_b),
        "count_findings_in_both": len(findings_in_both),
        "blocks_only_in_a": blocks_only_in_a,
        "blocks_only_in_b": blocks_only_in_b,
        "blocks_in_both": blocks_in_both,
        "count_blocks_only_in_a": {block: len(cids) for block, cids in blocks_only_in_a.items()},
        "count_blocks_only_in_b": {block: len(cids) for block, cids in blocks_only_in_b.items()},
        "count_blocks_in_both": {block: len(cids) for block, cids in blocks_in_both.items()},
    }


def finding_key(record: Record, finding_id: str) -> str:
    """A finding's id without its run's prefix, the same in every run:
    <run_id>:<checkpoint_id>:<feature> -> <checkpoint_id>:<feature>."""
    return finding_id.removeprefix(f"{record.run_id}:")


def available_blocks(record: Record, checkpoints: list[str], config: DerivationConfig) -> dict[str, list[str]]:
    """Each sensor block's checkpoints, of those given, where its reading passes
    the exclusion rule: the checkpoint was completed, its sensor reading is ok,
    connected and reachable, and the block's own device flag is true. A
    checkpoint id recorded twice is judged by its first entry."""
    by_id = {}
    for checkpoint in record.checkpoints:
        by_id.setdefault(checkpoint.checkpoint_id, checkpoint)
    return {
        block: [cid for cid in checkpoints if checkpoint_problem(by_id[cid], flag, config) is None]
        for flag, block in config.subsystem_flags.items()
    }


# D8

def run_date_range(
    records: Sequence[Record], params: dict, eligibility: Eligibility, config: DerivationConfig,
) -> dict:
    """When the runs took place: the earliest and latest start, the days between,
    and each day a run started on, with its runs.

    Days are the calendar dates as recorded, each in its own offset, with no
    conversion to UTC. span_days counts from the earliest date to the latest,
    so one run, or runs all on one day, give 0. days_without_run is the days in
    that span, both ends included, on which no run started. A run with no start
    time is in no day and counted only in runs_without_start.
    """
    dated = [r for r in records if r.start_time is not None]
    if not dated:
        return not_computable("run_date_range", "no run has a start time")

    first = min(dated, key=lambda r: r.start_time)
    last = max(dated, key=lambda r: r.start_time)
    span_days = (last.start_time.date() - first.start_time.date()).days

    # Each day's runs in the order they started.
    by_day: dict = {}
    for r in sorted(dated, key=lambda r: r.start_time):
        by_day.setdefault(r.start_time.date(), []).append(r)
    days = [
        {
            "date": day.isoformat(),
            "date_formatted": format_date(runs[0].start_time, config),
            "weekday": day.strftime("%A"),
            "run_count": len(runs),
            "run_ids": [r.run_id for r in runs],
        }
        for day, runs in sorted(by_day.items())
    ]

    return {
        "derivation": "run_date_range",
        "status": "OK",
        "run_count": len(dated),
        "runs_without_start": len(records) - len(dated),
        "earliest": format_timestamp(first.start_time, config),
        "latest": format_timestamp(last.start_time, config),
        "earliest_formatted": format_date(first.start_time, config),
        "latest_formatted": format_date(last.start_time, config),
        "earliest_run_id": first.run_id,
        "latest_run_id": last.run_id,
        "span_days": span_days,
        "day_count": len(days),
        "days_without_run": span_days + 1 - len(days),
        "days": days,
    }


# D9

def sensor_alert_count(
    record: Record, params: dict, eligibility: Eligibility, config: DerivationConfig,
) -> dict:
    """The sensor alerts in one run: how many, and how many of each severity.

    The expected severities are listed first, each with 0 when no alert has
    it; any other severity found follows, verbatim. A run with no alerts is
    a count of 0: the run recorded none.
    """
    expected = list(params.get("expected") or [])
    found = Counter(alert.severity for alert in record.sensor_alerts)
    severities = expected + [s for s in found if s not in expected]

    return {
        "derivation": "sensor_alert_count",
        "status": "OK",
        "count": len(record.sensor_alerts),
        "by_severity": [
            {"severity": s, "count": found[s], "expected": s in expected} for s in severities
        ],
    }


# D10

def evidence_count(
    record: Record, params: dict, eligibility: Eligibility, config: DerivationConfig,
) -> dict:
    """The evidence images in one run: how many, how many are thermal or RGB,
    and how many resolve or do not. The same per checkpoint, in route order,
    each listed even when it captured none.

    A checkpoint visited twice is one entry, and an image path recorded on
    both visits counts once, as in the lookup database's evidence rows.
    Thermal is read from the file name (common.paths.parse_filename); every
    other image is RGB. An image resolves when it is a readable file under
    config.evidence_root (common.paths.resolve_evidence, the report's own
    check). resolved_count and unresolved_count are None when there is no
    evidence_root.
    """
    images: dict[str, dict] = {}   # checkpoint_id -> its image paths, as an ordered set
    for cp in record.checkpoints:
        images.setdefault(cp.checkpoint_id, {}).update(dict.fromkeys(cp.evidence_images))

    root = config.evidence_root
    by_checkpoint = []
    for cid, uris in images.items():
        thermal = sum(1 for uri in uris if parse_filename(uri, cid)[1])
        resolved = None if root is None else sum(
            1 for uri in uris if resolve_evidence(uri, root, checkpoint_id=cid).readable
        )
        by_checkpoint.append({
            "checkpoint_id": cid,
            "count": len(uris),
            "thermal_count": thermal,
            "rgb_count": len(uris) - thermal,
            "resolved_count": resolved,
            "unresolved_count": None if resolved is None else len(uris) - resolved,
        })

    totals = {key: sum(e[key] for e in by_checkpoint) for key in ("count", "thermal_count", "rgb_count")}
    for key in ("resolved_count", "unresolved_count"):
        totals[key] = None if root is None else sum(e[key] for e in by_checkpoint)

    return {"derivation": "evidence_count", "status": "OK", **totals, "by_checkpoint": by_checkpoint}


# D11

def was_warned(cp: Checkpoint) -> bool:
    """A WARN verdict, or a sensor warning at the checkpoint whatever its verdict."""
    return cp.result_status == "WARN" or bool(cp.sensor is not None and cp.sensor.warnings)


def counted_counts(record: Record) -> dict[str, int]:
    """Each count the robot declares on the run -> the same count taken from the
    run's own lists. Every entry in checkpoints counts, so a checkpoint the
    robot came back to on its round trip counts once per visit."""
    cps = record.checkpoints
    return {
        "total_required_checkpoints": len(cps),
        "total_completed_checkpoints": sum(1 for cp in cps if cp.status == "COMPLETED"),
        "passed_checkpoints": sum(1 for cp in cps if cp.result_status == "PASS"),
        "failed_checkpoints": sum(1 for cp in cps if cp.result_status == "FAIL"),
        "missed_checkpoints": sum(1 for cp in cps if cp.status == "MISSED"),
        "warned_checkpoints": sum(1 for cp in cps if was_warned(cp)),
        "finding_count": len(record.findings),
    }


def count_check(
    record: Record, params: dict, eligibility: Eligibility, config: DerivationConfig,
) -> dict:
    """Whether the counts the robot declared are what the run's lists hold.

    counts: one entry per count declared on the run, in counted_counts' order:
    the field, the count the robot declared there (None when it declared
    nothing), the count taken from the lists, and whether they match (None
    when nothing was declared).

    evidence: one entry per event_log entry with a checkpoint_id and an
    evidence_count, in log order. On a round trip the robot passes a
    checkpoint more than once, each pass with its own event, so the nth event
    for a checkpoint is matched to its nth visit in checkpoints. declared is
    the event's evidence_count; counted is that visit's evidence_images
    length. counted is None, and the entry does not match, when there is no
    such visit. event and visit are positions in event_log and checkpoints.

    mismatch_count is how many entries of both lists do not match.
    """
    counts = []
    for field, counted in counted_counts(record).items():
        declared = getattr(record, field)
        counts.append({
            "field": field, "declared": declared, "counted": counted,
            "matches": None if declared is None else declared == counted,
        })

    visits: dict[str, list[int]] = {}   # checkpoint_id -> its positions in checkpoints, in order
    for j, cp in enumerate(record.checkpoints):
        visits.setdefault(cp.checkpoint_id, []).append(j)
    seen: Counter = Counter()   # events matched so far, per checkpoint
    evidence = []
    for k, event in enumerate(record.event_log):
        if event.checkpoint_id is None or event.evidence_count is None:
            continue
        n = seen[event.checkpoint_id]
        seen[event.checkpoint_id] += 1
        positions = visits.get(event.checkpoint_id, [])
        j = positions[n] if n < len(positions) else None
        counted = None if j is None else len(record.checkpoints[j].evidence_images)
        evidence.append({
            "event": k, "visit": j, "declared": event.evidence_count, "counted": counted,
            "matches": counted == event.evidence_count,
        })

    return {
        "derivation": "count_check",
        "status": "OK",
        "mismatch_count": sum(1 for entry in counts + evidence if entry["matches"] is False),
        "counts": counts,
        "evidence": evidence,
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
    "sensor_alert_count": sensor_alert_count,
    "evidence_count": evidence_count,
    "count_check": count_check,
}


# D1-D6 and D9-D11 work on one run and are run once for every run. D7 and D8 compare runs.
PER_RUN = {
    "threshold_compare", "condition_count", "proportion", "group_mean", "group_max", "rank_top_n",
    "sensor_alert_count", "evidence_count", "count_check",
}


def derive(
    entry: dict, records: Sequence[Record], eligibility: Eligibility, config: DerivationConfig,
) -> dict | list[dict]:
    """Run one derivations.yaml entry. Any type outside the eleven is an error.

    D1-D6 and D9-D11 give a list, one output per run, oldest first, each with its run_id.
    """
    kind = param(entry, "type")
    if kind not in DERIVATIONS:
        raise UnknownDerivationError(f"unknown derivation type '{kind}'")
    if not records:
        return not_computable(kind, "no runs supplied")
    if kind in PER_RUN:
        outputs = []
        for record in records:
            output = DERIVATIONS[kind](record, entry, eligibility, config)
            outputs.append({"derivation": kind, "run_id": record.run_id, **output})
        return outputs
    return DERIVATIONS[kind](records, entry, eligibility, config)
