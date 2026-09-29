"""Ear clipping on quantised integer coordinates.

Two properties make this worth writing instead of a dependency:

  * It introduces NO new vertices, and the triangulation's boundary edges are
    exactly the input ring's edges -- which keeps the roof welded to the walls.
  * On integer keys the orientation and point-in-triangle predicates are EXACT.

Collinear vertices are the subtle part. Noding inserts a vertex wherever a roof
segment edge meets a straight footprint edge, and the wall uses it too; dropping
it from the roof would leave a T-junction hole. So only strictly convex ears are
clipped: a collinear vertex usually becomes clippable once its neighbours change.
If the search stalls instead, the ring is not triangulated at all (None) and the
face is written as one polygon that still carries every vertex. That replaces the
reference's "relaxed" pass, which clipped collinear ears and so emitted zero-area
triangles -- degenerate faces that val3dity/cjio flag and that the purely
topological half-edge check cannot see.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from .grid import PlanKey, signed_area2

log = logging.getLogger(__name__)

Triangle = tuple[PlanKey, PlanKey, PlanKey]


def _cross(o: PlanKey, a: PlanKey, b: PlanKey) -> int:
    """Twice the signed area of triangle o-a-b. Exact: all inputs are ints."""
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _inside_triangle(p: PlanKey, a: PlanKey, b: PlanKey, c: PlanKey) -> bool:
    """True if p is inside or on the boundary of the CCW triangle a-b-c."""
    return _cross(a, b, p) >= 0 and _cross(b, c, p) >= 0 and _cross(c, a, p) >= 0


def ear_clip(ring: Sequence[PlanKey]) -> list[Triangle] | None:
    """Triangulate a simple, hole-free ring given in any orientation.

    Returns triangles wound the same way as the input ring, each with a strictly
    positive plan area, or None if the ring cannot be triangulated that way (the
    caller then writes the face as a polygon). O(n^2); roof faces carry 4-20 vertices.
    """
    n = len(ring)
    if n < 3 or signed_area2(ring) == 0:
        return None
    if n == 3:
        return [(ring[0], ring[1], ring[2])]

    ccw = signed_area2(ring) > 0
    work: list[PlanKey] = list(ring) if ccw else list(reversed(ring))

    remaining = list(range(len(work)))
    triangles: list[Triangle] = []

    # Each clip removes one vertex, so n - 3 clips finish the job.
    while len(remaining) > 3:
        clipped_at = -1
        for position in range(len(remaining)):
            count = len(remaining)
            ia = remaining[(position - 1) % count]
            ib = remaining[position]
            ic = remaining[(position + 1) % count]
            a, b, c = work[ia], work[ib], work[ic]

            if _cross(a, b, c) <= 0:
                continue  # reflex, or collinear: clipping it would make a zero-area face

            blocked = any(
                _inside_triangle(work[other], a, b, c)
                for other in remaining
                if other not in (ia, ib, ic)
            )
            if blocked:
                continue

            triangles.append((a, b, c))
            clipped_at = position
            break

        if clipped_at < 0:
            return None  # stalled on collinear vertices: keep the face as a polygon
        remaining.pop(clipped_at)

    a, b, c = (work[i] for i in remaining)
    if _cross(a, b, c) == 0:
        return None
    triangles.append((a, b, c))

    if not ccw:
        triangles = [(t[2], t[1], t[0]) for t in triangles]
    return triangles


def triangulate_rings(
    exterior: Sequence[PlanKey],
    interiors: Sequence[Sequence[PlanKey]] = (),
) -> list[Triangle] | None:
    """Triangulate a face, or return None to say "emit it as a polygon".

    Faces with holes are not handled; they should be vanishingly rare because the
    partition nodes courtyard rings too. A polygon face stays watertight -- it
    only forgoes the planarity guarantee.
    """
    if interiors:
        return None
    return ear_clip(exterior)
