"""Tests for the report engine. Run with `pytest` from the project root."""

from __future__ import annotations

import collections
import json
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from common.loader import load_record
from common.paths import (
    DIRECTIONS, PATH_PREFIX, THERMAL_SUFFIX, ConfigError, ResolvedImage, load_config, setting,
    parse_filename, resolve_checkpoint_images, resolve_evidence, resolve_record_images,
)
from report.cli import dump_artifacts, run_pipeline, write_json
from common.schema import (
    Accelerometer, Checkpoint, Environment, Event, Finding, Particulate, RawSensor,
    Record, SensorBlock, SensorWarning,
)
from report.derive import computed_counts, declared_counts
from report.manifest import (
    PLACEHOLDER_FIELDS, build_manifest, config_hash, write_manifest,
)
from report.gaps import (
    RUN_LEVEL, Gap, GapType, checkpoint_gaps, missed_checkpoint_gaps,
    missing_image_gaps, missing_thermal_gaps, no_evidence_gaps,
    count_mismatch_gaps, detect_gaps, empty_record_gaps, no_findings_gaps,
    sensor_unavailable_gaps, stale_reading_gaps, subsystem_offline_gaps,
    unavailable_reason,
)


def sensor_gaps_for(point: Checkpoint) -> list[Gap]:
    """The three sensor rules together, for the tests that cover them."""
    return [
        *sensor_unavailable_gaps(point),
        *subsystem_offline_gaps(point),
        *stale_reading_gaps(point),
    ]
from report.images import build_direction_grid

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_RUN = (
    PROJECT_ROOT / "data" / "20260728_144120-sis_facility_checkpoint_route"
    / "records" / "run.json"
)

# A directory name with spaces and mixed case, as the real evidence tree has.
EVIDENCE_DIR = "SIS Facility Checkpoint Route"


@pytest.fixture
def evidence_root(tmp_path: Path) -> Path:
    """An evidence tree with one usable image and two unusable ones."""
    checkpoint = tmp_path / EVIDENCE_DIR / "checkpoint_1"
    checkpoint.mkdir(parents=True)
    Image.new("RGB", (16, 16), "red").save(checkpoint / "checkpoint_1_N.jpg")
    (checkpoint / "checkpoint_1_NE.jpg").write_bytes(b"")
    (checkpoint / "checkpoint_1_E.jpg").write_text("not a jpeg\n", encoding="utf-8")
    return tmp_path


def recorded(filename: str) -> str:
    """A path as the robot would have recorded it."""
    return f"{PATH_PREFIX}/{EVIDENCE_DIR}/checkpoint_1/{filename}"


def testsettings_come_from_report_yaml() -> None:
    assert DIRECTIONS == ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    assert THERMAL_SUFFIX == "_thermal"
    assert PATH_PREFIX == "/run-files"


@pytest.mark.parametrize(
    ("filename", "direction", "is_thermal"),
    [
        ("checkpoint_1_N.jpg", "N", False),
        ("checkpoint_1_NE.jpg", "NE", False),
        ("checkpoint_1_NW_thermal.jpg", "NW", True),
        ("home_docking_station_SE.jpg", "SE", False),
        ("checkpoint_1_n.jpg", "N", False),
        ("checkpoint_1_thermal.jpg", None, True),
        ("checkpoint_1.jpg", None, False),
    ],
)
def test_parse_filename(filename: str, direction: str | None, is_thermal: bool) -> None:
    assert parse_filename(filename) == (direction, is_thermal)


def test_resolves_path_with_spaces_and_mixed_case(evidence_root: Path) -> None:
    image = resolve_evidence(recorded("checkpoint_1_N.jpg"), evidence_root)
    assert (image.exists, image.readable, image.reason) == (True, True, None)
    assert image.direction == "N"
    assert image.local_path is not None and image.local_path.is_file()


def test_missing_file(evidence_root: Path) -> None:
    image = resolve_evidence(recorded("checkpoint_1_W.jpg"), evidence_root)
    assert (image.exists, image.readable) == (False, False)
    assert "no file at" in image.reason
    # The direction survives, so the grid can place the gap in the right cell.
    assert image.direction == "W"


def test_empty_file(evidence_root: Path) -> None:
    image = resolve_evidence(recorded("checkpoint_1_NE.jpg"), evidence_root)
    assert (image.exists, image.readable) == (True, False)
    assert image.reason == "file is empty"


def test_undecodable_file(evidence_root: Path) -> None:
    image = resolve_evidence(recorded("checkpoint_1_E.jpg"), evidence_root)
    assert (image.exists, image.readable) == (True, False)
    assert image.reason == "file is not a readable image"


def test_empty_and_undecodable_give_different_reasons(evidence_root: Path) -> None:
    empty = resolve_evidence(recorded("checkpoint_1_NE.jpg"), evidence_root)
    undecodable = resolve_evidence(recorded("checkpoint_1_E.jpg"), evidence_root)
    assert empty.reason != undecodable.reason


def test_thermal_is_read_from_the_filename(evidence_root: Path) -> None:
    checkpoint = evidence_root / EVIDENCE_DIR / "checkpoint_1"
    Image.new("RGB", (16, 16), "grey").save(checkpoint / "checkpoint_1_N_thermal.jpg")
    image = resolve_evidence(recorded("checkpoint_1_N_thermal.jpg"), evidence_root)
    assert (image.direction, image.is_thermal, image.readable) == ("N", True, True)


def test_resolved_image_is_frozen(evidence_root: Path) -> None:
    image = resolve_evidence(recorded("checkpoint_1_N.jpg"), evidence_root)
    with pytest.raises(Exception):
        image.direction = "S"  # type: ignore[misc]


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_every_reference_evidence_path_resolves() -> None:
    """All 87 recorded paths in the reference run resolve under data/."""
    record = json.loads(REFERENCE_RUN.read_text(encoding="utf-8"))
    images = [
        resolve_evidence(uri, PROJECT_ROOT / "data")
        for checkpoint in record["checkpoints"]
        for uri in (checkpoint.get("evidence_images") or [])
    ]
    assert len(images) == 87
    assert [i.reason for i in images if not i.readable] == []
    assert [i.original_uri for i in images if i.direction is None] == []


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_reference_thermal_counts() -> None:
    """Checkpoint 2 has eight thermal pairs, checkpoint 1 four, 6 and 7 none."""
    record = json.loads(REFERENCE_RUN.read_text(encoding="utf-8"))
    thermal = {
        checkpoint["checkpoint_id"]: sum(
            resolve_evidence(uri, PROJECT_ROOT / "data").is_thermal
            for uri in (checkpoint.get("evidence_images") or [])
        )
        for checkpoint in record["checkpoints"]
    }
    assert thermal["checkpoint_1"] == 4
    assert thermal["checkpoint_2"] == 8
    assert thermal["checkpoint_6"] == 0
    assert thermal["checkpoint_7"] == 0


# --- config errors ---------------------------------------------------------


MINIMAL_CONFIG = """
paths:
  path_prefix: /run-files
evidence:
  directions: [N, NE, E, SE, S, SW, W, NW]
  grid_columns: 4
  thermal_suffix: _thermal
"""

#: The same settings, written in a different order.
REORDERED_CONFIG = """
evidence:
  thermal_suffix: _thermal
  grid_columns: 4
  directions: [N, NE, E, SE, S, SW, W, NW]
paths:
  path_prefix: /run-files
"""


def write_config(tmp_path: Path, body: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "report.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_missing_config_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as error:
        load_config(tmp_path / "absent.yaml")
    assert "absent.yaml" in str(error.value)
    assert "cannot be read" in str(error.value)


def test_invalid_yaml_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as error:
        load_config(write_config(tmp_path, "evidence:\n  directions: [unclosed\n"))
    assert "not valid YAML" in str(error.value)


@pytest.mark.parametrize("body", ["", "- a list\n", "a string\n"])
def test_config_that_is_not_a_mapping_is_reported(tmp_path: Path, body: str) -> None:
    with pytest.raises(ConfigError) as error:
        load_config(write_config(tmp_path, body))
    assert "expected a mapping of settings" in str(error.value)


@pytest.mark.parametrize(
    ("config", "named"),
    [
        ({}, "evidence.directions"),
        ({"evidence": {}}, "evidence.directions"),
        ({"evidence": {"directions": ["N"]}}, "evidence.thermal_suffix"),
        ({"evidence": {"thermal_suffix": "_thermal"}}, "evidence.directions"),
        ({"paths": {}}, "paths.path_prefix"),
    ],
)
def test_missingsetting_names_thesetting(config: dict, named: str) -> None:
    section, key = named.split(".")
    with pytest.raises(ConfigError) as error:
        setting(config, section, key)
    assert named in str(error.value)
    assert "missing required setting" in str(error.value)


def test_presentsetting_is_returned() -> None:
    assert setting({"evidence": {"thermal_suffix": "_ir"}}, "evidence", "thermal_suffix") == "_ir"


# --- resolving from the loader's output ------------------------------------


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_resolve_record_images_covers_every_checkpoint() -> None:
    record = load_record(REFERENCE_RUN)
    images = resolve_record_images(record, PROJECT_ROOT / "data")

    assert set(images) == {c.checkpoint_id for c in record.checkpoints}
    assert sum(len(v) for v in images.values()) == 87
    assert all(i.readable for found in images.values() for i in found)


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_checkpoint_with_no_evidence_resolves_to_an_empty_list() -> None:
    record = load_record(REFERENCE_RUN)
    images = resolve_record_images(record, PROJECT_ROOT / "data")
    assert images["home_docking_station"] == []


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_resolve_checkpoint_images_matches_the_recorded_order() -> None:
    record = load_record(REFERENCE_RUN)
    checkpoint = record.checkpoints[1]
    found = resolve_checkpoint_images(checkpoint, PROJECT_ROOT / "data")
    assert [i.original_uri for i in found] == list(checkpoint.evidence_images)


# --- the direction grid ----------------------------------------------------


def cell(direction: str, is_thermal: bool = False, readable: bool = True) -> ResolvedImage:
    """A ResolvedImage for grid tests, without touching the filesystem."""
    return ResolvedImage(
        f"checkpoint_1_{direction}.jpg", Path("x.jpg"), direction, is_thermal,
        True, readable, None if readable else "file is empty",
    )


def test_grid_is_always_eight_cells_in_compass_order() -> None:
    grid = build_direction_grid([])
    assert [c.direction for c in grid] == list(DIRECTIONS)
    assert all(c.rgb is None and c.thermal is None for c in grid)


def test_grid_pairs_thermal_with_its_rgb() -> None:
    images = [cell(d) for d in DIRECTIONS] + [cell(d, is_thermal=True) for d in DIRECTIONS]
    grid = build_direction_grid(images)
    assert len(grid) == 8
    assert all(c.rgb is not None and c.thermal is not None for c in grid)


def test_grid_with_rgb_but_no_thermal() -> None:
    grid = build_direction_grid([cell(d) for d in DIRECTIONS])
    assert all(c.rgb is not None for c in grid)
    assert all(c.thermal is None for c in grid)


def test_grid_keeps_empty_cells_for_missing_directions() -> None:
    grid = build_direction_grid([cell(d) for d in ("N", "NE", "E", "SE", "S")])
    filled = [c.direction for c in grid if c.rgb is not None]
    empty = [c.direction for c in grid if c.rgb is None]
    assert filled == ["N", "NE", "E", "SE", "S"]
    assert empty == ["SW", "W", "NW"]


def test_grid_keeps_an_unusable_image_in_its_cell() -> None:
    """A broken image must still occupy its cell, so the reason can be shown."""
    grid = build_direction_grid([cell("N", readable=False)])
    north = grid[0]
    assert north.rgb is not None
    assert north.rgb.readable is False
    assert north.rgb.reason == "file is empty"


def test_grid_ignores_images_with_no_direction() -> None:
    stray = ResolvedImage("photo.jpg", Path("photo.jpg"), None, False, True, True, None)
    grid = build_direction_grid([cell("N"), stray])
    assert grid[0].rgb is not None
    assert sum(1 for c in grid if c.rgb is not None) == 1


def test_grid_keeps_the_first_of_two_images_for_one_direction() -> None:
    first, second = cell("N"), cell("N")
    grid = build_direction_grid([first, second])
    assert grid[0].rgb is first


# --- run_pipeline ----------------------------------------------------------


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_run_pipeline_returns_artifacts() -> None:
    artifacts = run_pipeline(REFERENCE_RUN, PROJECT_ROOT / "data", PROJECT_ROOT / "output")

    assert artifacts.record.run_id.startswith("20260728_144120")
    assert sum(len(f) for f in artifacts.images.values()) == 87
    assert set(artifacts.grids) == set(artifacts.images)
    assert all(len(grid) == 8 for grid in artifacts.grids.values())


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_run_pipeline_thermal_pairing_matches_the_reference_run() -> None:
    grids = run_pipeline(REFERENCE_RUN, PROJECT_ROOT / "data", PROJECT_ROOT / "output").grids
    paired = {cid: sum(1 for c in grid if c.thermal is not None) for cid, grid in grids.items()}
    assert paired["checkpoint_2"] == 8
    assert paired["checkpoint_1"] == 4
    assert paired["checkpoint_6"] == 0
    assert paired["checkpoint_7"] == 0


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_run_pipeline_renders_a_grid_for_a_checkpoint_with_no_evidence() -> None:
    """home_docking_station has no images, and still gets eight empty cells."""
    grids = run_pipeline(REFERENCE_RUN, PROJECT_ROOT / "data", PROJECT_ROOT / "output").grids
    grid = grids["home_docking_station"]
    assert len(grid) == 8
    assert all(c.rgb is None and c.thermal is None for c in grid)


# --- dumping artifacts -----------------------------------------------------


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_dump_artifacts_writes_every_stage(tmp_path: Path) -> None:
    artifacts = run_pipeline(REFERENCE_RUN, PROJECT_ROOT / "data", PROJECT_ROOT / "output")
    destination = tmp_path / "artifacts.json"
    dump_artifacts(artifacts, destination)

    dumped = json.loads(destination.read_text(encoding="utf-8"))
    assert set(dumped) == {"images", "grids"}
    assert sum(len(v) for v in dumped["images"].values()) == 87
    assert all(len(grid) == 8 for grid in dumped["grids"].values())


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_dumped_cells_carry_their_images(tmp_path: Path) -> None:
    artifacts = run_pipeline(REFERENCE_RUN, PROJECT_ROOT / "data", PROJECT_ROOT / "output")
    destination = tmp_path / "artifacts.json"
    dump_artifacts(artifacts, destination)

    grid = json.loads(destination.read_text(encoding="utf-8"))["grids"]["checkpoint_1"]
    assert [c["direction"] for c in grid] == list(DIRECTIONS)
    # checkpoint_1 has RGB everywhere and thermal for four directions only.
    assert all(c["rgb"] is not None for c in grid)
    assert [c["direction"] for c in grid if c["thermal"] is None] == ["N", "NE", "E", "SE"]


def test_write_json_handles_paths_and_datetimes(tmp_path: Path) -> None:
    """Values with no JSON form are written as text rather than raising."""
    destination = tmp_path / "out.json"
    write_json({"path": Path("/a/b.jpg"), "when": datetime(2026, 7, 28, 14, 41)}, destination)
    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "path": "/a/b.jpg", "when": "2026-07-28 14:41:00",
    }


# --- gap types -------------------------------------------------------------


#: The ten gap types, exactly as the manifest spells them.
EXPECTED_GAP_TYPES = [
    "MISSING_IMAGE", "MISSING_THERMAL", "NO_EVIDENCE", "MISSED_CHECKPOINT",
    "SENSOR_UNAVAILABLE", "SUBSYSTEM_OFFLINE", "STALE_READING", "NO_FINDINGS",
    "COUNT_MISMATCH", "EMPTY_RECORD",
]


def test_there_are_exactly_ten_gap_types() -> None:
    """A closed set. A new value here means the manifest no longer matches."""
    assert [g.name for g in GapType] == EXPECTED_GAP_TYPES


def test_each_gap_type_spells_itself() -> None:
    """The name and the value must match, since the value reaches the manifest."""
    assert all(g.value == g.name for g in GapType)


def test_gap_type_serialises_as_its_name() -> None:
    """A StrEnum, so json.dumps writes MISSING_IMAGE, not GapType.MISSING_IMAGE."""
    assert json.dumps({"gap_type": GapType.MISSING_IMAGE}) == '{"gap_type": "MISSING_IMAGE"}'
    assert f"{GapType.NO_EVIDENCE}" == "NO_EVIDENCE"


def test_gap_field_order() -> None:
    """item_id, gap_type, detail - positional construction stays available."""
    gap = Gap("checkpoint_1", GapType.MISSING_THERMAL, "directions N, NE have no thermal")
    assert gap.item_id == "checkpoint_1"
    assert gap.gap_type is GapType.MISSING_THERMAL
    assert gap.detail == "directions N, NE have no thermal"


def test_gap_is_frozen() -> None:
    gap = Gap(RUN_LEVEL, GapType.EMPTY_RECORD, "no checkpoints recorded")
    with pytest.raises(Exception):
        gap.detail = "changed"  # type: ignore[misc]


def test_run_level_item_id() -> None:
    """Run-level gaps are owned by the literal __run__."""
    assert RUN_LEVEL == "__run__"


# --- the sensor trust rule -------------------------------------------------


def sensor_block(**overrides) -> SensorBlock:
    """A healthy sensor reading, with the given fields changed."""
    raw = overrides.pop("raw", {})
    raw_flags = None if raw is None else {
        "adxl345_ok": True, "bme680_ok": True, "sps30_ok": True, **raw,
    }
    defaults = {
        "ok": True, "status": "connected", "sensor_hub_reachable": True, "age_seconds": 0.0,
        "accelerometer": Accelerometer(vibration_rms_g=0.05),
        "environment": Environment(temperature_c=30.8, humidity_pct=25.2),
        "particulate": Particulate(pm1_0=0.0, pm2_5=0.0, pm4_0=0.0, pm10=0.0),
        "raw": None if raw_flags is None else RawSensor(**raw_flags),
    }
    return SensorBlock(**{**defaults, **overrides})


def checkpoint(sensor: SensorBlock | None, checkpoint_id: str = "checkpoint_1") -> Checkpoint:
    """A checkpoint carrying the given sensor reading."""
    return Checkpoint(
        checkpoint_id=checkpoint_id, checkpoint_name="Checkpoint 1", zone="checkpoint_1",
        status="COMPLETED", result_status="PASS", sensor=sensor,
    )


def gap_types(gaps: list[Gap]) -> list[GapType]:
    return [gap.gap_type for gap in gaps]


def test_healthy_reading_has_no_gaps() -> None:
    assert sensor_gaps_for(checkpoint(sensor_block())) == []


def test_ta09_sps30_offline_suppresses_particulate() -> None:
    """The reference run's case: every particulate value reads 0.0 and must not print."""
    gaps = sensor_gaps_for(checkpoint(sensor_block(raw={"sps30_ok": False})))
    assert gap_types(gaps) == [GapType.SUBSYSTEM_OFFLINE]
    assert gaps[0].detail == "sps30_ok=false; particulate block not recorded"


def test_ta10_bme680_offline_suppresses_environment() -> None:
    """Plausible non-zero values are not an exemption."""
    sensor = sensor_block(
        raw={"bme680_ok": False}, environment=Environment(temperature_c=22.4, humidity_pct=48.1),
    )
    gaps = sensor_gaps_for(checkpoint(sensor))
    assert gap_types(gaps) == [GapType.SUBSYSTEM_OFFLINE]
    assert "environment" in gaps[0].detail


def test_ta11_adxl345_offline_suppresses_accelerometer() -> None:
    gaps = sensor_gaps_for(checkpoint(sensor_block(raw={"adxl345_ok": False})))
    assert gap_types(gaps) == [GapType.SUBSYSTEM_OFFLINE]
    assert "accelerometer" in gaps[0].detail


def test_every_flag_false_reports_every_block() -> None:
    """Three dead devices are three gaps, each naming its own block."""
    sensor = sensor_block(raw={"adxl345_ok": False, "bme680_ok": False, "sps30_ok": False})
    gaps = sensor_gaps_for(checkpoint(sensor))
    assert gap_types(gaps) == [GapType.SUBSYSTEM_OFFLINE] * 3
    assert {"accelerometer", "environment", "particulate"} == {
        gap.detail.split()[-4] for gap in gaps
    }


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"ok": False, "status": "degraded"}, "ok=false"),          # TA-12
        ({"ok": False}, "ok=false"),
        ({"status": "degraded"}, "not 'connected'"),
        ({"status": None}, "not 'connected'"),
        ({"sensor_hub_reachable": False}, "hub was not reachable"),
    ],
)
def test_ta12_unusable_reading_is_unavailable(overrides: dict, expected: str) -> None:
    gaps = sensor_gaps_for(checkpoint(sensor_block(**overrides)))
    assert gap_types(gaps) == [GapType.SENSOR_UNAVAILABLE]
    assert expected in gaps[0].detail


def test_ta14_no_sensor_block_at_all() -> None:
    """Handled as unavailable, not a crash."""
    gaps = sensor_gaps_for(checkpoint(None))
    assert gap_types(gaps) == [GapType.SENSOR_UNAVAILABLE]
    assert gaps[0].detail == "no sensor reading was recorded"


def test_unavailable_reading_reports_nothing_else() -> None:
    """The whole table reads unavailable, so no gap may name a block inside it."""
    sensor = sensor_block(ok=False, age_seconds=480, raw={"sps30_ok": False})
    assert gap_types(sensor_gaps_for(checkpoint(sensor))) == [GapType.SENSOR_UNAVAILABLE]


def test_ta13_stale_reading_still_renders() -> None:
    gaps = sensor_gaps_for(checkpoint(sensor_block(age_seconds=480)))
    assert gap_types(gaps) == [GapType.STALE_READING]
    assert "480 s old" in gaps[0].detail
    assert "120 s maximum" in gaps[0].detail


@pytest.mark.parametrize("age", [0.0, 119.9, 120.0])
def test_reading_at_or_under_the_maximum_is_not_stale(age: float) -> None:
    assert sensor_gaps_for(checkpoint(sensor_block(age_seconds=age))) == []


def test_stale_and_offline_are_reported_together() -> None:
    """A trusted-but-stale reading can still have a dead device inside it."""
    sensor = sensor_block(age_seconds=300, raw={"sps30_ok": False})
    assert gap_types(sensor_gaps_for(checkpoint(sensor))) == [
        GapType.SUBSYSTEM_OFFLINE, GapType.STALE_READING,
    ]


def test_absent_raw_block_reports_no_subsystem_gaps() -> None:
    """No flag says false, so nothing is disqualified. Never crashes."""
    assert sensor_gaps_for(checkpoint(sensor_block(raw=None))) == []


def test_gaps_are_owned_by_their_checkpoint() -> None:
    gaps = sensor_gaps_for(checkpoint(sensor_block(raw={"sps30_ok": False}), "checkpoint_7"))
    assert all(gap.item_id == "checkpoint_7" for gap in gaps)


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_reference_run_reports_sps30_at_every_checkpoint() -> None:
    """sps30_ok is false at all eight, and every particulate value reads 0.0."""
    record = load_record(REFERENCE_RUN)
    gaps = [gap for c in record.checkpoints for gap in sensor_gaps_for(c)]
    assert len(gaps) == 8
    assert all(gap.gap_type is GapType.SUBSYSTEM_OFFLINE for gap in gaps)
    assert all("particulate" in gap.detail for gap in gaps)


# --- the evidence rules ----------------------------------------------------


def evidence_checkpoint(**overrides) -> Checkpoint:
    """A completed checkpoint, with the given fields changed."""
    defaults = {
        "checkpoint_id": overrides.pop("checkpoint_id", "checkpoint_1"),
        "checkpoint_name": "Checkpoint 1",
        "zone": "checkpoint_1", "status": "COMPLETED", "result_status": "PASS",
        "observed": "normal_scene", "evidence_images": ("checkpoint_1_N.jpg",),
    }
    return Checkpoint(**{**defaults, **overrides})


def rgb(direction: str, readable: bool = True) -> ResolvedImage:
    return ResolvedImage(
        f"checkpoint_1_{direction}.jpg", Path("x.jpg"), direction, False,
        True, readable, None if readable else "file is empty",
    )


def thermal(direction: str) -> ResolvedImage:
    return ResolvedImage(
        f"checkpoint_1_{direction}_thermal.jpg", Path("x.jpg"), direction, True,
        True, True, None,
    )


def test_missed_checkpoint_reports_its_reason() -> None:
    point = evidence_checkpoint(
        status="MISSED", missed_reason="Navigation failed", evidence_images=(),
    )
    gaps = [*missed_checkpoint_gaps(point), *no_evidence_gaps(point)]
    assert gap_types(gaps) == [GapType.MISSED_CHECKPOINT, GapType.NO_EVIDENCE]
    assert gaps[0].detail == "Navigation failed"


def test_missed_checkpoint_without_a_reason() -> None:
    point = evidence_checkpoint(status="MISSED", evidence_images=())
    assert missed_checkpoint_gaps(point)[0].detail == "no reason recorded"


def test_a_status_that_is_neither_completed_nor_missed_is_not_a_miss() -> None:
    """Some records carry PENDING. It is rendered as written, not read as a miss."""
    point = evidence_checkpoint(status="PENDING", evidence_images=())
    assert missed_checkpoint_gaps(point) == []


def test_no_evidence_from_an_empty_list() -> None:
    gaps = no_evidence_gaps(evidence_checkpoint(evidence_images=()))
    assert gap_types(gaps) == [GapType.NO_EVIDENCE]
    assert gaps[0].detail == "evidence_images empty"


def test_no_evidence_from_the_observed_field() -> None:
    """Images were listed, but the check itself says nothing was seen."""
    point = evidence_checkpoint(observed="no_evidence")
    gaps = no_evidence_gaps(point)
    assert gap_types(gaps) == [GapType.NO_EVIDENCE]
    assert gaps[0].detail == "observed=no_evidence"


def test_no_evidence_detail_names_both_causes() -> None:
    """The reference run's home_docking_station, exactly as the manifest has it."""
    point = evidence_checkpoint(evidence_images=(), observed="no_evidence")
    gaps = no_evidence_gaps(point)
    assert gaps[0].detail == "evidence_images empty; observed=no_evidence"


def test_ta18_to_ta20_one_gap_per_broken_image_with_its_reason() -> None:
    images = [rgb("N"), rgb("NE", readable=False), rgb("E", readable=False)]
    gaps = missing_image_gaps(evidence_checkpoint(), images)
    assert len(gaps) == 2
    assert gaps[0].detail == "checkpoint_1_NE.jpg: file is empty"


def test_ta16_eight_rgb_and_no_thermal_names_every_direction() -> None:
    images = [rgb(d) for d in DIRECTIONS]
    gaps = missing_thermal_gaps(evidence_checkpoint(), build_direction_grid(images))
    assert gaps[0].detail == (
        "directions N, NE, E, SE, S, SW, W, NW have RGB with no thermal counterpart"
    )


def test_ta15_every_direction_paired_reports_nothing() -> None:
    images = [rgb(d) for d in DIRECTIONS] + [thermal(d) for d in DIRECTIONS]
    point = evidence_checkpoint()
    assert missing_thermal_gaps(point, build_direction_grid(images)) == []
    assert missing_image_gaps(point, images) == []


def test_one_unpaired_direction_reads_as_singular() -> None:
    images = [rgb(d) for d in DIRECTIONS] + [thermal(d) for d in DIRECTIONS if d != "NW"]
    gaps = missing_thermal_gaps(evidence_checkpoint(), build_direction_grid(images))
    assert gaps[0].detail == "direction NW has RGB with no thermal counterpart"


def test_ta17_a_direction_nobody_listed_is_not_a_gap() -> None:
    """Five directions captured. The other three are placeholders, not gaps."""
    images = [rgb(d) for d in ("N", "NE", "E", "SE", "S")]
    images += [thermal(d) for d in ("N", "NE", "E", "SE", "S")]
    point = evidence_checkpoint()
    assert missing_thermal_gaps(point, build_direction_grid(images)) == []
    assert missing_image_gaps(point, images) == []


def test_a_broken_rgb_still_reports_its_missing_thermal() -> None:
    """Decision 14, provisional: one broken file must not suppress a second gap."""
    point, images = evidence_checkpoint(), [rgb("N", readable=False)]
    assert gap_types(missing_image_gaps(point, images)) == [GapType.MISSING_IMAGE]
    assert gap_types(missing_thermal_gaps(point, build_direction_grid(images))) == [
        GapType.MISSING_THERMAL,
    ]


# --- the count rules -------------------------------------------------------


def run_record(**overrides) -> Record:
    """A run whose declared counts all agree with its arrays."""
    points = overrides.pop("checkpoints", (
        evidence_checkpoint(checkpoint_id="checkpoint_1", status="COMPLETED", result_status="PASS"),
        evidence_checkpoint(checkpoint_id="checkpoint_2", status="COMPLETED", result_status="FAIL"),
    ))
    defaults = {
        "run_id": "r", "facility_id": "f", "facility_name": "F",
        "run_status": "COMPLETED", "final_status": "FAIL",
        "checkpoints": points,
        "total_required_checkpoints": len(points),
        "total_completed_checkpoints": sum(1 for c in points if c.status == "COMPLETED"),
        "passed_checkpoints": sum(1 for c in points if c.result_status == "PASS"),
        "failed_checkpoints": sum(1 for c in points if c.result_status == "FAIL"),
        "missed_checkpoints": sum(1 for c in points if c.status == "MISSED"),
        "warned_checkpoints": sum(1 for c in points if c.result_status == "WARN"),
        "finding_count": 0,
    }
    return Record(**{**defaults, **overrides})


def test_counts_that_agree_raise_nothing() -> None:
    assert count_mismatch_gaps(run_record()) == []


def test_declared_counts_are_copied_verbatim() -> None:
    """Never corrected, even when wrong."""
    record = run_record(passed_checkpoints=99)
    assert declared_counts(record).passed == 99
    assert computed_counts(record).passed == 1


@pytest.mark.parametrize(
    ("field", "value", "named"),
    [
        ("total_required_checkpoints", 8, "total_required_checkpoints declared 8; 2 checkpoints"),
        ("total_completed_checkpoints", 5, "total_completed_checkpoints declared 5; 2 checkpoints"),
        ("passed_checkpoints", 7, "passed_checkpoints declared 7; 1 checkpoints"),
        ("failed_checkpoints", 0, "failed_checkpoints declared 0; 1 checkpoints"),
        ("missed_checkpoints", 3, "missed_checkpoints declared 3; 0 checkpoints"),
    ],
)
def test_each_row_of_the_coverage_page_is_checked(field: str, value: int, named: str) -> None:
    gaps = count_mismatch_gaps(run_record(**{field: value}))
    assert [g.gap_type for g in gaps] == [GapType.COUNT_MISMATCH]
    assert gaps[0].item_id == RUN_LEVEL
    assert gaps[0].detail.startswith(named)


def test_ta24_finding_count_disagrees_with_the_findings_array() -> None:
    """Both figures printed, neither silently preferred."""
    findings = tuple(
        Finding(finding_id=f"f{i}", severity="fail", status="logged") for i in range(2)
    )
    gaps = count_mismatch_gaps(run_record(finding_count=3, findings=findings))
    assert gaps[0].detail == "finding_count declared 3; 2 findings in the array"


def test_ta23_warned_checkpoints_against_sensor_warnings() -> None:
    """The contradiction the coverage rows cannot see."""
    warned = SensorWarning(code="temperature_high", severity="warning")
    points = tuple(
        evidence_checkpoint(
            checkpoint_id=f"checkpoint_{i}", status="COMPLETED", result_status="PASS",
            sensor=sensor_block(warnings=(warned,)),
        )
        for i in range(6)
    )
    record = run_record(checkpoints=points, warned_checkpoints=0)

    # The coverage row agrees: nothing has result_status WARN.
    assert declared_counts(record).warned == computed_counts(record).warned == 0

    gaps = count_mismatch_gaps(record)
    assert [g.gap_type for g in gaps] == [GapType.COUNT_MISMATCH]
    assert gaps[0].item_id == RUN_LEVEL
    assert gaps[0].detail == "warned_checkpoints declared 0; 6 checkpoints carry sensor warnings"


def test_ta25_event_evidence_count_disagrees_per_checkpoint() -> None:
    """Surfaced against the checkpoint, not the run."""
    point = evidence_checkpoint(
        checkpoint_id="checkpoint_1",
        evidence_images=tuple(f"checkpoint_1_{d}.jpg" for d in DIRECTIONS[:9]),
    )
    event = Event(
        event_id="e1", event_type="checkpoint_completed",
        checkpoint_id="checkpoint_1", evidence_count=12,
    )
    record = run_record(checkpoints=(point,), event_log=(event,))
    gaps = [g for g in count_mismatch_gaps(record) if g.item_id == "checkpoint_1"]
    assert [g.gap_type for g in gaps] == [GapType.COUNT_MISMATCH]
    assert gaps[0].detail == "event_log declared evidence_count 12; evidence_images lists 8"


def test_run_level_events_carry_no_evidence_count() -> None:
    """run_started and run_completed omit the key, so they raise nothing."""
    event = Event(event_id="e1", event_type="run_started")
    assert count_mismatch_gaps(run_record(event_log=(event,))) == []


def test_a_count_the_record_never_declared_raises_nothing() -> None:
    """No claim, so nothing to disagree with. The loader records the absence."""
    record = run_record(passed_checkpoints=None, warned_checkpoints=None)
    assert count_mismatch_gaps(record) == []


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_reference_run_count_mismatch() -> None:
    """Every coverage row agrees; the sensor-warning contradiction does not."""
    record = load_record(REFERENCE_RUN)
    assert declared_counts(record) == computed_counts(record)

    gaps = count_mismatch_gaps(record)
    assert len(gaps) == 1
    assert gaps[0].item_id == RUN_LEVEL
    assert gaps[0].detail == "warned_checkpoints declared 0; 7 checkpoints carry sensor warnings"


# --- findings and the empty record -----------------------------------------


def test_no_findings_for_this_checkpoint() -> None:
    point = evidence_checkpoint(checkpoint_id="checkpoint_1")
    other = Finding(finding_id="f1", severity="fail", status="logged",
                    checkpoint_id="checkpoint_2")
    gaps = no_findings_gaps(point, [other])
    assert gap_types(gaps) == [GapType.NO_FINDINGS]
    assert gaps[0].detail == "no findings recorded for this checkpoint"
    assert gaps[0].item_id == "checkpoint_1"


def test_a_checkpoint_with_a_finding_raises_nothing() -> None:
    point = evidence_checkpoint(checkpoint_id="checkpoint_1")
    finding = Finding(finding_id="f1", severity="fail", status="logged",
                      checkpoint_id="checkpoint_1")
    assert no_findings_gaps(point, [finding]) == []


def test_no_findings_with_an_empty_run() -> None:
    assert gap_types(no_findings_gaps(evidence_checkpoint(), [])) == [GapType.NO_FINDINGS]


def test_ta30_empty_checkpoints_array() -> None:
    """Valid structure otherwise. Not a crash."""
    record = run_record(checkpoints=(), total_required_checkpoints=0,
                        total_completed_checkpoints=0, passed_checkpoints=0,
                        failed_checkpoints=0, missed_checkpoints=0, warned_checkpoints=0)
    gaps = detect_gaps(record, {})
    assert GapType.EMPTY_RECORD in gap_types(gaps)
    assert [g for g in gaps if g.gap_type is GapType.EMPTY_RECORD][0].item_id == RUN_LEVEL
    assert [g for g in gaps if g.gap_type is GapType.EMPTY_RECORD][0].detail == (
        "no checkpoints recorded"
    )


def test_a_record_with_checkpoints_is_not_empty() -> None:
    assert empty_record_gaps(run_record()) == []


# --- detect_gaps -----------------------------------------------------------


def test_detect_gaps_orders_checkpoints_then_the_run() -> None:
    record = run_record(passed_checkpoints=99)
    gaps = detect_gaps(record, {})
    assert [g.item_id for g in gaps][-1] == RUN_LEVEL
    assert gaps[0].item_id == "checkpoint_1"


def test_detect_gaps_accepts_the_two_argument_call() -> None:
    """5.3's shape: grids are optional, and built here when not supplied."""
    record = run_record()
    assert detect_gaps(record, {}) == detect_gaps(record, {}, None)


def test_detect_gaps_uses_the_grids_it_is_given() -> None:
    point = evidence_checkpoint(checkpoint_id="checkpoint_1")
    images = {"checkpoint_1": [rgb("N")]}
    grids = {"checkpoint_1": build_direction_grid(images["checkpoint_1"])}
    record = run_record(checkpoints=(point,), total_required_checkpoints=1,
                        total_completed_checkpoints=1, passed_checkpoints=1,
                        failed_checkpoints=0)
    assert detect_gaps(record, images, grids) == detect_gaps(record, images)


def test_detect_gaps_never_returns_none() -> None:
    """An empty list means genuinely complete, not "not checked"."""
    point = evidence_checkpoint(checkpoint_id="checkpoint_1", observed="normal_scene",
                                sensor=sensor_block())
    finding = Finding(finding_id="f1", severity="info", status="logged",
                      checkpoint_id="checkpoint_1")
    images = {"checkpoint_1": [rgb("N"), thermal("N")]}
    record = run_record(
        checkpoints=(point,), findings=(finding,), finding_count=1,
        total_required_checkpoints=1, total_completed_checkpoints=1,
        passed_checkpoints=1, failed_checkpoints=0,
    )
    assert detect_gaps(record, images) == []


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_reference_run_every_gap() -> None:
    record = load_record(REFERENCE_RUN)
    gaps = detect_gaps(record, resolve_record_images(record, PROJECT_ROOT / "data"))

    assert collections.Counter(g.gap_type for g in gaps) == {
        GapType.SUBSYSTEM_OFFLINE: 8,    # sps30_ok false at every checkpoint
        GapType.NO_FINDINGS: 7,          # only home_docking_station has one
        GapType.MISSING_THERMAL: 6,      # checkpoint_2 is the only fully paired one
        GapType.NO_EVIDENCE: 1,          # home_docking_station
        GapType.COUNT_MISMATCH: 1,       # warned_checkpoints against sensor warnings
    }
    # Every gap is owned by a real checkpoint, or by the run.
    known = {c.checkpoint_id for c in record.checkpoints} | {RUN_LEVEL}
    assert {g.item_id for g in gaps} <= known


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_every_supplied_run_produces_gaps_without_crashing() -> None:
    for path in sorted((PROJECT_ROOT / "data").glob("*/records/run.json")):
        record = load_record(path)
        gaps = detect_gaps(record, resolve_record_images(record, PROJECT_ROOT / "data"))
        assert isinstance(gaps, list)
        assert all(isinstance(g.gap_type, GapType) for g in gaps)


# --- the manifest ----------------------------------------------------------


@pytest.fixture
def reference_manifest(tmp_path: Path) -> dict:
    """The manifest for the reference run."""
    record = load_record(REFERENCE_RUN)
    images = resolve_record_images(record, PROJECT_ROOT / "data")
    gaps = detect_gaps(record, images)
    return build_manifest(record, gaps, images, tmp_path / f"report_{record.run_id}.pdf")


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_manifest_has_every_field(reference_manifest: dict) -> None:
    assert list(reference_manifest) == [
        "manifest_version", "generated_at", "source_run_id", "facility_id",
        "engine_version", "template_version", "config_hash", "report_file",
        "page_count", "sections", "checkpoints_rendered", "checkpoints_with_gaps",
        "images_referenced", "images_rendered", "gaps", "counts", "alerts",
    ]


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_manifest_values(reference_manifest: dict) -> None:
    m = reference_manifest
    assert m["manifest_version"] == "1.0"
    assert m["source_run_id"] == "20260728_144120-sis_facility_checkpoint_route"
    assert m["facility_id"] == "sis_facility_checkpoint_route"
    assert m["report_file"] == "report_20260728_144120-sis_facility_checkpoint_route.pdf"
    assert m["checkpoints_rendered"] == 8
    assert m["images_referenced"] == 87
    assert m["images_rendered"] == 87
    assert m["alerts"] == {"critical": 8, "warning": 12}


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_generated_at_is_iso_utc(reference_manifest: dict) -> None:
    assert datetime.strptime(reference_manifest["generated_at"], "%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_checkpoints_with_gaps_counts_checkpoints_not_gaps(reference_manifest: dict) -> None:
    """23 gaps across 8 checkpoints, and the run itself, which does not count."""
    assert len(reference_manifest["gaps"]) == 23
    assert reference_manifest["checkpoints_with_gaps"] == 8
    assert RUN_LEVEL in {g["item_id"] for g in reference_manifest["gaps"]}


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_manifest_gap_types_are_all_known(reference_manifest: dict) -> None:
    """A closed set: the manifest can never carry a type outside the ten."""
    assert {g["gap_type"] for g in reference_manifest["gaps"]} <= {g.value for g in GapType}


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_counts_have_both_halves(reference_manifest: dict) -> None:
    keys = {"required", "completed", "passed", "failed", "missed", "warned", "findings"}
    assert set(reference_manifest["counts"]["declared"]) == keys
    assert set(reference_manifest["counts"]["computed"]) == keys


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_manifest_is_json(reference_manifest: dict) -> None:
    """No enum or Path leaks into it: it must serialise with no coaxing."""
    assert json.loads(json.dumps(reference_manifest)) == reference_manifest


def test_config_hash_is_prefixed_and_stable() -> None:
    first = config_hash()
    assert first.startswith("sha256:")
    assert len(first) == len("sha256:") + 64
    assert first == config_hash()


def test_config_hash_follows_the_values(tmp_path: Path) -> None:
    """Changing a value changes the hash; reordering keys does not."""
    same = write_config(tmp_path / "a", MINIMAL_CONFIG)
    reordered = write_config(tmp_path / "b", REORDERED_CONFIG)
    changed = write_config(tmp_path / "c", MINIMAL_CONFIG.replace("/run-files", "/other"))

    assert config_hash(same) == config_hash(reordered)   # same values, different order
    assert config_hash(same) != config_hash(changed)     # one value differs


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_write_manifest_names_the_file_after_the_run(
    reference_manifest: dict, tmp_path: Path
) -> None:
    path = write_manifest(reference_manifest, tmp_path)
    assert path.name == "report_20260728_144120-sis_facility_checkpoint_route.manifest.json"
    assert json.loads(path.read_text(encoding="utf-8")) == reference_manifest


def test_placeholder_fields_are_named() -> None:
    """Nothing should ship believing page_count or sections are real."""
    assert PLACEHOLDER_FIELDS == ("page_count", "sections")
