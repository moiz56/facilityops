"""Entry point for the agent layer.

Config is resolved here and passed down as an argument. No module under
src/agents reads a config file at import time, so this is the only place one is
opened. Helpers live in utils.py; this file is the pipeline:

  1. eligibility, once per field
  2. the extended record: every derivation over that eligibility
  3. --verify / --fill: verification and slot filling, by hand
  4. --router / --ask: B-1, over the database built from the extended record

--summary RUN, --section-intros RUN, --item-notes RUN and --coverage RUN are
separate: B-2's executive summary, section introductions, item notes or
coverage statement for that one run, from that run alone. --plan RUN is B-3's
action plan for one run, the same way. The corpus is not loaded.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from agents.b1_analytical import answer_question
from agents.b1_analytical_router_derived import RouteError, route_question
from agents.b1_analytical_router_lookup import route_lookup
from agents.b1_retrieval_router import RetrievalError, classify_question
from agents.b1_answer_pdf import write_answer_pdf
from agents.b2_narrative import run_coverage, run_item_notes, run_section_intros, run_summary
from agents.b3_action import run_plan
from agents.data_agent import load_corpus
from agents.database_derivation import open_database
from agents.database_lookup import open_lookup_database
from agents.derivation import compute_eligibility, eligible_values, extend_record
from agents.envelope import envelope
from agents.provider import ProviderError, make_provider
from agents.schema import Template
from agents.slots import fill_slots
from agents.utils import (
    b1_config, derivation_config, eligibility_by_run, hash_configs, load_env,
    print_b1_answer, print_b1_trace, print_eligibility, print_runs, provider_config, verification_config,
)
from agents.verification import verify_numeric
from common import PROJECT_ROOT
from common.loader import RecordParseError, load_record
from common.paths import ConfigError, load_config, setting

#: Where the config files live by default. Locations, not settings: nothing is
#: read from them until main() runs, and --paths / --report replace them.
AGENT_PATH_CONFIG = PROJECT_ROOT / "config" / "agent_path.yaml"
REPORT_CONFIG = PROJECT_ROOT / "config" / "report.yaml"
DERIVATIONS_CONFIG = PROJECT_ROOT / "config" / "derivations.yaml"
B2_DERIVATIONS_CONFIG = PROJECT_ROOT / "config" / "b2_derivations.yaml"   # B-2's own set; B-1 never reads it
ACTION_MAPPING_CONFIG = PROJECT_ROOT / "config" / "action_mapping.yaml"    # B-3's feature -> category mapping
AGENTS_CONFIG = PROJECT_ROOT / "config" / "agents.yaml"
ENV_FILE = PROJECT_ROOT / ".env"   # API keys; see provider.make_provider


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FacilityOps agent layer")
    parser.add_argument("--paths", type=Path, default=AGENT_PATH_CONFIG,
                        help="path config to read (default: %(default)s)")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="directory holding the run directories; overrides agent.data_dir")
    parser.add_argument("--report", type=Path, default=REPORT_CONFIG,
                        help="report config holding the sensor settings (default: %(default)s)")
    parser.add_argument("--b2-derivations", type=Path, default=B2_DERIVATIONS_CONFIG,
                        help="B-2's derivation config, read by --summary (default: %(default)s)")
    parser.add_argument("--derivations", type=Path, default=DERIVATIONS_CONFIG,
                        help="derivation config (default: %(default)s)")
    parser.add_argument("--agents", type=Path, default=AGENTS_CONFIG,
                        help="agent config (default: %(default)s)")
    parser.add_argument("--output", type=Path, default=None,
                        help="where --all writes the extended record; overrides agent.extended_record")

    parser.add_argument("--field", default=None,
                        help="field path to run eligible_values on, e.g. environment.temperature_c")
    parser.add_argument("--all", action="store_true",
                        help="run eligibility on every field, then every derivation, and write the extended record")
    parser.add_argument("--hide-eligible", action="store_true",
                        help="compute eligibility as usual but do not print it")
    parser.add_argument("--eligibility-json", type=Path, default=None, metavar="PATH",
                        help="write eligibility (every field, or --field's) to PATH as JSON")
    parser.add_argument("--verify", metavar="TEXT", default=None,
                        help="verify the numeric tokens in TEXT against the extended record")
    parser.add_argument("--fill", metavar="TEXT", default=None,
                        help="fill the {{ slot }} placeholders in TEXT from the extended record, then verify it")
    parser.add_argument("--slot", metavar="ID=PATH", action="append", default=[],
                        help="where a --fill slot's value comes from; repeat once per slot")
    parser.add_argument("--classify", metavar="QUESTION", default=None,
                        help="run B-1's retrieval router on QUESTION: derivation, lookup or abstain")
    parser.add_argument("--router", metavar="QUESTION", default=None,
                        help="run B-1's retrieval router on QUESTION, then the derivation or lookup router it picks, and print the route")
    parser.add_argument("--ask", metavar="QUESTION", default=None,
                        help="answer QUESTION with B-1, print the answer and its trace, and write it as a PDF")
    parser.add_argument("--summary", metavar="RUN", type=Path, default=None,
                        help="write B-2's executive summary for one run: its directory or its run.json")
    parser.add_argument("--section-intros", metavar="RUN", type=Path, default=None,
                        help="write B-2's section introduction for each zone of one run: its directory or its run.json")
    parser.add_argument("--item-notes", metavar="RUN", type=Path, default=None,
                        help="write B-2's item note for each checkpoint of one run: its directory or its run.json")
    parser.add_argument("--coverage", metavar="RUN", type=Path, default=None,
                        help="write B-2's coverage statement for one run: its directory or its run.json")
    parser.add_argument("--plan", metavar="RUN", type=Path, default=None,
                        help="write B-3's action plan for one run: its directory or its run.json")
    parser.add_argument("--mapping", type=Path, default=ACTION_MAPPING_CONFIG,
                        help="action mapping B-3 reads (default: %(default)s)")
    return parser.parse_args(argv)


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    """Load a corpus of run records, then run eligibility, the derivations and the agents asked for."""
    args = parse_args(argv)
    agent_run = bool(args.verify or args.fill or args.classify or args.router or args.ask)
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
    except ConfigError as error:
        return fail(str(error))

    if args.summary:
        return summarise(args.summary, paths, report, args.b2_derivations, agents)
    if args.section_intros:
        return introduce(args.section_intros, paths, report, args.b2_derivations, agents)
    if args.item_notes:
        return annotate(args.item_notes, paths, report, args.b2_derivations, agents)
    if args.coverage:
        return state_coverage(args.coverage, paths, report, args.b2_derivations, agents)
    if args.plan:
        return action_plan(args.plan, paths, report, args.b2_derivations, agents, args.mapping)

    try:
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
        if needs_record or (args.eligibility_json and not args.field):
            eligibility = compute_eligibility(records, config)
        elif args.field:
            eligibility = {args.field: eligible_values(records, args.field, config)}
        else:
            eligibility = {}
    except ValueError as error:
        return fail(str(error))
    if eligibility and not args.hide_eligible and not agent_run:
        print_eligibility(eligibility)
    if args.eligibility_json:
        args.eligibility_json.parent.mkdir(parents=True, exist_ok=True)
        args.eligibility_json.write_text(json.dumps(
            eligibility_by_run(records, eligibility, config), indent=2, default=str,
        ) + "\n", encoding="utf-8")
        print(f"eligibility written to {args.eligibility_json}")

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

    # Step 4: B-1. The derivation and lookup databases are opened once for
    # both flags; a saved copy is loaded into memory when nothing it is built
    # from has changed.
    if args.classify or args.router or args.ask:
        try:
            b1 = b1_config(agents)
            if not b1.enabled:
                return fail("B-1 is disabled in agents.yaml")
            provider = make_provider(provider_config(agents))
            db_path = PROJECT_ROOT / setting(paths, "agent", "database")
            conn, reused = open_database(extended, config, db_path)
            lookup_path = PROJECT_ROOT / setting(paths, "agent", "lookup_database")
            lookup_conn, lookup_reused = open_lookup_database(
                eligibility_by_run(records, eligibility, config), config, lookup_path,
            )
        except (ConfigError, ProviderError, ValueError, sqlite3.Error, OSError) as error:
            return fail(str(error))
        print(f"database: {'reused' if reused else 'built and saved to'} {db_path}")
        print(f"lookup database: {'reused' if lookup_reused else 'built and saved to'} {lookup_path}")

        if args.classify:
            try:
                retrieval = classify_question(args.classify.strip(), provider, b1, config)
            except (ProviderError, RetrievalError) as error:
                return fail(str(error))
            print(json.dumps(asdict(retrieval), indent=2))

        if args.router:
            question = args.router.strip()
            try:
                retrieval = classify_question(question, provider, b1, config)
                print(json.dumps(asdict(retrieval), indent=2))
                if retrieval.kind == "derivation":
                    print(json.dumps(asdict(route_question(question, extended.records, provider, b1, config)), indent=2))
                elif retrieval.kind == "lookup":
                    print(json.dumps(asdict(route_lookup(question, extended.records, provider, b1)), indent=2))
            except (ProviderError, RetrievalError, RouteError) as error:
                return fail(str(error))

        if args.ask:
            try:
                result = answer_question(args.ask, extended, conn, provider, b1, config, verification, lookup_conn)
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
                    PROJECT_ROOT / setting(paths, "agent", "evidence_root"),
                )
            except (ConfigError, OSError) as error:
                return fail(f"the answer PDF was not written: {error}")
            print(f"\nanswer written to {pdf}")

    return 0


def run_record(run: Path, paths: dict) -> Path | None:
    """The record file for one run: run itself if it is a file, else the one inside its directory."""
    if run.is_file():
        return run
    # The record patterns are relative to the data directory (*/run.json);
    # for one run's directory, drop the leading */.
    found = [run / p.split("/", 1)[1] for p in setting(paths, "agent", "record_patterns")]
    return next((path for path in found if path.is_file()), None)


def b2_run(
    run: Path, paths: dict, report: dict, b2_derivations: Path, agents: dict, call,
    agent: str = "B-2", file: str = "extended_record",
) -> tuple:
    """Load one run, call an agent on it (B-2, or B-3), and write the run's extended record
    to <b2_output>/<run_id>_<file>.json.

    Returns (result, None), or (None, exit status) when it failed.
    """
    found = run_record(run, paths)
    if found is None:
        return None, fail(f"no run record found at {run}")
    try:
        record = load_record(found)
        extended, result = call(record, report, load_config(b2_derivations), agents)
        output = PROJECT_ROOT / setting(paths, "agent", "b2_output") / f"{record.run_id}_{file}.json"
    except (ConfigError, RecordParseError, ValueError) as error:
        return None, fail(str(error))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(extended.to_dict(), indent=2) + "\n", encoding="utf-8")
    if result is None:
        return None, fail(f"{agent} is disabled in agents.yaml")
    return result, None


def summarise(run: Path, paths: dict, report: dict, b2_derivations: Path, agents: dict) -> int:
    """--summary: B-2's executive summary for one run, derived from that run alone
    with B-2's own derivation set (b2_derivations.yaml)."""
    result, status = b2_run(run, paths, report, b2_derivations, agents, run_summary)
    if result is None:
        return status
    print(json.dumps(result, indent=2))
    print(f"\n[{result['status']}]\n" + (result["output"]["text"] if result["output"] else "(nothing shown)"))
    return 0


def introduce(run: Path, paths: dict, report: dict, b2_derivations: Path, agents: dict) -> int:
    """--section-intros: B-2's section introduction for each zone of one run."""
    intros, status = b2_run(run, paths, report, b2_derivations, agents, run_section_intros)
    if intros is None:
        return status
    print(json.dumps(intros, indent=2))
    for zone, result in intros.items():
        print(f"\n[{result['status']}] {zone}\n" + (result["output"]["text"] if result["output"] else "(nothing shown)"))
    return 0


def annotate(run: Path, paths: dict, report: dict, b2_derivations: Path, agents: dict) -> int:
    """--item-notes: B-2's item note for each checkpoint of one run, in record order."""
    notes, status = b2_run(run, paths, report, b2_derivations, agents, run_item_notes)
    if notes is None:
        return status
    print(json.dumps(notes, indent=2))
    for j, result in enumerate(notes):
        print(f"\n[{result['status']}] checkpoints[{j}]\n" + (result["output"]["text"] if result["output"] else "(nothing shown)"))
    return 0


def state_coverage(run: Path, paths: dict, report: dict, b2_derivations: Path, agents: dict) -> int:
    """--coverage: B-2's coverage statement for one run. Images resolve against agent.evidence_root."""
    evidence_root = PROJECT_ROOT / setting(paths, "agent", "evidence_root")
    result, status = b2_run(run, paths, report, b2_derivations, agents,
                            lambda *args: run_coverage(*args, evidence_root))
    if result is None:
        return status
    print(json.dumps(result, indent=2))
    print(f"\n[{result['status']}]\n" + (result["output"]["text"] if result["output"] else "(nothing shown)"))
    return 0


def action_plan(run: Path, paths: dict, report: dict, b2_derivations: Path, agents: dict, mapping: Path) -> int:
    """--plan: B-3's action plan for one run."""
    try:
        table = load_config(mapping)
    except ConfigError as error:
        return fail(str(error))
    result, status = b2_run(run, paths, report, b2_derivations, agents,
                            lambda *args: run_plan(*args, table), agent="B-3", file="plan_extended_record")
    if result is None:
        return status
    print(json.dumps(result, indent=2))
    print(f"\n[{result['status']}]\n" + (result["output"]["text"] if result["output"] else "(nothing shown)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
