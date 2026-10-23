"""Entry point for asking the record as one tenant.

Settings are loaded here, once, and passed to ask().

    PYTHONPATH=src python -m query.main --tenant tenant_racks --ask "how many runs are there"
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone

from agents.provider import ProviderError
from common import PROJECT_ROOT
from common.loader import RecordParseError
from common.paths import ConfigError
from query.ask import ask, load_settings
from query.scope import QueryContext, ScopeError


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ask a question of one tenant's records.")
    parser.add_argument("--tenant", required=True, help="the tenant asking, as named in config/tenants.yaml")
    parser.add_argument("--ask", required=True, help="the question")
    parser.add_argument("--facility", action="append", default=[],
                        help="limit to this facility (repeatable); default every facility the tenant owns")
    parser.add_argument("--from", dest="start", help="first run date, YYYY-MM-DD (needs --to)")
    parser.add_argument("--to", dest="end", help="last run date, YYYY-MM-DD (needs --from)")
    parser.add_argument("--asked-by", default="cli", help="who is asking")
    return parser.parse_args(argv)


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if bool(args.start) != bool(args.end):
        return fail("--from and --to go together")

    context = QueryContext(
        tenant_id=args.tenant,
        facility_ids=tuple(args.facility),
        run_window=(args.start, args.end) if args.start else None,
        asked_by=args.asked_by,
        asked_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    try:
        settings = load_settings(PROJECT_ROOT)
        ask(args.ask, context, settings, verbose=True)
    except (ConfigError, ScopeError, ProviderError, RecordParseError, ValueError, sqlite3.Error, OSError) as error:
        return fail(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
