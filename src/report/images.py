"""Build the evidence grid: one cell per compass direction, RGB with its thermal pair."""

from __future__ import annotations

from dataclasses import dataclass

from common.paths import DIRECTIONS, ResolvedImage

__all__ = ["DirectionCell", "build_direction_grid"]


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
