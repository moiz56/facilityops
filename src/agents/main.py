"""Entry point for the agent layer.

Config is resolved here and passed down as an argument. No module under
src/agents reads a config file at import time, so this is the only place one is
opened. Helpers live in utils.py; this file is the pipeline:

  1. eligibility, once per field
  2. the extended record: every derivation over that eligibility
  3. --verify / --fill: verification and slot filling, by hand
  4. --router / --ask: B-1, over the database built from the extended record
  5. --narrate: B-2's report prose for the most recent run
  6. --plan: B-3's action plan for the most recent run
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from agents.b1_analytical import answer_question
from agents.b1_analytical_router import route_question
from agents.b1_answer_pdf import write_answer_pdf
from agents.b2_narrative import narrate
from agents.b3_action import plan
from agents.data_agent import load_corpus
from agents.database import open_database
from agents.derivation import compute_eligibility, eligible_values, extend_record
from agents.envelope import envelope
from agents.provider import ProviderError, make_provider
from agents.schema import Template
from agents.slots import fill_slots
from agents.utils import (
    b1_config, b2_config, b3_config, derivation_config, hash_configs, load_env, print_b1_answer, print_b1_trace,
    print_eligibility, print_runs, provider_config, verification_config,
)
from agents.verification import verify_numeric
from common import PROJECT_ROOT
from common.loader import RecordParseError
from common.paths import ConfigError, load_config, setting

#: Where the config files live by default. Locations, not settings: nothing is
#: read from them until main() runs, and --paths / --report replace them.
AGENT_PATH_CONFIG = PROJECT_ROOT / "config" / "agent_path.yaml"
REPORT_CONFIG = PROJECT_ROOT / "config" / "report.yaml"
DERIVATIONS_CONFIG = PROJECT_ROOT / "config" / "derivations.yaml"
AGENTS_CONFIG = PROJECT_ROOT / "config" / "agents.yaml"
ACTION_MAPPING_CONFIG = PROJECT_ROOT / "config" / "action_mapping.yaml"
ENV_FILE = PROJECT_ROOT / ".env"   # API keys; see provider.make_provider


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FacilityOps agent layer")
    parser.add_argument("--paths", type=Path, default=AGENT_PATH_CONFIG,
                        help="path config to read (default: %(default)s)")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="directory holding the run directories; overrides agent.data_dir")
    parser.add_argument("--report", type=Path, default=REPORT_CONFIG,
                        help="report config holding the sensor settings (default: %(default)s)")
    parser.add_argument("--derivations", type=Path, default=DERIVATIONS_CONFIG,
                        help="derivation config (default: %(default)s)")
    parser.add_argument("--agents", type=Path, default=AGENTS_CONFIG,
                        help="agent config (default: %(default)s)")
    parser.add_argument("--mapping", type=Path, default=ACTION_MAPPING_CONFIG,
                        help="action mapping B-3 reads (default: %(default)s)")
    parser.add_argument("--output", type=Path, default=None,
                        help="where --all writes the extended record; overrides agent.extended_record")

    parser.add_argument("--field", default=None,
                        help="field path to run eligible_values on, e.g. environment.temperature_c")
    parser.add_argument("--all", action="store_true",
                        help="run eligibility on every field, then every derivation, and write the extended record")
    parser.add_argument("--hide-eligible", action="store_true",
                        help="compute eligibility as usual but do not print it")
    parser.add_argument("--verify", metavar="TEXT", default=None,
                        help="verify the numeric tokens in TEXT against the extended record")
    parser.add_argument("--fill", metavar="TEXT", default=None,
                        help="fill the {{ slot }} placeholders in TEXT from the extended record, then verify it")
    parser.add_argument("--slot", metavar="ID=PATH", action="append", default=[],
                        help="where a --fill slot's value comes from; repeat once per slot")
    parser.add_argument("--router", metavar="QUESTION", default=None,
                        help="run B-1's router on QUESTION and print the route")
    parser.add_argument("--ask", metavar="QUESTION", default=None,
                        help="answer QUESTION with B-1, print the answer and its trace, and write it as a PDF")
    parser.add_argument("--narrate", action="store_true",
                        help="write B-2's four sections for the most recent run")
    parser.add_argument("--plan", action="store_true",
                        help="write B-3's action plan for the most recent run")
    return parser.parse_args(argv)


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    """Load a corpus of run records, then run eligibility, the derivations and the agents asked for."""
    args = parse_args(argv)
    agent_run = bool(args.verify or args.fill or args.router or args.ask or args.narrate or args.plan)
    needs_record = args.all or agent_run

    slots = {}
    for entry in args.slot:
        slot_id, sep, path = entry.partition("=")
        if not sep or not slot_id or not path:
            return fail(f"--slot must be ID=PATH, got {entry!r}")
        slots[slot_id.strip()] = path.strip()

    load_env(ENV_FILE)
    try:
        paths = load_config(args.paths)
        report = load_config(args.report)
        derivations = load_config(args.derivations)
        config = derivation_config(report, derivations, hash_configs(report, derivations))
        agents = load_config(args.agents)
        verification = verification_config(agents, report, derivations)
        data_dir = args.data_dir or PROJECT_ROOT / setting(paths, "agent", "data_dir")
        records = load_corpus(data_dir, setting(paths, "agent", "record_patterns"))
        output = args.output or PROJECT_ROOT / setting(paths, "agent", "extended_record")
    except (ConfigError, RecordParseError) as error:
        return fail(str(error))

    if not records:
        return fail(f"no run records found under {data_dir}")
    print_runs(records)

    # Step 1: eligibility, once per field. The derivations use these results
    # and filter nothing.
    try:
        if needs_record:
            eligibility = compute_eligibility(records, config)
        elif args.field:
            eligibility = {args.field: eligible_values(records, args.field, config)}
        else:
            eligibility = {}
    except ValueError as error:
        return fail(str(error))
    if eligibility and not args.hide_eligible and not agent_run:
        print_eligibility(eligibility)

    if not needs_record:
        return 0

    # Step 2: the extended record, every derivation over that eligibility.
    try:
        extended = extend_record(records, config, eligibility)
    except ValueError as error:
        return fail(str(error))
    if args.all:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(extended.to_dict(), indent=2) + "\n", encoding="utf-8")
        print(f"extended record written to {output}")

    # Step 3: verification and slot filling, by hand.
    if args.verify:
        print(json.dumps(verify_numeric(args.verify, extended, verification).to_dict(), indent=2))
    if args.fill:
        try:
            filled = fill_slots(Template("cli", args.fill, slots), extended, config)
        except ValueError as error:
            return fail(str(error))
        print(filled.text)
        for slot in filled.slots:
            start, end = slot.span
            print(f"  {slot.slot_id:<12} {start:>4}-{end:<4} {slot.formatted!r:<28} {slot.source_field}")
        print(json.dumps(verify_numeric(filled.text, extended, verification).to_dict(), indent=2))

    # Step 4: B-1. The database is opened once for both flags; its saved copy
    # is loaded into memory when nothing it is built from has changed.
    if args.router or args.ask:
        try:
            b1 = b1_config(agents)
            if not b1.enabled:
                return fail("B-1 is disabled in agents.yaml")
            provider = make_provider(provider_config(agents))
            db_path = PROJECT_ROOT / setting(paths, "agent", "database")
            conn, reused = open_database(extended, config, db_path)
        except (ConfigError, ProviderError, ValueError, sqlite3.Error, OSError) as error:
            return fail(str(error))
        print(f"database: {'reused' if reused else 'built and saved to'} {db_path}")

        if args.router:
            try:
                route = route_question(args.router.strip(), extended.records, provider, b1, config)
            except (ProviderError, ValueError) as error:
                return fail(str(error))
            print(json.dumps(asdict(route), indent=2))

        if args.ask:
            try:
                result = answer_question(args.ask, extended, conn, provider, b1, config, verification)
            except ValueError as error:
                return fail(str(error))
            run_ids = (result.output or {}).get("records_consulted") or [r.run_id for r in extended.records]
            answer_envelope = envelope(
                "B-1", provider.model, b1.prompt_version, run_ids,
                result.status, result.output, result.verification,
            )
            print(json.dumps(answer_envelope, indent=2))
            print(f"\nattempts: {result.attempts}")
            if result.reason:
                print(f"reason: {result.reason}")
            if result.trace:
                print_b1_trace(result.trace)
            print_b1_answer(result, extended)
            try:
                pdf = write_answer_pdf(
                    args.ask, result, answer_envelope, extended, config, report,
                    PROJECT_ROOT / setting(paths, "agent", "b1_answers"),
                )
            except (ConfigError, OSError) as error:
                return fail(f"the answer PDF was not written: {error}")
            print(f"\nanswer written to {pdf}")

    # Step 5: B-2 writes the report prose for the most recent run.
    if args.narrate:
        try:
            b2 = b2_config(agents)
            if not b2.enabled:
                return fail("B-2 is disabled in agents.yaml: the report keeps its own templated text")
            provider = make_provider(provider_config(agents)) if b2.prose else None
            sections = narrate(extended, len(extended.records) - 1, provider, b2, config, verification)
        except (ConfigError, ValueError) as error:
            return fail(str(error))
        print(json.dumps(sections, indent=2))
        envelopes = [sections["executive_summary"], sections["coverage"],
                     *sections["section_intro"].values(), *sections["item_note"]]
        for env in envelopes:
            text = env["output"]["text"] if env["output"] else "(nothing shown)"
            section = env["output"]["section"] if env["output"] else ""
            print(f"\n[{env['status']}] {section}\n{text}")

    # Step 6: B-3 turns the most recent run's findings into an action plan.
    if args.plan:
        try:
            b3 = b3_config(agents, load_config(args.mapping))
            if not b3.enabled:
                return fail("B-3 is disabled in agents.yaml")
            provider = make_provider(provider_config(agents)) if b3.prose else None
            result = plan(extended, len(extended.records) - 1, provider, b3, config, verification)
        except (ConfigError, ValueError) as error:
            return fail(str(error))
        print(json.dumps(result, indent=2))
        print(f"\n[{result['status']}]\n" + (result["output"]["text"] if result["output"] else "(nothing shown)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
