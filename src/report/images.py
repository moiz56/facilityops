"""Build the evidence grid, and size an image down before it is embedded.

Two jobs, kept apart: pairing an image with the view it belongs to, and making
it small enough to put in a PDF. 5.5 puts the resize here rather than in a
template, and 5.7 puts the limit in config rather than in this file.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from common.paths import DEFAULT_CONFIG, ResolvedImage, setting

logger = logging.getLogger("report.images")

#: Longest edge, in pixels, that an image may have when it is embedded (5.5,
#: TUNABLE). Sixty checkpoints at sixteen full-resolution images each is what
#: this limit exists to stop.
MAX_IMAGE_DIMENSION: int = setting(DEFAULT_CONFIG, "report", "max_image_dimension")

#: The most cells the evidence grid puts on a row. A checkpoint that recorded
#: fewer views uses fewer, so the grid fans out rather than padding (3.2, 5.7).
GRID_COLUMNS: int = setting(DEFAULT_CONFIG, "evidence", "grid_columns")


@dataclass(frozen=True)
class ViewCell:
    """One cell of the evidence grid: one view, in up to two modalities.

    `view` is what the filename called this view - `N` on a compass route,
    `h120` on one that records headings, None where a checkpoint captured a
    single view and labelled it nothing.

    `rgb` and `thermal` are None when the checkpoint recorded no image for that
    view and modality. An image that is present but unusable is still here,
    carrying its reason, so the cell can say what went wrong instead of looking
    like nothing was captured.
    """

    view: str | None
    rgb: ResolvedImage | None
    thermal: ResolvedImage | None


def build_view_grid(images: list[ResolvedImage]) -> list[ViewCell]:
    """Return one cell per view the checkpoint recorded, in recorded order.

    A cell pairs the RGB and the thermal carrying the same view label, so a pair
    draws as one picture above another rather than as two unrelated cells. How
    many cells a checkpoint gets is therefore how many views it recorded: eight
    on a compass route that photographed all eight, three where it recorded
    `h000`, `h120` and `h240`, one where it took a single unlabelled picture.

    Nothing is expected and nothing is invented. Without a fixed vocabulary
    there is no way to know a view was meant to exist, so a view nobody
    photographed has no cell rather than an empty one. What was captured is
    still checked against what the record claimed: an empty `evidence_images`
    is NO_EVIDENCE, a path that does not resolve is MISSING_IMAGE, and a
    recorded count that disagrees is COUNT_MISMATCH.

    Views are paired without regard to case, so `checkpoint_1_n.jpg` shares a
    cell with `checkpoint_1_N_thermal.jpg` rather than opening a second cell and
    a MISSING_THERMAL that is really a spelling (decision 13). The label prints
    as the first file spelled it; only the pairing ignores case.

    If two images claim the same view and modality, the first one wins.
    """
    labels: dict[str, str | None] = {}          # key -> the spelling to print
    rgb: dict[str, ResolvedImage] = {}
    thermal: dict[str, ResolvedImage] = {}

    for image in images:
        key = image.view.casefold() if image.view else ""
        labels.setdefault(key, image.view)
        found = thermal if image.is_thermal else rgb
        found.setdefault(key, image)

    return [
        ViewCell(view=label, rgb=rgb.get(key), thermal=thermal.get(key))
        for key, label in labels.items()
    ]


# The fixed-width implementation this replaced, kept so 2.3's eight-cell grid can
# be restored alongside the parse_filename and the DIRECTIONS constant it depends
# on (decision 174). It returns exactly len(DIRECTIONS) cells whatever the
# checkpoint recorded, which is what gives a compass route its "Image not
# captured" placeholders - and what leaves every cell empty on a route that does
# not name its views that way:
#
# def build_view_grid(images: list[ResolvedImage]) -> list[ViewCell]:
#     """Return one cell per compass direction, in fixed order.
#
#     Always returns a cell for every direction, whether or not an image exists
#     for it, so the grid keeps its shape and a missing direction shows a
#     placeholder rather than shifting the others along.
#
#     Images whose filename gave no direction are left out. If two images claim
#     the same direction and modality, the first one wins.
#     """
#     rgb: dict[str, ResolvedImage] = {}
#     thermal: dict[str, ResolvedImage] = {}
#
#     for image in images:
#         if image.direction is None:
#             continue
#         found = thermal if image.is_thermal else rgb
#         found.setdefault(image.direction, image)
#
#     return [
#         ViewCell(direction=direction, rgb=rgb.get(direction), thermal=thermal.get(direction))
#         for direction in DIRECTIONS
#     ]


# --- sizing an image down before it is embedded (5.5) ------------------------


def resize_to_fit(source: Path, image_dir: Path, limit: int = MAX_IMAGE_DIMENSION) -> Path:
    """Return a copy of `source` no larger than `limit` on its longest edge.

    An image already within the limit is returned untouched: rewriting it would
    cost a second JPEG generation and save nothing.

    The copy is named from a digest of the source path, so two checkpoints that
    recorded the same filename cannot overwrite each other's resized copy - and
    so a copy this run already made can be recognised and reused. It has to be:
    the document is laid out twice to settle the contents page numbers, and
    without that check the second pass re-opens, re-resizes and re-encodes every
    image the first one produced. `image_dir` is a fresh temporary directory per
    run, so there is nothing stale to find in it.

    Never raises. An image PIL will not resize is embedded at its recorded size
    and the reason logged - 11 asks for a record that does not match section 2
    to be handled rather than to crash, and a large page is a better failure
    than no page.
    """
    digest = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:12]
    destination = image_dir / f"{source.stem}-{digest}{source.suffix}"
    if destination.exists():
        return destination

    try:
        with Image.open(source) as opened:
            if max(opened.size) <= limit:
                return source
            resized = opened.copy()

        resized.thumbnail((limit, limit))
        if resized.mode not in ("RGB", "L") and source.suffix.lower() in (".jpg", ".jpeg"):
            resized = resized.convert("RGB")

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
