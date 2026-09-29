"""The envelope every agent returns (section 9).

One shape for all three agents, so downstream code reads them the same way.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agents.schema import VerificationResult

AGENT_VERSION = "0.2.0"
STATUSES = ("OK", "DEGRADED_TEMPLATE_ONLY", "REFUSED_UNVERIFIABLE", "PROVIDER_UNAVAILABLE")


def envelope(
    agent: str, model: str | None, prompt_version: str, source_run_ids: list[str], status: str,
    output: dict | None, verification: VerificationResult | None,
) -> dict:
    """Wrap one agent output (section 9).

    model is "provider/model-name", or None when no model was called.
    Raises ValueError for a status outside the closed set, or an OK whose
    verification did not pass (section 9.1).
    """
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}, got {status!r}")
    if status == "OK" and verification is not None and not verification.passed:
        raise ValueError("status cannot be OK when verification did not pass")

    return {
        "agent": agent,
        "agent_version": AGENT_VERSION,
        "model": model,
        "prompt_version": prompt_version,
        "source_run_ids": source_run_ids,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "output": output,
        "verification": None if verification is None else {
            "method": verification.method,
            "numeric_tokens_emitted": verification.tokens_emitted,
            "numeric_tokens_verified": verification.tokens_verified,
            "derived_values_used": verification.derived_values_used,
            "regeneration_attempts": verification.regeneration_attempts,
        },
    }
