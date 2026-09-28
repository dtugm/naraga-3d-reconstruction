"""Ear clipping on quantised integer coordinates.

Two properties make this worth writing instead of a dependency:

  * It introduces NO new vertices, and the triangulation's boundary edges are
    exactly the input ring's edges -- which keeps the roof welded to the walls.
  * On integer keys the orientation and point-in-triangle predicates are EXACT.

Collinear vertices are the subtle part. Noding inserts a vertex wherever a roof
segment edge meets a straight footprint edge, and the wall uses it too; dropping
it from the roof would leave a T-junction hole. So the ear search demands strict
convexity first and only relaxes if it stalls completely.
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

    Returns triangles wound the same way as the input ring, or None if the ring
    could not be triangulated. O(n^2); roof faces carry 4-20 vertices.
    """
    n = len(ring)
    if n < 3:
        return None
    if n == 3:
        return [(ring[0], ring[1], ring[2])]

    ccw = signed_area2(ring) > 0
    work: list[PlanKey] = list(ring) if ccw else list(reversed(ring))

    remaining = list(range(len(work)))
    triangles: list[Triangle] = []

    # Each clip removes one vertex, so n-3 clips finish the job; the extra
    # allowance covers passes that only relax.
    for _ in range(2 * n):
        if len(remaining) <= 3:
            break

        clipped_at = -1
        for relaxed in (False, True):
            for position in range(len(remaining)):
                count = len(remaining)
                ia = remaining[(position - 1) % count]
                ib = remaining[position]
                ic = remaining[(position + 1) % count]
                a, b, c = work[ia], work[ib], work[ic]

                turn = _cross(a, b, c)
                if turn < 0 or (turn == 0 and not relaxed):
                    continue  # reflex, or collinear while still being strict

                blocked = any(
                    _inside_triangle(work[other], a, b, c)
                    for other in remaining
                    if other not in (ia, ib, ic)
                )
                if blocked:
                    continue

                # A collinear ear is emitted even with zero plan area: skipping it
                # would delete vertex b, which the wall ring still uses.
                triangles.append((a, b, c))
                clipped_at = position
                break
            if clipped_at >= 0:
                break

        if clipped_at < 0:
            return None
        remaining.pop(clipped_at)

    if len(remaining) == 3:
        a, b, c = (work[i] for i in remaining)
        triangles.append((a, b, c))
    elif len(remaining) > 3:
        return None  # ran out of passes without finishing

    if not triangles:
        return None

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
