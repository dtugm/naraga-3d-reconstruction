"""Assemble roof, ground and wall faces into one correctly-oriented CityJSON Solid.

ORIENTATION
-----------
CityJSON follows the right-hand rule: every exterior-shell face is wound so its
normal points out of the solid. Everything here derives from one invariant,
established in io_vector._prepare_polygons and on each partitioned face:

    orient(polygon, 1.0)  ->  exterior ring CCW, interior rings CW

which makes "the building material is on the LEFT of travel" true for every
ring, exterior and interior alike. So for the ring A -> B, the quad

    [A_top, A_bot, B_bot, B_top]

has normal (A_bot - A_top) x (B_bot - A_top) = (h*dy, -h*dx, 0), which points to
the RIGHT of travel -- away from the material. Outward, in both cases, with no
per-ring flip. validate.check_shell proves the result rather than trusting this.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

from .grid import Grid, PlanKey, VertexPool
from .params import Face, LOD2Params
from .triangulate import triangulate_rings

log = logging.getLogger(__name__)

# Indices into the CityJSON semantic surface list; see cityjson.SURFACES.
ROOF_SURFACE = 0
GROUND_SURFACE = 1
WALL_SURFACE = 2

# boundaries[shell][face][ring][vertex]
Ring = list[int]
CityFace = list[Ring]


class SolidError(RuntimeError):
    """The building could not be assembled into a shell at all."""


def _outline_rings(
    outline: BaseGeometry | None, grid: Grid
) -> tuple[list[PlanKey], list[list[PlanKey]]]:
    """Exterior and interior key rings of the roof outline.

    A MultiPolygon outline cannot be one Solid; the largest part is kept and the
    rest reported, rather than emitting a shell that silently omits area.
    """
    if outline is None:
        raise SolidError("no roof outline")
    if isinstance(outline, MultiPolygon):
        parts = sorted(outline.geoms, key=lambda g: float(g.area), reverse=True)
        dropped = sum(float(g.area) for g in parts[1:])
        log.warning(
            "roof outline is disconnected; keeping the largest of %d parts and dropping %.2f m2",
            len(parts),
            dropped,
        )
        outline = parts[0]
    if not isinstance(outline, Polygon):
        raise SolidError(f"unusable roof outline of type {outline.geom_type}")

    exterior = grid.ring_keys(outline.exterior.coords)
    if exterior is None:
        raise SolidError("roof outline collapsed under quantisation")

    interiors: list[list[PlanKey]] = []
    for ring in outline.interiors:
        keys = grid.ring_keys(ring.coords)
        if keys is not None:
            interiors.append(keys)
    return exterior, interiors


def _wall_quads(
    ring: Sequence[PlanKey],
    z_top: dict[PlanKey, float],
    z_ground: float,
    grid: Grid,
    pool: VertexPool,
) -> list[CityFace]:
    """Vertical quads from the ground ring up to the roof ring.

    Planar by construction (both bottom vertices share z_ground, all four points
    lie in the vertical plane through A and B), so walls are never triangulated.
    """
    quads: list[CityFace] = []
    count = len(ring)
    for i in range(count):
        key_a = ring[i]
        key_b = ring[(i + 1) % count]
        top_a = pool.index_of(grid.key_with_z(key_a, z_top[key_a]))
        top_b = pool.index_of(grid.key_with_z(key_b, z_top[key_b]))
        bot_a = pool.index_of(grid.key_with_z(key_a, z_ground))
        bot_b = pool.index_of(grid.key_with_z(key_b, z_ground))
        # A zero-height wall is still emitted: skipping it would leave the roof
        # and ground edges unpaired. The ground clamp in core prevents it.
        quads.append([[top_a, bot_a, bot_b, top_b]])
    return quads


def build_solid(
    faces: Sequence[Face],
    outline: BaseGeometry | None,
    z_top: dict[PlanKey, float],
    z_ground: float,
    grid: Grid,
    pool: VertexPool,
    params: LOD2Params,
) -> tuple[list[CityFace], list[int]]:
    """Build the exterior shell (boundaries[0]) and its per-face semantic indices."""
    exterior, interiors = _outline_rings(outline, grid)

    missing = [k for k in exterior if k not in z_top]
    for ring in interiors:
        missing.extend(k for k in ring if k not in z_top)
    if missing:
        raise SolidError(
            f"{len(missing)} outline vertices have no reconciled height; the "
            "outline carries vertices the roof faces do not"
        )

    shell: list[CityFace] = []
    semantics: list[int] = []

    # --- roof -------------------------------------------------------------
    for face in faces:
        face_exterior = grid.ring_keys(face.polygon.exterior.coords)
        if face_exterior is None:
            continue
        face_interiors = [
            keys
            for keys in (grid.ring_keys(r.coords) for r in face.polygon.interiors)
            if keys is not None
        ]

        triangles = (
            triangulate_rings(face_exterior, face_interiors) if params.triangulate_roof else None
        )

        if triangles:
            for tri in triangles:
                shell.append([[pool.index_of(grid.key_with_z(k, z_top[k])) for k in tri]])
                semantics.append(ROOF_SURFACE)
        else:
            rings = [face_exterior, *face_interiors]
            shell.append(
                [[pool.index_of(grid.key_with_z(k, z_top[k])) for k in ring] for ring in rings]
            )
            semantics.append(ROOF_SURFACE)

    if not shell:
        raise SolidError("no roof faces survived")

    # --- ground -----------------------------------------------------------
    # Reversed so the normal points down; interior rings flip too.
    ground: CityFace = [[pool.index_of(grid.key_with_z(k, z_ground)) for k in reversed(exterior)]]
    for ring in interiors:
        ground.append([pool.index_of(grid.key_with_z(k, z_ground)) for k in reversed(ring)])
    shell.append(ground)
    semantics.append(GROUND_SURFACE)

    # --- walls ------------------------------------------------------------
    # Courtyard walls belong to the EXTERIOR shell. An inner CityJSON shell means
    # an enclosed cavity (a bubble); a courtyard is a through-hole, so the result
    # is a valid genus-1 closed 2-manifold.
    for ring in [exterior, *interiors]:
        for quad in _wall_quads(ring, z_top, z_ground, grid, pool):
            shell.append(quad)
            semantics.append(WALL_SURFACE)

    return shell, semantics
