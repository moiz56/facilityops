"""Entry point for the agent layer.

Config is resolved here and passed down as an argument. No module under
src/agents reads a config file at import time, so this is the only place one is
opened.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import fields
from pathlib import Path

from agents.data_agent import load_corpus
from agents.derivation import DerivationConfig, EligibleValue, Exclusion, eligible_values
from common import PROJECT_ROOT
from common.loader import RecordParseError
from common.paths import ConfigError, load_config, setting
from common.schema import Accelerometer, Environment, Particulate

#: The measurement blocks a sensor reading carries, for --all.
SENSOR_BLOCKS = {
    "accelerometer": Accelerometer,
    "environment": Environment,
    "particulate": Particulate,
}

#: Where the config files live by default. Locations, not settings: nothing is
#: read from them until main() runs, and --paths / --report replace them.
AGENT_PATH_CONFIG = PROJECT_ROOT / "config" / "agent_path.yaml"
REPORT_CONFIG = PROJECT_ROOT / "config" / "report.yaml"


def main(argv: list[str] | None = None) -> int:
    """Load a corpus of run records and report what was read."""
    parser = argparse.ArgumentParser(description="FacilityOps agent layer")
    parser.add_argument(
        "--paths", type=Path, default=AGENT_PATH_CONFIG,
        help="path config to read (default: %(default)s)",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=None,
        help="directory holding the run directories; overrides corpus.data_dir",
    )
    parser.add_argument(
        "--report", type=Path, default=REPORT_CONFIG,
        help="report config holding the sensor settings (default: %(default)s)",
    )
    parser.add_argument(
        "--field", default=None,
        help="field path to run eligible_values on, e.g. environment.temperature_c",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="run eligible_values on every field of every sensor block",
    )
    args = parser.parse_args(argv)

    try:
        paths = load_config(args.paths)
        report = load_config(args.report)
        config = DerivationConfig(
            subsystem_flags=setting(report, "sensor", "subsystem_flags"),
            max_age_seconds=setting(report, "sensor", "max_age_seconds"),
        )
        data_dir = args.data_dir or PROJECT_ROOT / setting(paths, "agent", "data_dir")
        records = load_corpus(data_dir, setting(paths, "agent", "record_patterns"))
    except (ConfigError, RecordParseError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if not records:
        print(f"no run records found under {data_dir}", file=sys.stderr)
        return 1

    for record in records:
        print(
            f"{record.run_id}  checkpoints={len(record.checkpoints)}"
            f"  samples={len(record.sensor_samples)}"
            f"  findings={len(record.findings)}"
        )
    print(f"{len(records)} records, oldest first")

    if args.all:
        field_paths = [
            f"{block}.{field.name}"
            for block, block_type in SENSOR_BLOCKS.items()
            for field in fields(block_type)
        ]
    elif args.field:
        field_paths = [args.field]
    else:
        field_paths = []

    # Fields of one block come from one sensor reading, so they are shown together.
    # Eligibility is still computed per field, as eligible_values is defined.
    by_block: dict[str, dict[str, tuple[list[EligibleValue], list[Exclusion]]]] = {}
    for field_path in field_paths:
        block, _, name = field_path.partition(".")
        try:
            by_block.setdefault(block, {})[name] = eligible_values(records, field_path, config)
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    for block, results in by_block.items():
        print_block(block, results)
    return 0


def print_block(
    block: str, results: dict[str, tuple[list[EligibleValue], list[Exclusion]]],
) -> None:
    """Print one sensor block's eligibility, all its fields side by side.

    A count that is the same for every field is printed once; where the fields
    differ, each field's count is shown.
    """
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


if __name__ == "__main__":
    raise SystemExit(main())
