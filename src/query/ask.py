"""The query surface: ask(question, context, settings) -> envelope.

Scope first, derive second, answer third. B-1 runs exactly as it does under
`agents.main --ask`; the only difference is that its records come from
scope.resolve, so it never sees another tenant's runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agents.b1_analytical import ABSTAIN, answer_question
from agents.database_derivation import open_database
from agents.database_lookup import open_lookup_database
from agents.derivation import compute_eligibility, extend_record
from agents.envelope import envelope
from agents.provider import Provider, make_provider
from agents.schema import B1Config, DerivationConfig, VerificationConfig
from agents.utils import (
    b1_config, derivation_config, eligibility_by_run, hash_configs, load_env,
    print_b1_answer, print_b1_trace, print_runs, provider_config, verification_config,
)
from common.paths import load_config, setting
from query.scope import QueryContext, resolve


@dataclass(frozen=True)
class Settings:
    """Every setting ask needs, loaded once at the entry point and passed in."""

    root: Path
    tenants: dict
    derivation: DerivationConfig
    verification: VerificationConfig
    b1: B1Config
    provider: Provider
    database: Path
    lookup_database: Path


def load_settings(root: Path) -> Settings:
    """Read the config files under root/config. Raises ConfigError on a bad file."""
    config = root / "config"
    paths = load_config(config / "agent_path.yaml")
    report = load_config(config / "report.yaml")
    derivations = load_config(config / "derivations.yaml")
    agents = load_config(config / "agents.yaml")
    tenants = load_config(config / "tenants.yaml")

    load_env(root / ".env")
    return Settings(
        root=root,
        tenants=tenants,
        derivation=derivation_config(report, derivations, hash_configs(report, derivations, tenants)),
        verification=verification_config(agents, report, derivations),
        b1=b1_config(agents),
        provider=make_provider(provider_config(agents)),
        database=root / setting(paths, "agent", "database"),
        lookup_database=root / setting(paths, "agent", "lookup_database"),
    )


def ask(question: str, context: QueryContext, settings: Settings, verbose: bool = False) -> dict:
    """Answer one question from the records this context may see, as an envelope.

    verbose prints what `agents.main --ask` prints, in the same order: the
    runs, the databases, the envelope, attempts, reason, trace and answer.

    Raises ScopeError when the context does not resolve to a scope, and
    ValueError when B-1 is disabled or the question is empty or overlong.
    """
    if not settings.b1.enabled:
        raise ValueError("B-1 is disabled in agents.yaml")

    records = resolve(context, settings.tenants, settings.root)
    if not records:
        output = {"answer": ABSTAIN, "citations": [], "records_consulted": [], "records_in_scope": 0}
        answer_envelope = envelope("B-1", None, settings.b1.prompt_version, [], "OK", output, None)
        if verbose:
            print("no runs in scope")
            print(json.dumps(answer_envelope, indent=2))
        return answer_envelope
    if verbose:
        print_runs(records)

    eligibility = compute_eligibility(records, settings.derivation)
    extended = extend_record(records, settings.derivation, eligibility)
    conn, reused = open_database(extended, settings.derivation, settings.database)
    lookup_conn, lookup_reused = open_lookup_database(
        eligibility_by_run(records, eligibility, settings.derivation),
        settings.derivation, settings.lookup_database,
    )
    if verbose:
        print(f"database: {'reused' if reused else 'built and saved to'} {settings.database}")
        print(f"lookup database: {'reused' if lookup_reused else 'built and saved to'} {settings.lookup_database}")

    result = answer_question(
        question, extended, conn, settings.provider, settings.b1,
        settings.derivation, settings.verification, lookup_conn,
    )
    output = result.output
    if output is not None:
        output = {**output, "records_in_scope": len(records)}
    run_ids = (output or {}).get("records_consulted") or [r.run_id for r in records]
    answer_envelope = envelope(
        "B-1", settings.provider.model, settings.b1.prompt_version, run_ids,
        result.status, output, result.verification,
    )

    if verbose:
        print(json.dumps(answer_envelope, indent=2))
        print(f"\nattempts: {result.attempts}")
        if result.reason:
            print(f"reason: {result.reason}")
        if result.trace:
            print_b1_trace(result.trace)
        print_b1_answer(result, extended)
    return answer_envelope
