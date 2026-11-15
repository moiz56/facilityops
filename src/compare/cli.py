"""record-continuity: the continuity score of one route.

    PYTHONPATH=src python -m compare.cli data_tenant/<route>
    PYTHONPATH=src python -m compare.cli data_tenant/<route> --from 2026-07-01 --to 2026-10-01

Reads every run folder in the route folder and the seals in
output/sealed/<route>/, and prints the score as JSON. The schedule,
runs_per_day, comes from the route's tenant in config/tenants.yaml.

Exit codes: 0 the score was given, 1 it could not be.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from common import OUTPUT_DIR, PROJECT_ROOT
from common.paths import ConfigError, load_config
from compare.continuity import ContinuityError, continuity

TENANTS_CONFIG = PROJECT_ROOT / "config" / "tenants.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="record-continuity", description=__doc__.splitlines()[0])
    parser.add_argument("route", type=Path, help="the route folder, data_tenant/<route>")
    parser.add_argument("--from", dest="window_from", type=date.fromisoformat, default=None,
                        help="first day of the window, YYYY-MM-DD (default: the first run's date)")
    parser.add_argument("--to", dest="window_to", type=date.fromisoformat, default=None,
                        help="last day of the window, YYYY-MM-DD (default: the last run's date)")
    parser.add_argument("--sealed", type=Path, default=None,
                        help="the route's sealed folder (default: output/sealed/<route>)")
    parser.add_argument("--tenants", type=Path, default=TENANTS_CONFIG, help="the tenant list with runs_per_day")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    route = args.route.resolve()
    if not route.is_dir():
        print(f"record-continuity: {args.route} is not a folder", file=sys.stderr)
        return 1
    sealed = args.sealed or OUTPUT_DIR / "sealed" / route.name
    try:
        score = continuity(route, sealed, load_config(args.tenants), args.window_from, args.window_to)
    except (ConfigError, ContinuityError) as error:
        print(f"record-continuity: {error}", file=sys.stderr)
        return 1
    print(json.dumps(score, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
