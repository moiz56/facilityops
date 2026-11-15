"""Standards: TS-12 and TS-13, and the pack loader.

The pack is tests/standards/packs/nfpa_70b_2023.yaml, the brief's example
word for word: clause 11.3.2 maps thermal_hotspot and blocked_airflow_path.

Runs are real runs in data_tenant. No real run has an airflow_obstruction
finding, so TS-13's brief case is a copy of a real run, made in the test's
own temporary folder, with its one finding's feature changed to
airflow_obstruction. Nothing in data_tenant is changed.
"""

import json
import re
from pathlib import Path

import pytest

from common.loader import load_record
from common.paths import CONFIG_PATH, ConfigError, load_config
from standards.packs import load_packs
from standards.render import NOT_ASSESSED, PRESENTED, STATEMENT, map_findings
from tests.helpers import DATA, section_html

PACKS = Path(__file__).resolve().parent / "packs"

#: The SIS facility run whose one finding is general_condition at home_docking_station.
#: No finding of it maps to clause 11.3.2.
FACILITY = DATA / "sis_facility_checkpoint_route"
FACILITY_RUN = FACILITY / "20260729_082245-sis_facility_checkpoint_route" / "run.json"

#: A SIS racks run with blocked_airflow_path findings, which clause 11.3.2 maps.
RACKS = DATA / "sis_racks_checkpoint_route"
RACKS_RUN = RACKS / "20260821_191548-sis_racks_checkpoint_route" / "run.json"

#: The statement, as the brief prints it.
BRIEF_STATEMENT = (
    "This report presents inspection evidence mapped to the referenced clauses. It does not "
    "certify compliance with any standard, and no statement in it should be read as a "
    "compliance determination."
)


@pytest.fixture(scope="module")
def config():
    return load_config(CONFIG_PATH)


@pytest.fixture(scope="module")
def packs():
    return load_packs(["nfpa_70b_2023"], PACKS)


@pytest.fixture
def airflow_obstruction(tmp_path):
    """The facility run with its one finding's feature changed to airflow_obstruction."""
    record = json.loads(FACILITY_RUN.read_text(encoding="utf-8"))
    (finding,) = record["findings"]
    assert finding["checkpoint_id"] == "home_docking_station"
    finding["feature"] = "airflow_obstruction"
    path = tmp_path / "run.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return load_record(path)


def standards_html(record, evidence_root, config, standards) -> str:
    return section_html(record, evidence_root, config, "standards", standards=standards)


def clause(standards: dict, ref: str) -> dict:
    (pack,) = standards["packs"]
    return next(c for c in pack["clauses"] if c["clause"] == ref)


def collapse(html: str) -> str:
    """The text of the HTML on one line, so a sentence split across lines still matches."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# TS-12

def test_ts12_statement_is_the_brief_text():
    assert STATEMENT == BRIEF_STATEMENT


@pytest.mark.parametrize("run, root", [(FACILITY_RUN, FACILITY), (RACKS_RUN, RACKS)])
def test_ts12_statement_in_every_standards_section(run, root, packs, config):
    record = load_record(run)
    standards = map_findings(record, packs)
    assert standards["statement"] == BRIEF_STATEMENT
    assert BRIEF_STATEMENT in collapse(standards_html(record, root, config, standards))


def test_ts12_statement_identical_across_runs(packs):
    statements = {map_findings(load_record(run), packs)["statement"] for run in (FACILITY_RUN, RACKS_RUN)}
    assert statements == {BRIEF_STATEMENT}


# TS-13

def test_ts13_unmapped_feature_is_reported_unmapped(airflow_obstruction, packs, config):
    standards = map_findings(airflow_obstruction, packs)
    (finding,) = airflow_obstruction.findings

    # Not under the clause that maps the similar-sounding blocked_airflow_path.
    assert clause(standards, "11.3.2")["findings"] == []
    assert standards["findings"] == [{"finding_id": finding.finding_id, "clauses": []}]

    (unmapped,) = standards["unmapped"]
    assert unmapped["feature"] == "airflow_obstruction"
    assert unmapped["finding_ids"] == [finding.finding_id]
    assert unmapped["statement"] == (
        "One finding of type airflow_obstruction at home_docking_station is not mapped to a "
        "clause in the enabled standards packs."
    )
    assert unmapped["statement"] in collapse(standards_html(airflow_obstruction, FACILITY, config, standards))


def test_ts13_mapped_feature_is_under_its_clause(packs):
    """The control: blocked_airflow_path does map to 11.3.2, so the case above is the feature, not the pack."""
    record = load_record(RACKS_RUN)
    standards = map_findings(record, packs)
    mapped = clause(standards, "11.3.2")
    expected = list(dict.fromkeys(f.finding_id for f in record.findings if f.feature == "blocked_airflow_path"))
    assert expected, "the run has no blocked_airflow_path finding"
    assert [f["finding_id"] for f in mapped["findings"]] == expected
    assert mapped["status"] == PRESENTED
    assert "blocked_airflow_path" not in {u["feature"] for u in standards["unmapped"]}


def test_ts13_clause_with_no_evidence_is_not_assessed(airflow_obstruction, packs, config):
    standards = map_findings(airflow_obstruction, packs)
    assert clause(standards, "11.3.2")["status"] == NOT_ASSESSED

    text = collapse(standards_html(airflow_obstruction, FACILITY, config, standards))
    assert "Not assessed" in text
    # Outside the statement, which itself says it is no compliance determination,
    # nothing reads as passing or compliant.
    rest = text.replace(BRIEF_STATEMENT, "")
    assert not re.search(r"\b(complian\w*|complies|compliant|pass\w*)\b", rest, re.I)


@pytest.mark.parametrize("run", [FACILITY_RUN, RACKS_RUN])
def test_ts13_clause_status_is_only_presented_or_not_assessed(run, packs):
    standards = map_findings(load_record(run), packs)
    for pack in standards["packs"]:
        for entry in pack["clauses"]:
            assert entry["status"] == (PRESENTED if entry["findings"] else NOT_ASSESSED)


# The loader. A pack with a clause shape it has not seen still loads; a pack
# it cannot read without guessing is refused.

def write_pack(folder: Path, text: str) -> None:
    (folder / "nfpa_70b_2023.yaml").write_text(text, encoding="utf-8")


HEAD = 'pack_id: nfpa_70b_2023\npack_version: "1.0"\ntitle: "NFPA 70B"\n'


def test_clause_with_unknown_keys_and_no_mapping_loads(tmp_path):
    write_pack(tmp_path, HEAD + (
        "clauses:\n"
        '  - clause: "11.3.2"\n'
        "    notes: kept by the client, not read here\n"
        "    sub_clauses: [a, b]\n"
    ))
    (pack,) = load_packs(["nfpa_70b_2023"], tmp_path)
    (only,) = pack.clauses
    assert only.maps_to_features == () and only.evidence_required == () and only.summary is None

    standards = map_findings(load_record(RACKS_RUN), [pack])
    assert clause(standards, "11.3.2")["status"] == NOT_ASSESSED


@pytest.mark.parametrize("clauses, problem", [
    ('clauses: []\n', "'clauses' must be a non-empty list"),
    ('clauses:\n  - summary: "no reference"\n', "clauses[0].clause"),
    ('clauses:\n  - clause: 11.10\n', "must be text in quotes"),
    ('clauses:\n  - clause: "1"\n  - clause: "1"\n', "listed twice"),
    ('clauses:\n  - clause: "1"\n    maps_to_features: thermal_hotspot\n', "must be a list of names"),
])
def test_pack_that_needs_a_guess_is_refused(tmp_path, clauses, problem):
    write_pack(tmp_path, HEAD + clauses)
    with pytest.raises(ConfigError, match=re.escape(problem)):
        load_packs(["nfpa_70b_2023"], tmp_path)


def test_pack_id_must_match_its_file(tmp_path):
    write_pack(tmp_path, HEAD.replace("nfpa_70b_2023", "other_pack") + 'clauses:\n  - clause: "1"\n')
    with pytest.raises(ConfigError, match="file is named"):
        load_packs(["nfpa_70b_2023"], tmp_path)


def test_enabled_pack_with_no_file_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="does not exist"):
        load_packs(["nfpa_70b_2023"], tmp_path)


def test_no_pack_enabled_is_no_section(packs):
    assert load_packs([], PACKS) == []
    assert map_findings(load_record(RACKS_RUN), []) is None
