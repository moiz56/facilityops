"""Tests for the report engine. Run with `pytest` from the project root."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from common.loader import load_record
from common.paths import (
    DIRECTIONS, PATH_PREFIX, THERMAL_SUFFIX, ConfigError, ResolvedImage, _load_config, _setting,
    parse_filename, resolve_checkpoint_images, resolve_evidence, resolve_record_images,
)
from report.cli import run_pipeline
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


def test_settings_come_from_report_yaml() -> None:
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


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "report.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_missing_config_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as error:
        _load_config(tmp_path / "absent.yaml")
    assert "absent.yaml" in str(error.value)
    assert "cannot be read" in str(error.value)


def test_invalid_yaml_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as error:
        _load_config(write_config(tmp_path, "evidence:\n  directions: [unclosed\n"))
    assert "not valid YAML" in str(error.value)


@pytest.mark.parametrize("body", ["", "- a list\n", "a string\n"])
def test_config_that_is_not_a_mapping_is_reported(tmp_path: Path, body: str) -> None:
    with pytest.raises(ConfigError) as error:
        _load_config(write_config(tmp_path, body))
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
def test_missing_setting_names_the_setting(config: dict, named: str) -> None:
    section, key = named.split(".")
    with pytest.raises(ConfigError) as error:
        _setting(config, section, key)
    assert named in str(error.value)
    assert "missing required setting" in str(error.value)


def test_present_setting_is_returned() -> None:
    assert _setting({"evidence": {"thermal_suffix": "_ir"}}, "evidence", "thermal_suffix") == "_ir"


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
    artifacts = run_pipeline(REFERENCE_RUN, PROJECT_ROOT / "data")

    assert artifacts.record.run_id.startswith("20260728_144120")
    assert sum(len(f) for f in artifacts.images.values()) == 87
    assert set(artifacts.grids) == set(artifacts.images)
    assert all(len(grid) == 8 for grid in artifacts.grids.values())


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_run_pipeline_thermal_pairing_matches_the_reference_run() -> None:
    grids = run_pipeline(REFERENCE_RUN, PROJECT_ROOT / "data").grids
    paired = {cid: sum(1 for c in grid if c.thermal is not None) for cid, grid in grids.items()}
    assert paired["checkpoint_2"] == 8
    assert paired["checkpoint_1"] == 4
    assert paired["checkpoint_6"] == 0
    assert paired["checkpoint_7"] == 0


@pytest.mark.skipif(not REFERENCE_RUN.is_file(), reason="reference run not present in data/")
def test_run_pipeline_renders_a_grid_for_a_checkpoint_with_no_evidence() -> None:
    """home_docking_station has no images, and still gets eight empty cells."""
    grids = run_pipeline(REFERENCE_RUN, PROJECT_ROOT / "data").grids
    grid = grids["home_docking_station"]
    assert len(grid) == 8
    assert all(c.rgb is None and c.thermal is None for c in grid)
