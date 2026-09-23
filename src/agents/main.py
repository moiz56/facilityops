"""Entry point for the agent layer.

Config is resolved here and passed down as an argument. No module under
src/agents reads a config file at import time, so this is the only place one is
opened. Helpers live in utils.py; this file is the pipeline.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agents.data_agent import load_corpus
from agents.derivation import compute_eligibility, eligible_values, extend_record
from agents.utils import derivation_config, hash_configs, print_eligibility, print_runs
from common import PROJECT_ROOT
from common.loader import RecordParseError
from common.paths import ConfigError, load_config, setting

#: Where the config files live by default. Locations, not settings: nothing is
#: read from them until main() runs, and --paths / --report replace them.
AGENT_PATH_CONFIG = PROJECT_ROOT / "config" / "agent_path.yaml"
REPORT_CONFIG = PROJECT_ROOT / "config" / "report.yaml"
DERIVATIONS_CONFIG = PROJECT_ROOT / "config" / "derivations.yaml"


def main(argv: list[str] | None = None) -> int:
    """Load a corpus of run records, then run eligibility and the derivations."""
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
        help="run the pipeline: eligible_values on every sensor field, then every derivation",
    )
    parser.add_argument(
        "--hide-eligible", action="store_true",
        help="compute eligibility as usual but do not print it",
    )
    parser.add_argument(
        "--derivations", type=Path, default=DERIVATIONS_CONFIG,
        help="derivation config (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    try:
        paths = load_config(args.paths)
        report = load_config(args.report)
        derivations = load_config(args.derivations)
        config = derivation_config(report, derivations, hash_configs(report, derivations))
        data_dir = args.data_dir or PROJECT_ROOT / setting(paths, "agent", "data_dir")
        records = load_corpus(data_dir, setting(paths, "agent", "record_patterns"))
    except (ConfigError, RecordParseError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if not records:
        print(f"no run records found under {data_dir}", file=sys.stderr)
        return 1
    print_runs(records)

    # Step 1: eligibility, once per field. The derivations use these results
    # and filter nothing.
    try:
        if args.all:
            eligibility = compute_eligibility(records, config)
        elif args.field:
            eligibility = {args.field: eligible_values(records, args.field, config)}
        else:
            eligibility = {}
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if not args.hide_eligible and eligibility:
        print_eligibility(eligibility)

    # Step 2: the extended record, every derivation over that eligibility.
    if args.all:
        try:
            extended = extend_record(records, config, eligibility)
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(json.dumps(extended.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
