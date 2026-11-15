"""Command line for the sealed record.

    PYTHONPATH=src python -m seal.cli seal data_tenant
    PYTHONPATH=src python -m seal.cli seal data_tenant/<route>
    PYTHONPATH=src python -m seal.cli verify seal_<run_id>.json --record <path> --evidence-root <path>
    PYTHONPATH=src python -m seal.cli verify --chain output/sealed/<route>

verify_main is the same verify without the word "verify", for the
standalone facilityops-verify command. Exit codes: 0 intact, 1 tampered,
2 could not verify.

Only seal uses the report engine and the repo's config, and it imports them
inside its own functions. verify must run without either, so nothing at the
top of this file loads them.

seal
----
Seals every run of a route in order, each seal linked to the one before it.
Built for the data_tenant layout:

    data_tenant/<route>/<run_folder>/run.json
    data_tenant/<route>/<run_folder>/evidence/...

and writes a tree that mirrors it:

    output/sealed/<route>/<run_folder>/report_<run_id>.pdf
                                       report_<run_id>.manifest.json
                                       seal_<run_id>.json
                                       seal_<run_id>.record.json

The .record.json is the record in canonical form, kept so verify can name
the fields that changed.

A route is a folder whose subfolders hold run.json. Its runs are sealed in
folder-name order, which is date order since the names start with the date
and time. sequence is the run's place in that order, so a run deleted later
shows as a skipped number.

Recorded evidence paths start with the run_id once the prefix is off, so the
evidence root is the route folder.

The first call for a run renders its report and manifest from that run.json
and evidence, then seals all four. Once a seal exists the run is left alone:
no new render, no new seal. It still counts as the previous run for the next
one. A report with no seal beside it was never linked, so it is rendered
again.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from common import OUTPUT_DIR, PROJECT_ROOT
from seal.canonical import CanonicalError, load_json
from seal.create import (
    SealError, build_seal, run_id_of, seal_chain, seal_path, write_record_copy, write_seal,
)
from seal.verify import COULD_NOT_VERIFY, VerifyError, format_report, result, verify_chain, verify_seal

RECORD_NAME = "run.json"


# --- seal -------------------------------------------------------------------


def run_errors() -> tuple[type[Exception], ...]:
    """Everything one run can fail with, from reading the record to writing the seal."""
    from common.loader import RecordParseError
    from common.paths import ConfigError

    return (CanonicalError, SealError, RecordParseError, ConfigError, OSError)


def run_folders(route: Path) -> list[Path]:
    """The route's run folders, in name order."""
    return sorted(p for p in route.iterdir() if (p / RECORD_NAME).is_file())


def find_routes(path: Path) -> list[Path]:
    """Every route folder at or under path. Run folders are not searched."""
    if run_folders(path):
        return [path]
    routes: list[Path] = []
    for folder in sorted(p for p in path.iterdir() if p.is_dir()):
        if not (folder / RECORD_NAME).is_file():
            routes += find_routes(folder)
    return routes


def read_record(record_path: Path) -> dict:
    """The raw record, which must be a JSON object."""
    record = load_json(record_path)
    if not isinstance(record, dict):
        raise SealError(f"root is {type(record).__name__}, expected a JSON object")
    return record


def render_configs(record: dict) -> dict:
    """Every config file a render reads, by path from the project root, parsed.

    The five fixed files go in whether or not their sections are on, so no
    value that mattered is missed. The facility's sign-off file goes in when it
    has one, and so does each standards pack its tenant enables. YAML dates
    become ISO text, as the manifest does, since JSON has no date.
    """
    from common.paths import CONFIG_PATH, load_config
    from common.signoff import settings as signoff_settings
    from report.cli import ACTION_MAPPING_CONFIG, AGENTS_CONFIG, DERIVATIONS_CONFIG, TENANTS_CONFIG
    from standards.packs import enabled_packs, pack_path

    paths = [CONFIG_PATH, DERIVATIONS_CONFIG, AGENTS_CONFIG, ACTION_MAPPING_CONFIG, TENANTS_CONFIG]

    facility_id = record.get("facility_id")
    if isinstance(facility_id, str) and facility_id.strip():
        rows_dir = signoff_settings(load_config(CONFIG_PATH), PROJECT_ROOT).rows_dir
        signoff = rows_dir / f"{facility_id.strip()}.yaml"
        if signoff.exists():
            paths.append(signoff)
        paths += [pack_path(p) for p in enabled_packs(load_config(TENANTS_CONFIG), facility_id.strip())]

    return {
        path.relative_to(PROJECT_ROOT).as_posix(): json.loads(json.dumps(load_config(path), default=str))
        for path in paths
    }


def seal_run(
    run_folder: Path, record: dict, out_folder: Path, chain: dict,
    prefix: str, sealed_by: str,
) -> str:
    """Render one run's report and seal all four together. Returns a summary line."""
    from report.cli import run_pipeline
    from report.manifest import write_manifest

    record_path = run_folder / RECORD_NAME
    route_folder = run_folder.parent

    configs = render_configs(record)
    artifacts = run_pipeline(record_path, route_folder, out_folder)
    manifest_path = write_manifest(artifacts.manifest, out_folder)

    seal = build_seal(
        record, route_folder, prefix, sealed_by, artifacts.pdf_path, manifest_path, chain,
        configs,
    )
    # The copy first: the seal file is what marks a run sealed, so a run that
    # stops in between has no seal and is sealed again next time.
    write_record_copy(record, out_folder)
    write_seal(seal, out_folder)

    files = seal["evidence"]["files"]
    missing = sum(1 for entry in files if entry["sha256"] is None)
    line = f"sealed, {len(files)} files, {missing} missing"
    if chain.get("chain_broken"):
        line += f", CHAIN BROKEN: {chain['reason']}"
    return line


def seal_route(route: Path, out: Path, prefix: str, sealed_by: str) -> dict[str, int]:
    """Seal a route's runs in order. Returns how many were sealed, already sealed, failed."""
    errors = run_errors()
    counts = {"sealed": 0, "already sealed": 0, "failed": 0}
    previous: tuple[str, Path] | None = None    # run_id and seal path of the run before

    print(f"route {route}")
    for sequence, run_folder in enumerate(run_folders(route), start=1):
        out_folder = out / run_folder.name
        label = f"  {sequence:>3}  {run_folder.name}"

        try:
            record = read_record(run_folder / RECORD_NAME)
            run_id = run_id_of(record)
        except errors as error:
            print(f"{label}   FAILED: {error}", file=sys.stderr)
            counts["failed"] += 1
            # No run_id to go on, so the next run looks for a seal under the
            # folder name, finds none, and records the break.
            previous = (run_folder.name, out_folder / f"seal_{run_folder.name}.json")
            continue

        existing = seal_path(out_folder, run_id)
        if existing.exists():
            print(f"{label}   already sealed")
            counts["already sealed"] += 1
        else:
            previous_run_id, previous_seal = previous or (None, None)
            chain = seal_chain(sequence, previous_run_id, previous_seal)
            try:
                print(f"{label}   {seal_run(run_folder, record, out_folder, chain, prefix, sealed_by)}")
                counts["sealed"] += 1
            except errors as error:
                print(f"{label}   FAILED: {error}", file=sys.stderr)
                counts["failed"] += 1

        previous = (run_id, existing)
    return counts


def seal_command(args: argparse.Namespace) -> int:
    """Seal every route at or under args.path."""
    from common.paths import DEFAULT_CONFIG, PATH_PREFIX, setting

    if not args.path.is_dir():
        print(f"error: {args.path}: no such folder", file=sys.stderr)
        return 1
    if (args.path / RECORD_NAME).is_file():
        print(f"error: {args.path} is a run folder; pass its route folder so its runs are "
              f"sealed in order", file=sys.stderr)
        return 1

    routes = find_routes(args.path)
    if not routes:
        print(f"error: no route folders under {args.path}", file=sys.stderr)
        return 1

    prefix = PATH_PREFIX if args.path_prefix is None else args.path_prefix
    sealed_by = f"facilityops-report {setting(DEFAULT_CONFIG, 'provenance', 'engine_version')}"
    totals = {"sealed": 0, "already sealed": 0, "failed": 0}
    for route in routes:
        # Mirror the route's place under the given path; a route passed
        # directly keeps its own name.
        relative = route.relative_to(args.path) if route != args.path else Path(route.name)
        for key, count in seal_route(route, args.out / relative, prefix, sealed_by).items():
            totals[key] += count

    print(", ".join(f"{count} {key}" for key, count in totals.items()))
    return 1 if totals["failed"] else 0


# --- verify -----------------------------------------------------------------


def verify_command(args: argparse.Namespace) -> int:
    """Check one seal and print what moved, or a route's chain with --chain. Returns 0, 1 or 2."""
    if args.chain is not None:
        return chain_command(args.chain)
    needed = [name for name, value in (
        ("a seal", args.seal), ("--record", args.record), ("--evidence-root", args.evidence_root),
    ) if value is None]
    if needed:
        print(f"error: checking one seal needs {', '.join(needed)}; or pass --chain <sealed route folder>",
              file=sys.stderr)
        return COULD_NOT_VERIFY
    try:
        run_id, checks = verify_seal(args.seal, args.record, args.evidence_root)
    except VerifyError as error:
        print(f"SEAL VERIFICATION — {args.seal}")
        print(f"  seal              {error}")
        print("")
        print("RESULT: COULD NOT VERIFY — the seal cannot be read")
        return COULD_NOT_VERIFY

    result_line, code = result(checks)
    print(format_report(run_id, checks, result_line))
    return code


def chain_command(folder: Path) -> int:
    """Check every seal of one route in sequence order and name any hole. Returns 0, 1 or 2."""
    try:
        route, checks = verify_chain(folder)
    except VerifyError as error:
        print(f"CHAIN VERIFICATION — {folder}")
        print(f"  chain             {error}")
        print("")
        print("RESULT: COULD NOT VERIFY — no seal can be read")
        return COULD_NOT_VERIFY

    result_line, code = result(checks)
    print(format_report(route, checks, result_line, "CHAIN VERIFICATION"))
    return code


def add_verify_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("seal", type=Path, nargs="?", help="the seal_<run_id>.json to check")
    parser.add_argument("--record", type=Path, help="the run record it sealed")
    parser.add_argument("--evidence-root", type=Path,
                        help="the folder the sealed evidence paths are relative to")
    parser.add_argument("--chain", type=Path, metavar="FOLDER",
                        help="check every seal in one route's sealed folder, in sequence order, instead")


# --- entry points -----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="seal", description="Seal runs and verify seals.")
    commands = parser.add_subparsers(dest="command", required=True)

    seal = commands.add_parser(
        "seal", help="seal every run of a route in order, each seal linked to the one before it",
    )
    seal.add_argument("path", type=Path, help="data_tenant, or one route folder in it")
    seal.add_argument("--path-prefix", default=None,
                      help="prefix stripped from recorded evidence paths "
                           "(default: paths.path_prefix in config/report.yaml)")
    seal.add_argument("--out", type=Path, default=OUTPUT_DIR / "sealed",
                      help="where the mirrored tree is written (default: output/sealed)")
    seal.set_defaults(run=seal_command)

    verify = commands.add_parser("verify", help="check one seal and name what moved, or a route's chain")
    add_verify_arguments(verify)
    verify.set_defaults(run=verify_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.run(args)


def verify_main(argv: list[str] | None = None) -> int:
    """facilityops-verify seal_<run_id>.json --record <path> --evidence-root <path>
    facilityops-verify --chain <sealed route folder>"""
    parser = argparse.ArgumentParser(
        prog="facilityops-verify", description="Check one seal and name what moved, or a route's chain.",
    )
    add_verify_arguments(parser)
    return verify_command(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
