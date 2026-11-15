"""Report section per enabled pack.

Each clause with its mapped findings and evidence, clauses not assessed,
the unmapped list, and the non-certification statement word for word.

A finding maps to a clause when its feature is one the clause lists in
maps_to_features, and to no other. Every finding counts the same whatever
its status. A clause no finding maps to reads not assessed, never passing.
A finding that maps to no clause in any enabled pack is unmapped, and is
never moved under a clause that sounds like it.

Evidence is read from the finding itself: for each name in a clause's
evidence_required, the finding's field of that name. A text value that
starts with the recorded path prefix is an image path, anything else is a
reading, and a field that is absent or null is not recorded.

Built once per run and passed to both the report and the manifest, so the
two hold the same mapping.
"""

from __future__ import annotations

from dataclasses import asdict, fields, is_dataclass
from datetime import datetime
from pathlib import Path

from agents.slots import number_words
from common.paths import PATH_PREFIX, ResolvedImage, resolve_evidence
from common.schema import Finding, Record
from standards.packs import Pack

#: Printed in every standards section, the same words every run.
STATEMENT = (
    "This report presents inspection evidence mapped to the referenced clauses. It does not "
    "certify compliance with any standard, and no statement in it should be read as a "
    "compliance determination."
)

#: A clause's status. There is no third value.
PRESENTED = "evidence presented"
NOT_ASSESSED = "not assessed"

#: What an evidence value is.
IMAGE = "image"
READING = "reading"

FINDING_FIELDS = {f.name for f in fields(Finding)}


def plain(value):
    """A finding value as JSON can hold it: a time as ISO text, a pose as its fields."""
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return asdict(value)
    return value


def evidence(finding: Finding, required: tuple[str, ...], path_prefix: str = PATH_PREFIX) -> list[dict]:
    """The finding's value for each evidence name the clause requires, in the clause's order.

    kind is image for a recorded path, reading for any other value, and None
    when the finding has no such field or it is null.
    """
    result = []
    for name in required:
        value = getattr(finding, name) if name in FINDING_FIELDS else None
        if value is None or value == "":
            kind = None
        elif isinstance(value, str) and value.startswith(path_prefix):
            kind = IMAGE
        else:
            kind = READING
        result.append({"field": name, "kind": kind, "value": plain(value)})
    return result


def unmapped_statement(feature: str | None, count: int, checkpoints: list[str]) -> str:
    """One finding of type X at a is not mapped to a clause in the enabled standards packs.

    One checkpoint is named. More are counted, so the sentence stays short;
    the entry's checkpoints list names them.
    """
    subject = f"{number_words(count).capitalize()} finding{'' if count == 1 else 's'}"
    kind = f"of type {feature}" if feature else "with no recorded type"
    if len(checkpoints) == 1:
        where = f" at {checkpoints[0]}"
    elif checkpoints:
        where = f" at {number_words(len(checkpoints))} checkpoints"
    else:
        where = ""
    verb = "is" if count == 1 else "are"
    return f"{subject} {kind}{where} {verb} not mapped to a clause in the enabled standards packs."


def map_findings(record: Record, packs: list[Pack], path_prefix: str = PATH_PREFIX) -> dict:
    """The standards mapping for one run, in both directions.

    packs      each enabled pack, each clause with its status and its findings
    findings   each finding with the clauses it maps to, empty when unmapped
    unmapped   one entry per feature that maps to no clause, with its sentence

    Findings are taken in record order, and a finding id listed twice counts
    once. Returns None when no pack is enabled: the report then has no
    standards section.
    """
    if not packs:
        return None

    seen, findings = set(), []
    for finding in record.findings:
        if finding.finding_id not in seen:
            seen.add(finding.finding_id)
            findings.append(finding)

    clauses_of = {f.finding_id: [] for f in findings}
    pack_entries = []
    for pack in packs:
        clause_entries = []
        for clause in pack.clauses:
            mapped = [f for f in findings if f.feature in clause.maps_to_features]
            for f in mapped:
                clauses_of[f.finding_id].append({"pack_id": pack.pack_id, "clause": clause.clause})
            clause_entries.append({
                "clause": clause.clause,
                "summary": clause.summary,
                "evidence_required": list(clause.evidence_required),
                "status": PRESENTED if mapped else NOT_ASSESSED,
                "findings": [
                    {"finding_id": f.finding_id, "evidence": evidence(f, clause.evidence_required, path_prefix)}
                    for f in mapped
                ],
            })
        pack_entries.append({
            "pack_id": pack.pack_id,
            "pack_version": pack.pack_version,
            "title": pack.title,
            "clauses": clause_entries,
        })

    # Unmapped findings, grouped by feature in the order first found, with
    # their checkpoints in record order.
    groups: dict[str | None, list[Finding]] = {}
    for f in findings:
        if not clauses_of[f.finding_id]:
            groups.setdefault(f.feature, []).append(f)
    unmapped = []
    for feature, group in groups.items():
        checkpoints = list(dict.fromkeys(f.checkpoint_id for f in group if f.checkpoint_id))
        unmapped.append({
            "feature": feature,
            "finding_ids": [f.finding_id for f in group],
            "checkpoints": checkpoints,
            "statement": unmapped_statement(feature, len(group), checkpoints),
        })

    return {
        "statement": STATEMENT,
        "packs": pack_entries,
        "findings": [{"finding_id": f.finding_id, "clauses": clauses_of[f.finding_id]} for f in findings],
        "unmapped": unmapped,
    }


def evidence_images(standards: dict | None, evidence_root: Path) -> dict[str, ResolvedImage]:
    """Each image path the mapping holds, resolved the way checkpoint images are."""
    images = {}
    for pack in (standards or {}).get("packs", []):
        for clause in pack["clauses"]:
            for finding in clause["findings"]:
                for item in finding["evidence"]:
                    if item["kind"] == IMAGE and item["value"] not in images:
                        images[item["value"]] = resolve_evidence(item["value"], evidence_root)
    return images
