"""Numeric verification (section 6).

Finds every numeric token in a piece of text, classifies it (section 6.2) and
checks it against the extended record. A pure function over text, a record and
settings: it calls nothing and does not know which agent wrote the text.
"""

from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime

from agents.schema import ExtendedRecord, TokenClass, VerificationConfig, VerificationResult

log = logging.getLogger(__name__)

# Numbers written as words. "seven checkpoints" is a count (section 6.2).
WORD_VALUES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
}

ORDINAL_VALUES = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "twentieth": 20,
}


def _either(words) -> str:
    """Longest first, so "seventeen" is tried before "seven"."""
    return "|".join(sorted(words, key=len, reverse=True))


TENS = _either(w for w, n in WORD_VALUES.items() if n % 10 == 0 and 20 <= n <= 90)
ONE_TO_NINE = _either(w for w, n in WORD_VALUES.items() if 1 <= n <= 9)
MONTHS = _either(calendar.month_name[1:])

# What each kind of token looks like.
TIMESTAMP = (
    r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?"   # 2026-07-28T14:41:20-0700
    rf"|\d{{1,2}} (?:{MONTHS}) \d{{4}}"                                        # 28 July 2026
    r"|\d{1,2}:\d{2}(?::\d{2})?"                                               # 14:41:20
)
VERSION = r"v?\d+(?:\.\d+){2,}"                                    # 0.1.0
NAMED_VERSION = r"(?:\w+_)?v\d+(?:\.\d+)*"                         # full_report_v1, v2
NUMBER = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"                 # 1,006.39  -0.078  7
INTEGER = r"\d{1,3}(?:,\d{3})+|\d+"
DIGIT_ORDINAL = r"\d+(?:st|nd|rd|th)"                              # 1st, 22nd
WORD_NUMBER = rf"(?:{TENS})(?:[- ](?:{ONE_TO_NINE}))?|(?:one |a )?hundred|{_either(WORD_VALUES)}"
ORDINAL_WORD = _either(ORDINAL_VALUES)
WORD = r"[A-Za-z0-9]+(?:[_.\-][A-Za-z0-9]+)*"                      # checkpoint_1, run ids

# One pass over the text. Where two kinds could start at the same place, the
# earlier one wins, and each match is consumed whole: the 1 in checkpoint_1 is
# never seen on its own, because the whole word is matched first.
TOKEN_PATTERN = re.compile(
    rf"(?P<timestamp>(?<![\w:])(?:{TIMESTAMP})(?![\w:]))"
    rf"|(?P<version>(?<![\w.])(?:{VERSION})(?!\.?\w))"
    rf"|(?P<number>(?<![\w.])(?:{NUMBER})(?!\.?\w))"
    rf"|(?P<word_number>\b(?:{WORD_NUMBER})\b)"
    rf"|(?P<ordinal_word>\b(?:{ORDINAL_WORD})\b)"
    rf"|(?P<word>{WORD})",
    re.IGNORECASE,
)


def extract_tokens(text: str, word_numbers: bool) -> list[tuple[str, int, int]]:
    """Every numeric token in the text, as (token, start, end), in order.

    A word is a token only if it has a digit in it (checkpoint_1, 1st). Number
    and ordinal words (seven, second) are tokens when word_numbers is on.
    """
    tokens = []
    for match in TOKEN_PATTERN.finditer(text):
        kind = match.lastgroup
        token = match.group()
        if kind == "word" and not any(c.isdigit() for c in token):
            continue
        if kind in ("word_number", "ordinal_word") and not word_numbers:
            continue
        tokens.append((token, match.start(), match.end()))
    return tokens


def classify_token(token: str, context: str) -> TokenClass:
    """The class of one token (section 6.2).

    context is the text before the token. It decides one case: a plain number
    straight after the word "version" (version 1.0) is a version.
    """
    lower = token.lower()
    if re.fullmatch(TIMESTAMP, token, re.IGNORECASE):
        return TokenClass.TIMESTAMP
    if re.fullmatch(VERSION, lower) or re.fullmatch(NAMED_VERSION, lower):
        return TokenClass.VERSION
    if re.fullmatch(NUMBER, token) and re.search(r"\bversion\W*$", context, re.IGNORECASE):
        return TokenClass.VERSION
    if re.fullmatch(DIGIT_ORDINAL, lower) or lower in ORDINAL_VALUES:
        return TokenClass.ORDINAL
    if re.fullmatch(WORD_NUMBER, lower) or re.fullmatch(INTEGER, token):
        return TokenClass.COUNT
    if re.fullmatch(NUMBER, token):
        return TokenClass.MEASUREMENT
    if re.fullmatch(WORD, token) and re.search(r"[a-z_]", lower) and re.search(r"\d", token):
        return TokenClass.IDENTIFIER
    return TokenClass.UNVERIFIABLE


# Keys of a derived value that hold a count (section 6.2: "a count, n,
# population or n_returned field"), plus the other whole-number tallies the
# eight derivations produce. Keys starting count_ (D7) are counts too.
COUNT_KEYS = {
    "count", "n", "population", "n_returned", "n_requested", "numerator",
    "denominator", "run_count", "span_days", "inputs_used", "inputs_excluded",
    "inputs_stale",
}


@dataclass
class Known:
    """What a token can be checked against, gathered once from the extended record.

    Derived entries carry (path, derivation instance name) so a match can be
    cited and listed in derived_values_used. Paths look like 7.2's
    source_field: derived.values.group_max.groups.checkpoint_1.max, or
    records[0].checkpoints[2].sensor.environment.temperature_c.
    """

    derived_formatted: dict[str, tuple[str, str]] = field(default_factory=dict)
    derived_numbers: list[tuple[str, str, float]] = field(default_factory=list)
    derived_counts: list[tuple[str, str, int]] = field(default_factory=list)
    derived_strings: dict[str, tuple[str, str]] = field(default_factory=dict)
    record_formatted: set[str] = field(default_factory=set)
    record_numbers: list[tuple[str, float]] = field(default_factory=list)
    record_counts: set[int] = field(default_factory=set)
    record_strings: set[str] = field(default_factory=set)
    versions: set[str] = field(default_factory=set)
    largest_list: int = 0


def walk(obj: object, path: str):
    """Every value inside obj, containers included, as (path, value).

    Telemetry samples are skipped: the text reaches them only through a
    derivation, which carries the value it used.
    """
    yield path, obj
    if is_dataclass(obj):
        for f in fields(obj):
            if f.name != "sensor_samples":
                yield from walk(getattr(obj, f.name), f"{path}.{f.name}")
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield from walk(value, f"{path}.{key}")
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            yield from walk(value, f"{path}[{i}]")


def last_key(path: str) -> str:
    """records[0].checkpoints[2].sensor.environment.temperature_c -> temperature_c"""
    return path.rsplit(".", 1)[-1]


def is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def renderings(timestamp: datetime, config: VerificationConfig) -> set[str]:
    """The ways a record timestamp can appear in text."""
    return {
        timestamp.strftime(config.timestamp_format),
        timestamp.isoformat(),
        timestamp.strftime("%Y-%m-%d"),
        timestamp.strftime("%H:%M:%S"),
        timestamp.strftime("%H:%M"),
        config.date_format.format(day=timestamp.day, month=timestamp.strftime("%B"), year=timestamp.year),
    }


def gather(extended: ExtendedRecord, config: VerificationConfig) -> Known:
    """Everything in the extended record a token can match."""
    known = Known(largest_list=len(extended.records))

    for i, record in enumerate(extended.records):
        for path, value in walk(record, f"records[{i}]"):
            name = last_key(path)
            if isinstance(value, (list, tuple)):
                known.largest_list = max(known.largest_list, len(value))
            elif is_number(value):
                known.record_numbers.append((path, float(value)))
                if name in config.decimals:
                    known.record_formatted.add(f"{value:.{config.decimals[name]}f}")
                # The run's declared counts: total_completed_checkpoints, finding_count, ...
                if path.count(".") == 1 and name.endswith(("_checkpoints", "_count")):
                    known.record_counts.add(value)
            elif isinstance(value, datetime):
                known.record_strings |= renderings(value, config)
            elif isinstance(value, str):
                known.record_strings.add(value)
                # An evidence path is also known by its file name: cap_0142.
                known.record_strings.add(value.rsplit("/", 1)[-1].rsplit(".", 1)[0])

    for instance, output in extended.derived.values.items():
        for path, value in walk(output, f"derived.values.{instance}"):
            name = last_key(path)
            # Group keys and ids appear in paths: ...groups.checkpoint_1.mean
            for part in re.split(r"[.\[\]]", path):
                known.derived_strings.setdefault(part, (path, instance))
            if isinstance(value, (list, tuple)):
                known.largest_list = max(known.largest_list, len(value))
            elif is_number(value):
                known.derived_numbers.append((path, instance, float(value)))
                if isinstance(value, int) and (name in COUNT_KEYS or name.startswith("count_")):
                    known.derived_counts.append((path, instance, value))
            elif isinstance(value, str):
                known.derived_strings.setdefault(value, (path, instance))
                if name.endswith("_formatted"):
                    known.derived_formatted.setdefault(value, (path, instance))

    known.versions = {
        extended.extended_record_version, extended.derivation_set_version,
        config.engine_version, config.template_version,
    }
    return known


def word_value(word: str) -> int:
    """seven -> 7, twenty-one -> 21, one hundred -> 100."""
    parts = re.split(r"[- ]", word.lower())
    if parts[-1] == "hundred":
        return 100
    return sum(WORD_VALUES[p] for p in parts)


def count_value(token: str) -> int:
    if token[0].isdigit():
        return int(token.replace(",", ""))
    return word_value(token)


def ordinal_value(token: str) -> int:
    if token[0].isdigit():
        return int(re.match(r"\d+", token).group())
    return ORDINAL_VALUES[token.lower()]


def check_token(
    token: str, token_class: TokenClass, known: Known, config: VerificationConfig,
) -> tuple[str | None, str | None, dict | None]:
    """Check one classified token (section 6.2).

    Returns (why it failed, or None if it passed; the derivation instance it
    drew on, or None; {source_field, source_value} if it passed only because
    of the tolerance, or None).
    """
    if token_class == TokenClass.MEASUREMENT:
        return check_measurement(token, known, config)

    if token_class == TokenClass.COUNT:
        value = count_value(token)
        for _, instance, count in known.derived_counts:
            if count == value:
                return None, instance, None
        if value in known.record_counts:
            return None, None, None
        return f"no count field holds {value}", None, None

    if token_class in (TokenClass.IDENTIFIER, TokenClass.TIMESTAMP):
        if token in known.record_strings:
            return None, None, None
        if token in known.derived_strings:
            return None, known.derived_strings[token][1], None
        what = "identifier" if token_class == TokenClass.IDENTIFIER else "timestamp"
        return f"no {what} in the records or the extended record matches it", None, None

    if token_class == TokenClass.VERSION:
        if token in known.versions:
            return None, None, None
        return "matches no version or provenance field", None, None

    if token_class == TokenClass.ORDINAL:
        if 1 <= ordinal_value(token) <= known.largest_list:
            return None, None, None
        return "no item at that position", None, None

    return "could not be classified", None, None


def check_measurement(
    token: str, known: Known, config: VerificationConfig,
) -> tuple[str | None, str | None, dict | None]:
    """A measurement must be a derived value or a record field (section 6.2).

    An exact match is the formatted string or the same number. Otherwise the
    nearest value within the tolerance passes, and is returned as an anomaly
    (section 6.3).
    """
    text = token.replace(",", "")
    value = float(text)

    if text in known.derived_formatted:
        return None, known.derived_formatted[text][1], None
    for path, instance, number in known.derived_numbers:
        if number == value:
            return None, instance, None
    if text in known.record_formatted or any(number == value for _, number in known.record_numbers):
        return None, None, None

    # The 1e-9 keeps float error from failing a difference of exactly the tolerance.
    candidates = list(known.derived_numbers)
    candidates += [(p, None, n) for p, n in known.record_numbers]
    within = [c for c in candidates if abs(c[2] - value) <= config.numeric_tolerance + 1e-9]
    if not within:
        return f"matches no record field or derived value within {config.numeric_tolerance}", None, None
    path, instance, number = min(within, key=lambda c: abs(c[2] - value))
    return None, instance, {"source_field": path, "source_value": number}


def verify_numeric(text: str, extended: ExtendedRecord, config: VerificationConfig) -> VerificationResult:
    """Extract, classify and check every numeric token in the text (section 6).

    passed is true only when every token verified, so tokens_emitted equals
    tokens_verified whenever it is. Text with no tokens passes. method and
    regeneration_attempts are the caller's to set: the verifier does not know
    how the text was made or how many tries it took.
    """
    known = gather(extended, config)
    tokens = extract_tokens(text, config.word_numbers)
    by_class = {c.value: 0 for c in TokenClass}
    derived_values_used: list[str] = []
    failures: list[dict] = []
    anomalies: list[dict] = []
    verified = 0

    for token, start, _ in tokens:
        token_class = classify_token(token, text[:start])
        by_class[token_class.value] += 1
        entry = {"token": token, "class": token_class.value, "position": start}
        reason, instance, near = check_token(token, token_class, known, config)

        if reason and token_class == TokenClass.UNVERIFIABLE and not config.reject_unclassifiable:
            anomalies.append({**entry, "reason": f"{reason}; let through, reject_unclassifiable is off"})
            verified += 1
            continue
        if reason:
            log.warning("unverified %s token %r at %d: %s", token_class.value, token, start, reason)
            failures.append({**entry, "reason": reason})
            continue

        verified += 1
        if instance and instance not in derived_values_used:
            derived_values_used.append(instance)
        if near and config.record_tolerance_anomalies:
            anomalies.append({**entry, **near})

    return VerificationResult(
        method="template_slot_fill",
        passed=not failures and verified == len(tokens),
        tokens_emitted=len(tokens),
        tokens_verified=verified,
        by_class=by_class,
        derived_values_used=derived_values_used,
        regeneration_attempts=0,
        failures=failures,
        anomalies=anomalies,
    )
