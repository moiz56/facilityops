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
    """Every setting the derivation layer needs, read from config once.

    Built by:  utils.derivation_config, called from main.main.
    Passed to: derivation.extend_record, compute_eligibility, eligible_values,
               derive and every derivation (D1-D8), and the helpers that read
               settings: utils.checkpoint_problem, item_id, round_value,
               format_value, format_timestamp, format_date, match_condition,
               entry_inputs, excluded_entries, stale_entries.

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
    """One value that passed the exclusion rule, so a derivation may use it.

    Created by: derivation.eligible_values (sensor fields) and
                derivation.record_values (record fields).
    Read by:    every derivation through utils.lookup and utils.for_run,
                utils.group_inputs, utils.by_time, utils.match_condition,
                and the eligibility printout (utils.print_block, print_summary).
    """

    run_id: str
    kind: str               # checkpoint, sample, finding or sensor_alert
    source_id: str          # checkpoint_id, finding_id, or sample_0042 by position
    zone: str | None
    timestamp: datetime | None
    value: object           # a number for sensor fields, the recorded value otherwise
    stale: bool


@dataclass(frozen=True)
class Exclusion:
    """Values the exclusion rule left out, and why.

    One entry per run, kind, checkpoint, zone and reason, with a count.

    Created by: derivation.eligible_values and derivation.record_values.
    Read by:    utils.for_run, utils.group_inputs, utils.no_inputs_reason,
                threshold_compare (per-checkpoint reasons), inputs_excluded in
                every derivation, and the eligibility printout.
    """

    run_id: str
    kind: str
    scope: str | None       # the checkpoint that decided it
    zone: str | None
    block: str
    reason: str
    count: int


# Field path -> (eligible values, exclusions).
# Built by derivation.compute_eligibility (called from main.main, or from
# extend_record when not passed in). Passed to derive and every derivation,
# utils.excluded_entries, utils.stale_entries and utils.print_eligibility.
Eligibility = dict[str, tuple[list[EligibleValue], list[Exclusion]]]


@dataclass(frozen=True)
class DerivedValues:
    """Everything extend_record computed, held inside an ExtendedRecord.

    Built by: derivation.extend_record.
    Read by:  ExtendedRecord.to_dict.

    values:   derivation instance name -> its output
    excluded: {scope, block, reason, affected_derivations} per exclusion
    stale:    {scope, block, affected_derivations} per stale input used
    """

    values: dict[str, dict]
    excluded: list[dict]
    stale: list[dict]


@dataclass(frozen=True)
class ExtendedRecord:
    """The records, untouched, with the values derived from them (section 5).

    Built by: derivation.extend_record, called from main.main.
    Read by:  main.main (printed with to_dict), and later verify_numeric and
              fill_slots (sections 6 and 10.2), not written yet.
    """

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
    """Every setting verify_numeric needs, read from config once.

    Built by:  utils.verification_config, called from the entry point.
    Passed to: verification.verify_numeric, as its third argument.

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

    Built by: verification.verify_numeric.
    Read by:  the agents and the envelope, not written yet.

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
