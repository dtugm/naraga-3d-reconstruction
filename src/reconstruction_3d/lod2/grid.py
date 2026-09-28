"""Quantisation grid and vertex pool -- the identity substrate for the pipeline.

Every coordinate is snapped to one grid exactly once. Two roof faces that meet
at a corner then produce bit-identical integer keys, so "the same vertex"
becomes an exact dict lookup rather than a tolerance search. That is what makes
a watertight shell achievable at all.

CityJSON 1.1 stores vertices as integers with

    world = integer * transform.scale + transform.translate

so the integers that guarantee watertightness are also the ones serialised.

Two resolutions, one grid:

  * topology_tolerance T (default 0.01 m) -- the grid geometry is snapped to,
    matching the grid_size handed to shapely.set_precision.
  * output_scale s (default 0.001 m) -- the CityJSON transform.scale.

T is an exact multiple of s, and translate is floored onto the T grid, so every
snapped coordinate lands exactly on an integer.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

PlanKey = tuple[int, int]
VertexKey = tuple[int, int, int]


def _iround(value: float) -> int:
    """Round half away from zero, deterministically.

    Python's round() is half-to-even, which would send 0.005 and 0.015 to
    different sides of the same grid line.
    """
    return int(math.floor(value + 0.5)) if value >= 0 else -int(math.floor(-value + 0.5))


def _decimals(step: float) -> int:
    """Decimal places needed to write `step` exactly, e.g. 0.01 -> 2."""
    return max(0, int(math.ceil(-math.log10(step) - 1e-9)))


def signed_area2(ring: Sequence[PlanKey]) -> int:
    """Twice the signed area of a key ring. Positive means counter-clockwise. Exact."""
    total = 0
    for (x1, y1), (x2, y2) in zip(ring, [*ring[1:], ring[0]], strict=True):
        total += x1 * y2 - x2 * y1
    return total


class Grid:
    """Maps world coordinates to and from quantised integer keys."""

    def __init__(
        self,
        scale: float,
        translate: Sequence[float],
        topology_tolerance: float,
    ) -> None:
        if scale <= 0:
            raise ValueError("scale must be positive")
        if len(translate) != 3:
            raise ValueError("translate must be (tx, ty, tz)")

        ratio = topology_tolerance / scale
        if abs(ratio - round(ratio)) > 1e-9:
            raise ValueError(
                f"topology_tolerance ({topology_tolerance}) must be an exact multiple "
                f"of scale ({scale})"
            )

        self.scale = float(scale)
        self.translate = (float(translate[0]), float(translate[1]), float(translate[2]))
        self.topology_tolerance = float(topology_tolerance)
        self.step = int(round(ratio))  # integer keys are always multiples of this

    @classmethod
    def from_min_corner(
        cls,
        minx: float,
        miny: float,
        minz: float,
        scale: float,
        topology_tolerance: float,
        pad: float = 1.0,
    ) -> Grid:
        """Build a grid whose translate sits just below the dataset minimum.

        translate must be floored onto the topology grid, otherwise snapping and
        quantisation use grids offset from one another. Padding down from the
        minimum also keeps the integers small: a raw UTM northing at scale 0.001
        overflows int32, which C++ CityJSON readers commonly assume.
        """
        tol = topology_tolerance
        nd = _decimals(tol)
        # Round after scaling back up: floor(v/tol)*tol accumulates float error.
        origin = tuple(round(math.floor((v - pad) / tol) * tol, nd) for v in (minx, miny, minz))
        return cls(scale=scale, translate=origin, topology_tolerance=tol)

    def snap(self, value: float) -> float:
        """Snap a scalar onto the topology grid (absolute, origin at 0).

        Same absolute grid as shapely.set_precision, so geometry that has been
        through set_precision is already on it and snapping again is a no-op.
        """
        tol = self.topology_tolerance
        return _iround(value / tol) * tol

    def snap_xy(self, x: float, y: float) -> tuple[float, float]:
        return self.snap(x), self.snap(y)

    def key_xy(self, x: float, y: float) -> PlanKey:
        """Planimetric key. This is what makes "one z per (x, y)" enforceable."""
        return (
            _iround((self.snap(x) - self.translate[0]) / self.scale),
            _iround((self.snap(y) - self.translate[1]) / self.scale),
        )

    def key_z(self, z: float) -> int:
        return _iround((z - self.translate[2]) / self.scale)

    def key_xyz(self, x: float, y: float, z: float) -> VertexKey:
        ix, iy = self.key_xy(x, y)
        return (ix, iy, self.key_z(z))

    def key_with_z(self, key_xy: PlanKey, z: float) -> VertexKey:
        """Attach a height to an existing planimetric key without re-snapping."""
        return (key_xy[0], key_xy[1], self.key_z(z))

    def to_world(self, key: VertexKey) -> tuple[float, float, float]:
        return (
            key[0] * self.scale + self.translate[0],
            key[1] * self.scale + self.translate[1],
            key[2] * self.scale + self.translate[2],
        )

    def xy_to_world(self, key_xy: PlanKey) -> tuple[float, float]:
        return (
            key_xy[0] * self.scale + self.translate[0],
            key_xy[1] * self.scale + self.translate[1],
        )

    def ring_keys(self, coords: Iterable[Sequence[float]]) -> list[PlanKey] | None:
        """Quantise a ring's coordinates into the canonical key sequence.

        Consecutive duplicates are collapsed, including across the wrap-around.
        Returns None for a ring that cannot be represented on the grid:

          * fewer than three distinct vertices;
          * a repeated non-consecutive vertex (the ring pinches or crosses itself);
          * zero quantised area -- every vertex collinear on the grid.

        This is the honest degeneracy test: a 0.005 m^2 triangle still has
        positive float area but collapses to a segment once quantised, and it is
        the quantised form that gets written and checked.
        """
        keys: list[PlanKey] = []
        for point in coords:
            key = self.key_xy(point[0], point[1])
            if not keys or key != keys[-1]:
                keys.append(key)
        while len(keys) > 1 and keys[0] == keys[-1]:
            keys.pop()
        if len(keys) < 3:
            return None
        if len(set(keys)) != len(keys):
            return None
        if signed_area2(keys) == 0:
            return None
        return keys

    def as_cityjson_transform(self) -> dict[str, list[float]]:
        return {
            "scale": [self.scale, self.scale, self.scale],
            "translate": list(self.translate),
        }


class VertexPool:
    """Insertion-ordered set of quantised vertices, deduplicated exactly by integer key."""

    __slots__ = ("_index", "_keys")

    def __init__(self) -> None:
        self._index: dict[VertexKey, int] = {}
        self._keys: list[VertexKey] = []

    def index_of(self, key: VertexKey) -> int:
        """Insert-or-get. Returns the CityJSON vertex index for this key."""
        existing = self._index.get(key)
        if existing is not None:
            return existing
        idx = len(self._keys)
        self._index[key] = idx
        self._keys.append(key)
        return idx

    def key_at(self, index: int) -> VertexKey:
        return self._keys[index]

    def __len__(self) -> int:
        return len(self._keys)

    def as_int_list(self) -> list[list[int]]:
        """The CityJSON 1.1 "vertices" array."""
        return [list(k) for k in self._keys]

    def bbox_world(self, grid: Grid) -> list[float]:
        """geographicalExtent, in real-world coordinates."""
        if not self._keys:
            return [0.0] * 6
        xs = [k[0] for k in self._keys]
        ys = [k[1] for k in self._keys]
        zs = [k[2] for k in self._keys]
        lo = grid.to_world((min(xs), min(ys), min(zs)))
        hi = grid.to_world((max(xs), max(ys), max(zs)))
        return [lo[0], lo[1], lo[2], hi[0], hi[1], hi[2]]
