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


_config = load_config(CONFIG_PATH)

#: The compass directions and thermal marker, from config/report.yaml.
_directions = setting(_config, "evidence", "directions")
if not isinstance(_directions, list) or not _directions:
    raise ConfigError(f"{CONFIG_PATH}: 'evidence.directions' must be a non-empty list")

DIRECTIONS: tuple[str, ...] = tuple(_directions)
THERMAL_SUFFIX: str = setting(_config, "evidence", "thermal_suffix")
PATH_PREFIX: str = setting(_config, "paths", "path_prefix")


@dataclass(frozen=True)
class ResolvedImage:
    """One recorded evidence path, and what could be made of it."""

    original_uri: str
    local_path: Path | None
    direction: str | None
    is_thermal: bool
    exists: bool
    readable: bool
    reason: str | None


def parse_filename(uri: str) -> tuple[str | None, bool]:
    """Read the direction and the thermal flag out of an evidence filename.

    Filenames look like `checkpoint_1_N.jpg` or `checkpoint_1_N_thermal.jpg`.
    Checkpoint ids contain underscores too, so the direction is the last
    underscore-separated token; comparing whole tokens keeps N and NE apart.
    """
    stem = PurePosixPath(uri).stem

    is_thermal = stem.endswith(THERMAL_SUFFIX)
    if is_thermal:
        stem = stem[: -len(THERMAL_SUFFIX)]

    token = stem.rsplit("_", 1)[-1].upper()
    return (token if token in DIRECTIONS else None), is_thermal


def resolve_evidence(
    uri: str, evidence_root: Path, path_prefix: str = PATH_PREFIX
) -> ResolvedImage:
    """Resolve one recorded evidence path against a local evidence root.

    Strips `path_prefix` from the recorded path, joins the rest to
    `evidence_root`, and reports whether a usable image is there. Never raises:
    a path that does not resolve, or a file that is empty or will not decode,
    comes back with a reason so the report can show the gap.
    """
    direction, is_thermal = parse_filename(uri)

    relative = uri[len(path_prefix):] if uri.startswith(path_prefix) else uri
    local_path = evidence_root / relative.lstrip("/")

    def result(exists: bool, readable: bool, reason: str | None) -> ResolvedImage:
        return ResolvedImage(uri, local_path, direction, is_thermal, exists, readable, reason)

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
    return [resolve_evidence(uri, evidence_root) for uri in checkpoint.evidence_images]


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
