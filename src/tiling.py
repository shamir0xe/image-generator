"""Tiling strategies: how the mosaic traverses (and fills) the target.

A strategy emits an ordered list of `Placement`s -- a tile center plus a
rotation -- that cover the target. The *same* list drives both passes of the
pipeline:

  * sampling (`ImageModifier.tile_target`) averages the target's colors under
    each placement, in order, to get the per-cell target color;
  * rendering (`construct_box`) drops the matched movie frame onto each
    placement, in the same order and orientation.

Because both passes walk the identical list, cell `i` always means the same
tile. Coordinates are normalized so the list is independent of pixel size:

  * `v` (vertical) runs 0..1 over the canvas height,
  * `u` (horizontal) runs 0..`aspect` (canvas width / height),
  * `angle` is degrees, counter-clockwise, 0 = upright.

Tiles are a uniform size set by the caller (`1/rows` tall, `ratio` wide); a
strategy only decides *where* each tile sits and *how* it is turned.

Add a strategy by subclassing `TilingStrategy`, giving it a unique `name`,
implementing `placements`, and listing an instance in `_STRATEGIES`.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Placement:
    # Tile center in normalized canvas coordinates (u in [0, aspect], v in
    # [0, 1]) and the tile's rotation in degrees (CCW, 0 = upright).
    u: float
    v: float
    angle: float = 0.0


class TilingStrategy:
    name: str = "base"

    def placements(self, rows: int, ratio: float, aspect: float) -> list[Placement]:
        """Ordered tile centers covering the unit-height canvas.

        `rows`  -- tiles stacked along the height (the `box` resolution knob),
        `ratio` -- tile aspect (width / height),
        `aspect`-- canvas aspect (width / height).
        """
        raise NotImplementedError


class CrossboardTiling(TilingStrategy):
    """The original layout: a plain aligned grid, upright tiles."""

    name = "crossboard"

    def placements(self, rows: int, ratio: float, aspect: float) -> list[Placement]:
        h_n = 1.0 / rows
        w_n = h_n * ratio
        cols = max(1, math.ceil(aspect / w_n))
        out: list[Placement] = []
        for j in range(rows):
            for i in range(cols):
                out.append(Placement((i + 0.5) * w_n, (j + 0.5) * h_n))
        return out


class BrickTiling(TilingStrategy):
    """A brick wall: every other row is shifted by half a tile width.

    The shift overflows the right edge (clipped) and leaves a half-tile gap on
    the left of the offset rows -- both mostly trimmed by the final A3 crop.
    """

    name = "brick"

    def placements(self, rows: int, ratio: float, aspect: float) -> list[Placement]:
        h_n = 1.0 / rows
        w_n = h_n * ratio
        cols = max(1, math.ceil(aspect / w_n))
        out: list[Placement] = []
        for j in range(rows):
            shift = w_n / 2.0 if j % 2 == 1 else 0.0
            for i in range(cols):
                out.append(Placement((i + 0.5) * w_n + shift, (j + 0.5) * h_n))
        return out


class CircularTiling(TilingStrategy):
    """Concentric rings centered on the image, each tile turned tangent to its
    radius (its "up" points along the radius). Inner rings hold few tiles, outer
    rings more, so the whole canvas fills out from the middle."""

    name = "circular"

    def placements(self, rows: int, ratio: float, aspect: float) -> list[Placement]:
        h_n = 1.0 / rows          # ring spacing == tile height
        w_n = h_n * ratio
        cx, cy = aspect / 2.0, 0.5
        max_r = math.hypot(cx, cy)  # reach the farthest corner
        n_rings = max(1, math.ceil(max_r / h_n))

        out: list[Placement] = []
        for r in range(n_rings + 1):
            radius = r * h_n
            if radius == 0.0:
                out.append(Placement(cx, cy, 0.0))
                continue
            count = max(1, round(2 * math.pi * radius / w_n))
            for t in range(count):
                theta = 2 * math.pi * t / count
                u = cx + radius * math.cos(theta)
                v = cy + radius * math.sin(theta)
                # Drop tiles whose center falls outside the canvas so the
                # matcher doesn't spend frames on off-image cells.
                if not (0.0 <= u <= aspect and 0.0 <= v <= 1.0):
                    continue
                # Turn the tile so its long (width) axis runs along the tangent
                # -- i.e. perpendicular to the radius, frames wrapping the ring.
                # The image y-axis points down while PIL rotates CCW, hence the
                # negated theta.
                out.append(Placement(u, v, 90.0 - math.degrees(theta)))
        return out


_STRATEGIES = {
    s.name: s for s in (CrossboardTiling(), BrickTiling(), CircularTiling())
}


def tiling_names() -> list[str]:
    return sorted(_STRATEGIES)


def get_tiling(name: str) -> TilingStrategy:
    try:
        return _STRATEGIES[name]
    except KeyError:
        raise ValueError(
            f"Unknown tiling '{name}'. Available: {', '.join(tiling_names())}"
        )
