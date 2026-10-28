"""B-1's answer to "what can I ask": the intent catalogue, written by code.

config/intent_catalogue.yaml lists what a customer can ask, by category, with
example questions. The retrieval router sends "what can I ask", "help" and the
like here, and no further model call is made. Code writes the answer from the
catalogue as written, so it only ever offers what the catalogue lists.

<checkpoint> and <zone> in an example are filled with a checkpoint of the
latest run and its zone. Those names are the answer's only claims about the
records: they are cited and verified against the fields they came from. The
rest is text code wrote that no field holds, so its tokens ("PM2.5", "five")
are exempt from verification. An example whose name cannot be filled is left
out.
"""

from __future__ import annotations

import re

from agents.schema import ExtendedRecord, FilledSlot, VerificationConfig, VerificationResult
from agents.verification import verify_numeric
from common.paths import ConfigError

PLACEHOLDERS = re.compile(r"<(checkpoint|zone)>")


def check_catalogue(catalogue: dict) -> dict:
    """The parsed intent_catalogue.yaml, checked, or ConfigError naming what is wrong."""
    for key in ("intro", "not_answerable"):
        if not isinstance(catalogue.get(key), str) or not catalogue[key].strip():
            raise ConfigError(f"intent_catalogue.yaml: '{key}' must be text")
    categories = catalogue.get("categories")
    if not isinstance(categories, list) or not categories:
        raise ConfigError("intent_catalogue.yaml: 'categories' must be a non-empty list")
    for k, category in enumerate(categories):
        if not isinstance(category, dict):
            raise ConfigError(f"intent_catalogue.yaml: categories[{k}] must be a mapping")
        for key in ("name", "description"):
            if not isinstance(category.get(key), str) or not category[key].strip():
                raise ConfigError(f"intent_catalogue.yaml: categories[{k}].{key} must be text")
        examples = category.get("examples")
        if not isinstance(examples, list) or not examples or not all(isinstance(e, str) for e in examples):
            raise ConfigError(f"intent_catalogue.yaml: categories[{k}].examples must be a non-empty list of text")
    return catalogue


def place_names(extended: ExtendedRecord) -> dict[str, tuple[str, str]]:
    """placeholder -> (the name, its path in the records), from the latest run:
    its first checkpoint with a zone, or its first checkpoint when none has
    one. A placeholder with nothing to fill it is left out."""
    i = len(extended.records) - 1
    checkpoints = extended.records[i].checkpoints if extended.records else ()
    if not checkpoints:
        return {}
    j = next((j for j, cp in enumerate(checkpoints) if cp.zone), 0)
    names = {"checkpoint": (checkpoints[j].checkpoint_id, f"records[{i}].checkpoints[{j}].checkpoint_id")}
    if checkpoints[j].zone:
        names["zone"] = (checkpoints[j].zone, f"records[{i}].checkpoints[{j}].zone")
    return names


def answer_catalogue(
    catalogue: dict, extended: ExtendedRecord, verification: VerificationConfig,
) -> tuple[dict, VerificationResult]:
    """The catalogue as the answer, and its verification.

    Returns (output, verification): output is {answer, citations,
    records_consulted}, with a citation for each name filled in.
    """
    names = place_names(extended)
    # The answer in pieces: (text, its path in the records when it is a filled-in name, else None).
    pieces: list[tuple[str, str | None]] = [(catalogue["intro"].strip(), None)]
    for category in catalogue["categories"]:
        pieces.append((f"\n\n{category['name'].strip()}: {category['description'].strip()}", None))
        for example in category["examples"]:
            parts = PLACEHOLDERS.split(example)   # text, name, text, name, ..., text
            if any(name not in names for name in parts[1::2]):
                continue
            pieces.append(('\n- "', None))
            for k, part in enumerate(parts):
                pieces.append(names[part] if k % 2 else (part, None))
            pieces.append(('"', None))
    pieces.append((f"\n\n{catalogue['not_answerable'].strip()}", None))

    text, slots, exempt = "", [], []
    for piece, source in pieces:
        span = (len(text), len(text) + len(piece))
        text += piece
        if source is None:
            exempt.append(span)
        else:
            slots.append(FilledSlot(f"name_{len(slots)}", source, piece, piece, span))

    result = verify_numeric(text, extended, verification, slots, exempt)
    output = {
        "answer": text,
        "citations": [{"claim_span": list(slot.span), "source_field": slot.source_field} for slot in slots],
        "records_consulted": [extended.records[-1].run_id] if slots else [],
    }
    return output, result
