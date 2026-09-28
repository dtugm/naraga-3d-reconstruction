"""Noded planar subdivision of one building footprint by its roof segments.

The riskiest component in the pipeline. It answers which piece of roof covers
which piece of footprint, in a way that makes the *vertices* line up, not just
the areas.

Construction: node all the linework together, polygonize it, classify the
faces. Noding is the part that matters -- where a segment edge ends in the
middle of a footprint edge, plain clipping leaves that T-junction invisible to
the footprint ring and the mesh gets a hole exactly there.

One construction delivers: a guaranteed disjoint cover, gaps surfaced as faces
nothing claims, full noding (the precondition for shared-vertex z
reconciliation), and the densified footprint ring the walls need.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence

from shapely.errors import GEOSException
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.ops import polygonize, unary_union

from .grid import Grid
from .io_vector import snap_geometry
from .params import Face, LOD2Params

log = logging.getLogger(__name__)

# How much of the footprint may go missing before the partition is rejected and
# the fallback method is tried. Snapping means exact equality never holds.
COVER_TOLERANCE = 1e-4


def as_polygons(geom: BaseGeometry | None) -> list[Polygon]:
    """Flatten any geometry down to its non-empty polygonal parts."""
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return [g for g in geom.geoms if not g.is_empty]
    if hasattr(geom, "geoms"):  # GeometryCollection
        out: list[Polygon] = []
        for part in geom.geoms:
            out.extend(as_polygons(part))
        return out
    return []


def _thinness(poly: Polygon) -> float:
    """Polsby-Popper compactness, 4*pi*A/P^2. A circle is 1, a sliver ~0."""
    perimeter = float(poly.length)
    if perimeter <= 0:
        return 0.0
    return 4.0 * math.pi * float(poly.area) / (perimeter * perimeter)


def clip_segments(
    footprint: Polygon, segments: Sequence[Polygon], params: LOD2Params
) -> list[Polygon]:
    """Clip roof segments to the footprint, dropping what is left of the rest."""
    clipped: list[Polygon] = []
    for seg in segments:
        piece: BaseGeometry | None
        try:
            piece = seg.intersection(footprint)
        except GEOSException:
            piece = snap_geometry(seg, params.topology_tolerance)
            if piece is None:
                continue
            try:
                piece = piece.intersection(footprint)
            except GEOSException:
                continue
        for part in as_polygons(piece):
            if part.area < params.min_segment_area:
                continue
            snapped = snap_geometry(part, params.topology_tolerance)
            for final in as_polygons(snapped):
                if final.area >= params.min_segment_area and final.is_valid:
                    clipped.append(final)
    return clipped


def _partition_polygonize(footprint: Polygon, clipped: Sequence[Polygon]) -> list[Polygon]:
    """Node every boundary together, then rebuild faces from the edge graph."""
    lines = [footprint.boundary] + [seg.boundary for seg in clipped]
    noded = unary_union(lines)  # this is the noding step
    return [
        face
        for face in polygonize(noded)
        if not face.is_empty and face.area > 0 and footprint.contains(face.representative_point())
    ]


def _partition_difference(footprint: Polygon, clipped: Sequence[Polygon]) -> list[Polygon]:
    """Escape hatch: subtract segments in descending area order.

    Produces a disjoint cover but does NOT node, so T-junction vertices are
    missing and the mesh may not close. Only reached when polygonize fails.
    """
    faces: list[Polygon] = []
    accumulated: BaseGeometry | None = None

    for seg in sorted(clipped, key=lambda s: float(s.area), reverse=True):
        piece = seg if accumulated is None else seg.difference(accumulated)
        faces.extend(as_polygons(piece))
        accumulated = seg if accumulated is None else unary_union([accumulated, seg])

    if accumulated is not None:
        faces.extend(as_polygons(footprint.difference(accumulated)))
    else:
        faces.append(footprint)
    return faces


def _classify(faces: Sequence[Polygon], clipped: Sequence[Polygon]) -> list[Face]:
    """Attach each face to the segment covering it, or mark it a gap."""
    out: list[Face] = []
    for face in faces:
        point = face.representative_point()
        index: int | None = None
        for i, seg in enumerate(clipped):
            if seg.contains(point):
                index = i
                break
        out.append(Face(polygon=orient(face, 1.0), segment_index=index))
    return out


def _drop_degenerate(faces: list[Face], grid: Grid) -> tuple[list[Face], float, int]:
    """Discard faces that cannot survive quantisation.

    polygonize emits micro-slivers wherever linework is nearly collinear. They
    are smaller than the output grid, so dropping them loses nothing
    representable, and the edges they contributed duplicate edges their
    neighbours already share.
    """
    kept: list[Face] = []
    lost_area = 0.0
    dropped = 0
    for face in faces:
        if grid.ring_keys(face.polygon.exterior.coords) is None:
            lost_area += float(face.polygon.area)
            dropped += 1
            continue
        kept.append(face)
    return kept, lost_area, dropped


def _absorb_slivers(faces: list[Face], params: LOD2Params) -> list[Face]:
    """Let tiny or thread-like gaps inherit a real neighbour's plane.

    The face stays in the mesh as its own face -- only its plane assignment
    changes. Merging geometry instead would require re-noding everything.
    """
    for _ in range(3):  # a gap may sit next to another gap
        changed = False
        for face in faces:
            if not face.is_gap or face.absorbed:
                continue
            poly = face.polygon
            if not (
                poly.area < params.sliver_absorb_area or _thinness(poly) < params.sliver_thinness
            ):
                continue

            best_index: int | None = None
            best_shared = 0.0
            for other in faces:
                if other is face or other.segment_index is None:
                    continue
                try:
                    shared = float(poly.intersection(other.polygon).length)
                except GEOSException:
                    continue
                if shared > best_shared:
                    best_shared = shared
                    best_index = other.segment_index

            if best_index is not None:
                face.segment_index = best_index
                face.absorbed = True
                changed = True
        if not changed:
            break
    return faces


def _cover_error(faces: Sequence[Face], footprint: Polygon) -> float:
    """Relative area the faces fail to account for."""
    area = float(footprint.area)
    if area <= 0:
        return 0.0
    total = sum(float(f.polygon.area) for f in faces)
    return abs(total - area) / area


def partition_footprint(
    footprint: Polygon,
    segments: Sequence[Polygon],
    params: LOD2Params,
    grid: Grid,
    method: str = "polygonize",
) -> tuple[list[Face], str]:
    """Subdivide `footprint` into disjoint faces covering it exactly.

    Returns the faces and the method that actually produced them: polygonize
    falls back to difference when it loses area, and both fall back to a single
    whole-footprint face.
    """
    clipped = clip_segments(footprint, segments, params)

    if not clipped:
        # No roof information at all. Still flows through the identical
        # watertight machinery; it just ends up as an LOD1-shaped box.
        return [Face(polygon=orient(footprint, 1.0), segment_index=None)], "no_segments"

    order = ["polygonize", "difference"] if method == "polygonize" else ["difference"]
    problems: list[str] = []

    for attempt in order:
        builder: Callable[[Polygon, Sequence[Polygon]], list[Polygon]] = (
            _partition_polygonize if attempt == "polygonize" else _partition_difference
        )
        try:
            raw = builder(footprint, clipped)
        except GEOSException as exc:
            problems.append(f"{attempt}: {exc}")
            continue

        if not raw:
            problems.append(f"{attempt}: produced no faces")
            continue
        if len(raw) > params.max_faces_per_building:
            problems.append(f"{attempt}: {len(raw)} faces exceeds the cap")
            continue

        faces, lost, dropped = _drop_degenerate(_classify(raw, clipped), grid)
        if not faces:
            problems.append(f"{attempt}: every face collapsed under quantisation")
            continue
        if dropped:
            log.debug("%s: dropped %d sub-grid faces totalling %.4f m2", attempt, dropped, lost)

        faces = _absorb_slivers(faces, params)
        # Dropped slivers count against the cover: a partition that shattered
        # into slivers fails here and falls through to the next method.
        error = _cover_error(faces, footprint)
        if error > COVER_TOLERANCE:
            problems.append(f"{attempt}: {error * 100:.3f}% of the footprint uncovered")
            continue

        return faces, attempt

    log.warning("partition fell back to a single flat face (%s)", "; ".join(problems))
    return [Face(polygon=orient(footprint, 1.0), segment_index=None)], "fallback"


def roof_union(faces: Sequence[Face]) -> BaseGeometry | None:
    """Outline of the face set -- the densified ring the walls extrude from.

    Because the partition was built by noding the footprint boundary together
    with every segment boundary, this outline already carries a vertex wherever
    a segment edge meets it.
    """
    if not faces:
        return None
    polygons = as_polygons(unary_union([f.polygon for f in faces]))
    if not polygons:
        return None
    if len(polygons) == 1:
        single: BaseGeometry = orient(polygons[0], 1.0)
        return single
    return MultiPolygon([orient(p, 1.0) for p in polygons])
