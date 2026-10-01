"""Slot filling (section 7).

Replaces each {{ slot_id }} in a template with a value from the extended
record, found by dotted path and formatted by code. Records where each value
landed, so citations point at exact spans.
"""

from __future__ import annotations

import re
from dataclasses import fields, is_dataclass
from datetime import datetime

from agents.schema import (
    DerivationConfig, ExtendedRecord, FilledSlot, FilledTemplate, Template,
)
from agents.utils import format_timestamp, format_value

PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")

# One step of a path: a name, or an index in brackets. records[0].checkpoints[2]
STEP = re.compile(r"([^.\[\]]+)|\[(\d+)\]")

# The start of a path into one derived output: derived.values.threshold_compare[0]
DERIVED_OUTPUT = re.compile(r"derived\.values\.\w+(?:\[\d+\])?")

# A slot path ending in KEY prints the last key of the path, not the value
# there: derived.values.threshold_compare#key -> threshold_compare. For a name
# that is a key in the extended record rather than a value in it, like a
# derivation instance or D7's only_in_a. Its citation is the path without it.
KEY = "#key"

# A slot path ending in WORDS prints a whole number as words (7 -> seven);
# CAP_WORDS capitalises it for the start of a sentence (Seven). The verifier
# still reads the word as a count. Its citation is the path without it.
WORDS = "#words"
CAP_WORDS = "#Words"
# A slot path ending in ORDINAL prints a whole number as an ordinal word
# (2 -> second). The verifier reads it as an ordinal.
ORDINAL = "#ordinal"
ORDINALS = ("first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth "
            "thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth").split()
ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
        "fourteen fifteen sixteen seventeen eighteen nineteen").split()
TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()


class MissingSlotError(ValueError):
    """A slot whose source path resolves to nothing (section 7.1)."""


def resolve(path: str, extended: ExtendedRecord) -> tuple[object, object, object]:
    """Follow a dotted path from the extended record.

    e.g. derived.values.group_max[2].groups.checkpoint_1.max, or
    records[0].checkpoints[2].sensor.environment.temperature_c.
    Returns (value, the container it was in, its key there), or Nones if any
    step is missing.
    """
    obj, parent, key = extended, None, None
    for name, index in STEP.findall(path):
        parent = obj
        if index:
            key = int(index)
            obj = obj[key] if isinstance(obj, (list, tuple)) and key < len(obj) else None
        elif isinstance(obj, dict):
            key = name
            # A group of values with no zone is keyed None, written "None" in a path.
            obj = obj.get(name) if name != "None" or name in obj else obj.get(None)
        elif is_dataclass(obj) and name in {f.name for f in fields(obj)}:
            key = name
            obj = getattr(obj, name)
        else:
            obj = None
        if obj is None:
            return None, None, None
    return obj, parent, key


def measured_field(path: str, extended: ExtendedRecord) -> str | None:
    """The field_path of the derived output a path points into, or None.

    e.g. environment.temperature_c for
    derived.values.threshold_compare[0].checkpoints.checkpoint_1.value.
    """
    match = DERIVED_OUTPUT.match(path)
    if not match:
        return None
    output, _, _ = resolve(match.group(0), extended)
    return output.get("field_path") if isinstance(output, dict) else None


def format_slot(
    value: object, parent: object, key: object, path: str, config: DerivationConfig, field: str | None = None,
) -> str:
    """The text a value is printed as. All formatting is here or in utils (section 7.3).

    A derived number uses its *_formatted sibling. Record floats and timestamps
    use the same formatters the derivations do. field is the derived output's
    field_path (see measured_field), for a number that has neither.
    """
    if isinstance(parent, dict) and isinstance(key, str) and f"{key}_formatted" in parent:
        return parent[f"{key}_formatted"]
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        raise ValueError(f"'{path}' is true/false; a template states that in words, not as a slot")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # D1's value and threshold have no *_formatted sibling and no decimals
        # entry of their own: they are readings of the output's field_path. A
        # number with neither (D3's proportion) was rounded to significant
        # figures by utils.round_value, and prints the same way.
        name = path.rsplit(".", 1)[-1]
        if name in config.decimals:
            return format_value(value, path, config)
        if field is not None:
            return format_value(value, field, config)
        return f"{value:.{config.significant_figures}g}"
    if isinstance(value, datetime):
        return format_timestamp(value, config)
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return ", ".join(value)
    raise ValueError(f"'{path}' holds a {type(value).__name__}, which has no printed form")


def number_words(n: int) -> str:
    """0-99 as words (7 -> seven, 21 -> twenty-one); anything else stays digits."""
    if 0 <= n < 20:
        return ONES[n]
    if 20 <= n < 100:
        tens, ones = divmod(n, 10)
        return TENS[tens - 2] + (f"-{ONES[ones]}" if ones else "")
    return str(n)


def ordinal_words(n: int) -> str:
    """1-20 as ordinal words (2 -> second); anything else as 21st, 22nd, ..."""
    if 1 <= n <= len(ORDINALS):
        return ORDINALS[n - 1]
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def fill_slots(template: Template, extended: ExtendedRecord, config: DerivationConfig) -> FilledTemplate:
    """Fill every slot in the template, left to right, recording each span.

    A slot with no path, or a path that finds nothing (including an empty
    string or list), raises MissingSlotError. Nothing is filled with a blank,
    a zero or a placeholder.
    """
    parts: list[str] = []
    filled: list[FilledSlot] = []
    length = 0
    pos = 0

    for match in PLACEHOLDER.finditer(template.text):
        slot_id = match.group(1)
        if slot_id not in template.slots:
            raise MissingSlotError(f"slot '{slot_id}' in template '{template.name}' has no source path")
        path = template.slots[slot_id]
        key_only = path.endswith(KEY)
        words = path.endswith((WORDS, CAP_WORDS))
        capital = path.endswith(CAP_WORDS)
        ordinal = path.endswith(ORDINAL)
        path = path.removesuffix(KEY).removesuffix(WORDS).removesuffix(CAP_WORDS).removesuffix(ORDINAL)
        value, parent, key = resolve(path, extended)
        if value is None or value == "" or value == [] or value == ():
            raise MissingSlotError(f"slot '{slot_id}' in template '{template.name}': '{path}' resolves to nothing")
        # A path ending at a group prints the group's name, which is its key:
        # ...groups.home_docking_station -> home_docking_station. So does a
        # path marked KEY, whatever is there.
        if (key_only or isinstance(value, dict)) and isinstance(key, str):
            value = key
            parent = None   # a key has no *_formatted sibling
        formatted = format_slot(value, parent, key, path, config, measured_field(path, extended))
        if words and isinstance(value, int) and not isinstance(value, bool):
            formatted = number_words(value)
            formatted = formatted[0].upper() + formatted[1:] if capital else formatted
        if ordinal and isinstance(value, int) and not isinstance(value, bool):
            formatted = ordinal_words(value)

        literal = template.text[pos:match.start()]
        parts.append(literal)
        length += len(literal)
        parts.append(formatted)
        filled.append(FilledSlot(slot_id, path, value, formatted, (length, length + len(formatted))))
        length += len(formatted)
        pos = match.end()

    parts.append(template.text[pos:])
    return FilledTemplate(template.name, "".join(parts), tuple(filled))
