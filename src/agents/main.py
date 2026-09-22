"""Entry point for the agent layer.

Config is resolved here and passed down as an argument. No module under
src/agents reads a config file at import time, so this is the only place one is
opened.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agents.data_agent import load_corpus
from common import PROJECT_ROOT
from common.loader import RecordParseError
from common.paths import ConfigError, load_config, setting

#: Where the path config lives by default. A location, not a setting: nothing is
#: read from it until main() runs, and --paths replaces it.
AGENT_PATH_CONFIG = PROJECT_ROOT / "config" / "agent_path.yaml"


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
    args = parser.parse_args(argv)

    try:
        paths = load_config(args.paths)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
