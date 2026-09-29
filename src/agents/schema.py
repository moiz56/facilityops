"""Data types of the agent layer.

The run record types themselves (Record, Checkpoint, ...) live in
common/schema.py. These are the types the agent layer builds on top of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from common.schema import Record


@dataclass(frozen=True)
class DerivationConfig:
    """Every setting the derivation layer needs, read from config once (utils.derivation_config).

    From report.yaml:
      subsystem_flags:      device flag -> sensor block it governs
      max_age_seconds:      older checkpoint readings are marked stale
      decimals:             field name -> decimal places
    From derivations.yaml:
      significant_figures:  rounding for raw numbers with no decimals entry
      conditions:           name -> {field, equals}
      completed_status:     checkpoint status that passes section 3.2 condition 1
      connected_status:     sensor and sample status that passes condition 2
      status_exempt_fields: checkpoint fields condition 1 is not applied to
      timestamp_format, date_format, sample_id_format, sensor_alert_id_format,
      computed_at_format
      derivations:          instance name -> its entry (type and parameters)
      derivation_set_version, extended_record_version
    From main:
      config_hash:          sha256 of report.yaml and derivations.yaml
    """

    subsystem_flags: dict[str, str]
    max_age_seconds: float
    decimals: dict[str, int]
    significant_figures: int
    conditions: dict[str, dict]
    completed_status: str
    connected_status: str
    status_exempt_fields: list[str]
    timestamp_format: str
    date_format: str
    sample_id_format: str
    sensor_alert_id_format: str
    computed_at_format: str
    derivations: dict[str, dict]
    derivation_set_version: str
    extended_record_version: str
    config_hash: str


@dataclass(frozen=True)
class EligibleValue:
    """One value that passed the exclusion rule, so a derivation may use it."""

    run_id: str
    kind: str               # checkpoint, sample, finding or sensor_alert
    source_id: str          # checkpoint_id, finding_id, or sample_0042 by position
    zone: str | None
    timestamp: datetime | None
    value: object           # a number for sensor fields, the recorded value otherwise
    stale: bool


@dataclass(frozen=True)
class Exclusion:
    """Values the exclusion rule left out, and why: one entry per run, kind,
    checkpoint, zone and reason, with a count."""

    run_id: str
    kind: str
    scope: str | None       # the checkpoint that decided it
    zone: str | None
    block: str
    reason: str
    count: int


# Field path -> (eligible values, exclusions), from derivation.compute_eligibility.
Eligibility = dict[str, tuple[list[EligibleValue], list[Exclusion]]]


@dataclass(frozen=True)
class DerivedValues:
    """Everything extend_record computed, held inside an ExtendedRecord.

    values:   derivation instance name -> its output. D1-D6 give a list, one
              output per run, oldest first, each with run_id; D7 and D8 one dict.
    excluded: run_id -> its {scope, block, reason, affected_derivations} per
              exclusion. Every run has a key, oldest first; [] when nothing was left out
    stale:    {run_id, scope, block, affected_derivations} per stale input used,
              for every run, oldest first
    """

    values: dict[str, dict | list[dict]]
    excluded: dict[str, list[dict]]
    stale: list[dict]


@dataclass(frozen=True)
class ExtendedRecord:
    """The records, untouched, with the values derived from them (section 5)."""

    records: tuple[Record, ...]
    derived: DerivedValues
    computed_at: str                # ISO 8601 UTC
    derivation_set_version: str
    extended_record_version: str
    config_hash: str

    def to_dict(self) -> dict:
        """The serialised shape in section 5.2."""
        return {
            "extended_record_version": self.extended_record_version,
            "computed_at": self.computed_at,
            "derivation_set_version": self.derivation_set_version,
            "config_hash": self.config_hash,
            "source_run_ids": [r.run_id for r in self.records],
            "values": self.derived.values,
            "excluded": self.derived.excluded,
            "stale": self.derived.stale,
        }


@dataclass(frozen=True)
class VerificationConfig:
    """Every setting verify_numeric needs, read from config once (utils.verification_config).

    From agents.yaml verification:
      numeric_tolerance, reject_unclassifiable, record_tolerance_anomalies,
      word_numbers
    From derivations.yaml formats:
      timestamp_format, date_format: so a timestamp in the text is compared
      with the record's timestamps formatted the same way
    From report.yaml:
      decimals: so a record value is compared in the form it is printed in
      engine_version, template_version: the provenance a version token can match
    """

    numeric_tolerance: float
    reject_unclassifiable: bool
    record_tolerance_anomalies: bool
    word_numbers: bool
    timestamp_format: str
    date_format: str
    decimals: dict[str, int]
    engine_version: str
    template_version: str


class TokenClass(StrEnum):
    """The token taxonomy of section 6.2. Returned by verification.classify_token."""

    MEASUREMENT = "measurement"
    COUNT = "count"
    IDENTIFIER = "identifier"
    TIMESTAMP = "timestamp"
    VERSION = "version"
    ORDINAL = "ordinal"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class VerificationResult:
    """What verify_numeric found in a piece of text.

    by_class:    class name -> number of tokens of that class, all seven keys
    failures:    {token, class, position, reason} per token that did not verify
    anomalies:   {token, class, position, source_field, source_value} per token
                 that verified only because of the tolerance; {token, class,
                 position, reason} for an unclassifiable token let through
                 because reject_unclassifiable is off
    """

    method: str                     # template_slot_fill or deterministic
    passed: bool
    tokens_emitted: int
    tokens_verified: int
    by_class: dict[str, int]
    derived_values_used: list[str]
    regeneration_attempts: int
    failures: list[dict]
    anomalies: list[dict]

    def to_dict(self) -> dict:
        """The serialised shape in section 6.5."""
        return {
            "method": self.method,
            "passed": self.passed,
            "tokens_emitted": self.tokens_emitted,
            "tokens_verified": self.tokens_verified,
            "by_class": self.by_class,
            "derived_values_used": self.derived_values_used,
            "regeneration_attempts": self.regeneration_attempts,
            "failures": self.failures,
            "anomalies": self.anomalies,
        }


@dataclass(frozen=True)
class Template:
    """Template text with {{ slot_id }} placeholders, and where each slot's value comes from.

    slots: slot_id -> source path, e.g.
           "derived.values.group_mean_vibration_rms_g[2].groups.checkpoint_1.mean"
    """

    name: str
    text: str
    slots: dict[str, str]


@dataclass(frozen=True)
class FilledSlot:
    """One value placed in the text (section 7.2). Citations are built from these."""

    slot_id: str
    source_field: str
    raw_value: object
    formatted: str
    span: tuple[int, int]      # character offsets in the filled text


@dataclass(frozen=True)
class FilledTemplate:
    """A template with every slot filled (slots.fill_slots); the slots become citations."""

    name: str
    text: str
    slots: tuple[FilledSlot, ...]


@dataclass(frozen=True)
class ProviderConfig:
    """agents.yaml provider settings. The API key is not one: make_provider reads it from the environment."""

    name: str
    model: str
    timeout_seconds: float
    max_retries: int
    retry_backoff_seconds: float


@dataclass(frozen=True)
class B1Config:
    """Every setting B-1 needs, read from agents.yaml once (utils.b1_config).

    From agents.b1_analytical: enabled, prompt_version, max_attempts,
      max_question_chars
    From provider: temperature
    From database: max_rows, query_timeout_seconds (its timeout_seconds)
    """

    enabled: bool
    prompt_version: str
    max_attempts: int
    max_question_chars: int
    temperature: float
    max_rows: int
    query_timeout_seconds: float


@dataclass(frozen=True)
class B2Config:
    """Every setting B-2 needs, read from agents.yaml once (utils.b2_config).

    From agents.b2_narrative: enabled, prose, prompt_version, max_attempts
    From provider: temperature
    """

    enabled: bool
    prose: bool
    prompt_version: str
    max_attempts: int
    temperature: float


@dataclass(frozen=True)
class B3Config:
    """Every setting B-3 needs, read from agents.yaml and action_mapping.yaml once (utils.b3_config).

    From agents.b3_action: enabled, prose, prompt_version, max_attempts
    From provider: temperature
    From action_mapping.yaml: features (feature -> category), category_rank,
      severity_rank
    """

    enabled: bool
    prose: bool
    prompt_version: str
    max_attempts: int
    temperature: float
    features: dict[str, str]
    category_rank: dict[str, int]
    severity_rank: dict[str, int]


@dataclass(frozen=True)
class B1Result:
    """What B-1 produced for one question, before the envelope wraps it.

    status:       OK, DEGRADED_TEMPLATE_ONLY (the prose writer failed, so the
                  answer is the facts code wrote, with no lead-in), REFUSED_UNVERIFIABLE or
                  PROVIDER_UNAVAILABLE (section 9.3)
    output:       {answer, citations, records_consulted} (section 8.1), or None
                  when nothing may be shown
    verification: of the answer that was shown, or None
    sql:          the query that produced it, or None
    attempts:     model calls made: router, SQL writer and prose writer together
    reason:       why nothing was shown, why B-1 abstained, or why the prose failed
    template:     the slot template the answer's facts were filled from, or None.
                  Printed by main; not part of the envelope.
    trace:        what each step was given and gave back, for tracing an
                  answer to its source. Printed by main; not part of the envelope:
                    question
                    router:      calls [{attempt, reply, rejected}], route (the
                                 checked Route)
                    sql_writer:  one per route: route, instances, calls
                                 [{attempt, reply, rejected, summary_sql}], sql,
                                 values (slot -> cell), slots (slot -> path)
                    prose:       calls [{attempt, reply, rejected}], lead (the
                                 lead-in that went above the facts, or None)
                    answer_from: prose, rows (the prose failed), abstain, or
                                 None when nothing was shown
    """

    status: str
    output: dict | None
    verification: VerificationResult | None
    sql: str | None
    attempts: int
    reason: str | None = None
    template: Template | None = None
    trace: dict | None = None
