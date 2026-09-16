"""Evidence path rewriting: turn a path recorded on the robot into a local file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml
from PIL import Image

from common.schema import Checkpoint, Record

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "report.yaml"


class ConfigError(Exception):
    """config/report.yaml is missing, unreadable, or lacks a setting the engine needs."""


def load_config(path: Path) -> dict:
    """Read a config file, or raise ConfigError saying what is wrong with it."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"{path}: cannot be read: {error.strerror or error}") from None

    try:
        config = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ConfigError(f"{path}: is not valid YAML: {error}") from None

    if not isinstance(config, dict):
        found = "empty" if config is None else type(config).__name__
        raise ConfigError(f"{path}: is {found}, expected a mapping of settings")
    return config


def setting(config: dict, section: str, key: str):
    """Return one required setting, or raise ConfigError naming the one that is missing.

    The message names the key, which is what 5.7 asks for, and not a file. This
    takes a config that has already been read and cannot know which file it came
    from; naming the default one would point at the wrong file whenever a caller
    passed --config. load_config names the file, where the file is what is wrong.
    """
    block = config.get(section)
    if not isinstance(block, dict) or key not in block:
        raise ConfigError(f"missing required setting '{section}.{key}'")
    return block[key]


def units(config: dict) -> dict[str, str]:
    """The configured unit for each measurement field, field -> unit.

    Absent, empty or null is an empty mapping and not an error: printing no unit
    is a valid configuration, and the one the engine ships with (decision 130).
    A field with no entry is unitless, so the caller prints the bare number
    rather than supplying a unit the record never recorded.

    The values are validated rather than trusted, because a unit that arrived as
    a number or a list would reach the page as whatever str() made of it.
    """
    block = config.get("units")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise ConfigError(f"'units' must be a mapping of field to unit, not {type(block).__name__}")

    for field, unit in block.items():
        if not isinstance(unit, str):
            raise ConfigError(f"unit for '{field}' must be text, not {type(unit).__name__}")
    return dict(block)


def decimals(config: dict) -> dict[str, int]:
    """The decimal places each measurement field prints to, field -> places.

    Absent, empty or null is an empty mapping and not an error. A field with no
    entry keeps measure()'s significant-figure rule; an entry is what pins a
    column to one precision (decision 131).

    A non-integer or negative value is refused rather than passed to a format
    spec, where it would raise from inside a template with no field named.
    `True` is an int to Python and would silently mean one decimal place, so
    bools are refused too.
    """
    block = config.get("decimals")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise ConfigError(
            f"'decimals' must be a mapping of field to decimal places, not {type(block).__name__}"
        )

    for field, places in block.items():
        if isinstance(places, bool) or not isinstance(places, int) or places < 0:
            raise ConfigError(
                f"decimal places for '{field}' must be a whole number of 0 or more, not {places!r}"
            )
    return dict(block)


#: config/report.yaml, parsed. The settings a module reads at import come from
#: here, and they come from this file whatever --config says, so there is one
#: copy rather than one per module that wants a value out of it.
DEFAULT_CONFIG: dict = load_config(CONFIG_PATH)

#: The two modality markers a filename may carry. Thermal is always marked;
#: RGB is marked on some routes and unmarked on others, so an unmarked file is
#: RGB by default.
THERMAL_SUFFIX: str = setting(DEFAULT_CONFIG, "evidence", "thermal_suffix")
RGB_SUFFIX: str = setting(DEFAULT_CONFIG, "evidence", "rgb_suffix")

PATH_PREFIX: str = setting(DEFAULT_CONFIG, "paths", "path_prefix")


@dataclass(frozen=True)
class ResolvedImage:
    """One recorded evidence path, and what could be made of it."""

    original_uri: str
    local_path: Path | None
    view: str | None        # what the filename called this view, or None if unlabelled
    is_thermal: bool
    exists: bool
    readable: bool
    reason: str | None


def parse_filename(uri: str, checkpoint_id: str | None = None) -> tuple[str | None, bool]:
    """Read the view label and the thermal flag out of an evidence filename.

    A filename is `<checkpoint_id>[_<view>][_<modality>].<ext>`. The modality
    marker comes off first, then the checkpoint id, and whatever is left is the
    view label - taken as recorded, with no vocabulary imposed on it:

        checkpoint_1_N.jpg          -> ("N", False)
        checkpoint_1_NW_thermal.jpg -> ("NW", True)
        ac_2_h120_rgb.jpg           -> ("h120", False)
        a4_back_rgb.jpg             -> (None, False)     one unlabelled view
        a4_back_thermal.jpg         -> (None, True)      its thermal counterpart

    Both modalities of one view come back with the same label, which is what
    lets build_view_grid pair them into a single cell. An unlabelled view
    is None rather than "", so a checkpoint that photographed itself once has
    one cell with no caption instead of one captioned with the empty string.

    `checkpoint_id` is what the label is measured against. Without it the whole
    stem is the label, since there is no way to tell which part of
    `a4_back_rgb` is the id and which the view.
    """
    stem = PurePosixPath(uri).stem

    is_thermal = stem.endswith(THERMAL_SUFFIX)
    if is_thermal:
        stem = stem[: -len(THERMAL_SUFFIX)]
    elif RGB_SUFFIX and stem.endswith(RGB_SUFFIX):
        stem = stem[: -len(RGB_SUFFIX)]

    if checkpoint_id and stem.startswith(checkpoint_id):
        stem = stem[len(checkpoint_id):]

    return (stem.strip("_") or None), is_thermal


# The compass implementation this replaced, kept so 2.3's fixed behaviour can be
# restored (decision 174). Restoring it also means restoring the DIRECTIONS
# constant and the `evidence.directions` config key it reads, both removed with
# it. It keeps the last underscore-separated token only if that token names a
# compass point, so a filename from any route that does not use compass points
# yields nothing at all and drops out of the grid entirely:
#
# _directions = setting(DEFAULT_CONFIG, "evidence", "directions")
# if not isinstance(_directions, list) or not _directions:
#     raise ConfigError(f"{CONFIG_PATH}: 'evidence.directions' must be a non-empty list")
# DIRECTIONS: tuple[str, ...] = tuple(_directions)
#
# def parse_filename(uri: str) -> tuple[str | None, bool]:
#     """Read the direction and the thermal flag out of an evidence filename.
#
#     Filenames look like `checkpoint_1_N.jpg` or `checkpoint_1_N_thermal.jpg`.
#     Checkpoint ids contain underscores too, so the direction is the last
#     underscore-separated token; comparing whole tokens keeps N and NE apart.
#     """
#     stem = PurePosixPath(uri).stem
#
#     is_thermal = stem.endswith(THERMAL_SUFFIX)
#     if is_thermal:
#         stem = stem[: -len(THERMAL_SUFFIX)]
#
#     token = stem.rsplit("_", 1)[-1].upper()
#     return (token if token in DIRECTIONS else None), is_thermal


def resolve_evidence(
    uri: str, evidence_root: Path, path_prefix: str = PATH_PREFIX,
    checkpoint_id: str | None = None,
) -> ResolvedImage:
    """Resolve one recorded evidence path against a local evidence root.

    Strips `path_prefix` from the recorded path, joins the rest to
    `evidence_root`, and reports whether a usable image is there. Never raises:
    a path that does not resolve, or a file that is empty or will not decode,
    comes back with a reason so the report can show the gap.

    `checkpoint_id` is passed to parse_filename, which measures the view label
    against it. Resolution itself does not use it.
    """
    view, is_thermal = parse_filename(uri, checkpoint_id)

    relative = uri[len(path_prefix):] if uri.startswith(path_prefix) else uri
    local_path = evidence_root / relative.lstrip("/")

    def result(exists: bool, readable: bool, reason: str | None) -> ResolvedImage:
        return ResolvedImage(uri, local_path, view, is_thermal, exists, readable, reason)

    if not local_path.exists():
        return result(False, False, f"no file at {local_path}")
    if local_path.stat().st_size == 0:
        return result(True, False, "file is empty")

    try:
        with Image.open(local_path) as image:
            image.verify()
    except Exception:
        return result(True, False, "file is not a readable image")

    return result(True, True, None)


def resolve_checkpoint_images(
    checkpoint: Checkpoint, evidence_root: Path
) -> list[ResolvedImage]:
    """Resolve every evidence path one checkpoint recorded."""
    return [
        resolve_evidence(uri, evidence_root, checkpoint_id=checkpoint.checkpoint_id)
        for uri in checkpoint.evidence_images
    ]


def resolve_record_images(
    record: Record, evidence_root: Path
) -> dict[str, list[ResolvedImage]]:
    """Resolve every evidence path in the run, keyed by checkpoint_id.

    If two checkpoints share an id - which the loader records as an anomaly -
    the second overwrites the first here. Use resolve_checkpoint_images per
    checkpoint when rendering, where each one needs its own images.
    """
    return {
        checkpoint.checkpoint_id: resolve_checkpoint_images(checkpoint, evidence_root)
        for checkpoint in record.checkpoints
    }
