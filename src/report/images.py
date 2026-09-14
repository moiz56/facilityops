"""Build the evidence grid, and size an image down before it is embedded.

Two jobs, kept apart: pairing an image with its direction, and making it small
enough to put in a PDF. 5.5 puts the resize here rather than in a template, and
5.7 puts the limit in config rather than in this file.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from common.paths import CONFIG_PATH, DIRECTIONS, ResolvedImage, load_config, setting

__all__ = [
    "DirectionCell", "build_direction_grid",
    "MAX_IMAGE_DIMENSION", "GRID_COLUMNS",
    "resize_to_fit", "prepare_image",
]

logger = logging.getLogger("report.images")

_config = load_config(CONFIG_PATH)

#: Longest edge, in pixels, that an image may have when it is embedded (5.5,
#: TUNABLE). Sixty checkpoints at sixteen full-resolution images each is what
#: this limit exists to stop.
MAX_IMAGE_DIMENSION: int = setting(_config, "report", "max_image_dimension")

#: How many cells the evidence grid puts on a row (3.2, 5.7).
GRID_COLUMNS: int = setting(_config, "evidence", "grid_columns")


@dataclass(frozen=True)
class DirectionCell:
    """One cell of the evidence grid.

    `rgb` and `thermal` are None when the checkpoint recorded no image for that
    direction and modality. An image that is present but unusable is still here,
    carrying its reason, so the cell can say what went wrong instead of looking
    like nothing was captured.
    """

    direction: str
    rgb: ResolvedImage | None
    thermal: ResolvedImage | None


def build_direction_grid(images: list[ResolvedImage]) -> list[DirectionCell]:
    """Return one cell per compass direction, in fixed order.

    Always returns a cell for every direction, whether or not an image exists
    for it, so the grid keeps its shape and a missing direction shows a
    placeholder rather than shifting the others along.

    Images whose filename gave no direction are left out. If two images claim
    the same direction and modality, the first one wins.
    """
    rgb: dict[str, ResolvedImage] = {}
    thermal: dict[str, ResolvedImage] = {}

    for image in images:
        if image.direction is None:
            continue
        found = thermal if image.is_thermal else rgb
        found.setdefault(image.direction, image)

    return [
        DirectionCell(direction=direction, rgb=rgb.get(direction), thermal=thermal.get(direction))
        for direction in DIRECTIONS
    ]


# --- sizing an image down before it is embedded (5.5) ------------------------


def resize_to_fit(source: Path, image_dir: Path, limit: int = MAX_IMAGE_DIMENSION) -> Path:
    """Return a copy of `source` no larger than `limit` on its longest edge.

    An image already within the limit is returned untouched: rewriting it would
    cost a second JPEG generation and save nothing.

    The copy is named from a digest of the source path, so two checkpoints that
    recorded the same filename cannot overwrite each other's resized copy.

    Never raises. An image PIL will not resize is embedded at its recorded size
    and the reason logged - 11 asks for a record that does not match section 2
    to be handled rather than to crash, and a large page is a better failure
    than no page.
    """
    try:
        with Image.open(source) as opened:
            if max(opened.size) <= limit:
                return source
            resized = opened.copy()

        resized.thumbnail((limit, limit))
        if resized.mode not in ("RGB", "L") and source.suffix.lower() in (".jpg", ".jpeg"):
            resized = resized.convert("RGB")

        digest = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:12]
        destination = image_dir / f"{source.stem}-{digest}{source.suffix}"
        resized.save(destination)
        return destination
    except Exception as error:
        logger.warning("could not resize %s, embedding as recorded: %s", source, error)
        return source


def prepare_image(
    image: ResolvedImage, image_dir: Path | None, limit: int = MAX_IMAGE_DIMENSION
) -> Path | None:
    """The file to embed for one resolved image, or None when there is nothing to embed.

    An image that did not resolve, or that will not decode, has no file to show
    and the cell says so in words instead (3.6).

    `image_dir` is where resized copies are written. None means no resizing -
    an HTML render embeds nothing, so there is nothing to keep within budget.
    """
    if image.local_path is None or not image.readable:
        return None
    if image_dir is None:
        return image.local_path
    return resize_to_fit(image.local_path, image_dir, limit)
