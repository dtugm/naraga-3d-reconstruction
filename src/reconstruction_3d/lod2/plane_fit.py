"""Roof plane fitting: iteratively trimmed least squares, with a fallback ladder.

Deliberately deterministic (RANSAC is stochastic, so identical inputs would give
different output between runs). The RS segmentation has already done the hard
work -- each segment is mostly one plane -- which is exactly where trimming
converges in two or three iterations.

The ladder's last rung is unconditionally available, so plane fitting can never
fail a building -- it only degrades it.
"""

from __future__ import annotations

import logging
import math

import numpy as np
from shapely.errors import GEOSException
from shapely.geometry import MultiPolygon, Polygon

from .params import LOD2Params, RoofPlane
from .raster import FloatArray, RasterView

log = logging.getLogger(__name__)


def erode(poly: Polygon, distance: float) -> Polygon | None:
    """Shrink a face so its samples avoid the mixed pixels at its edge.

    A pixel straddling an eave sees half roof and half ground; eroding by one GSD
    removes the largest single source of plane-fit error. None means the face is
    too narrow to fit at all.
    """
    if distance <= 0:
        return poly
    try:
        shrunk = poly.buffer(-distance)
    except GEOSException:
        return None
    if shrunk.is_empty:
        return None
    if isinstance(shrunk, MultiPolygon):
        shrunk = max(shrunk.geoms, key=lambda g: float(g.area))
    return shrunk if isinstance(shrunk, Polygon) and not shrunk.is_empty else None


def fit_plane(
    xs: FloatArray, ys: FloatArray, zs: FloatArray, params: LOD2Params
) -> RoofPlane | None:
    """Least-squares plane through the samples, with outliers trimmed out.

    Returns None if the data cannot support a plane; the caller then walks the
    fallback ladder.
    """
    n = int(zs.size)
    if n < max(params.min_plane_pixels, 3):
        return None

    # Centring is mandatory: raw UTM coordinates give a normal matrix with a
    # condition number near 1e13, where the float64 solution is meaningless.
    x0 = float(xs.mean())
    y0 = float(ys.mean())
    cx = xs - x0
    cy = ys - y0

    keep = np.ones(n, dtype=bool)
    a = b = c = 0.0

    for iteration in range(params.trim_iterations + 1):
        design = np.column_stack([cx[keep], cy[keep], np.ones(int(keep.sum()), dtype=np.float64)])
        try:
            solution = np.linalg.lstsq(design, zs[keep], rcond=None)[0]
        except np.linalg.LinAlgError:
            return None
        a, b, c = (float(v) for v in solution)

        if iteration == params.trim_iterations:
            break

        residual = zs - (a * cx + b * cy + c)
        centre = float(np.median(residual[keep]))
        # MAD -> robust sigma, so the rejection threshold comes from the data.
        sigma = 1.4826 * float(np.median(np.abs(residual[keep] - centre)))
        if sigma < 1e-6:
            break

        candidate = np.abs(residual - centre) <= params.trim_sigma * sigma
        if int(candidate.sum()) < max(params.min_plane_pixels, 3):
            break
        if np.array_equal(candidate, keep):
            break
        keep = candidate

    residual = zs[keep] - (a * cx[keep] + b * cy[keep] + c)
    rmse = float(np.sqrt(np.mean(residual * residual))) if residual.size else 0.0
    n_inliers = int(keep.sum())

    if n_inliers / n < params.min_inlier_fraction:
        return None
    if not math.isfinite(rmse) or rmse > params.max_plane_rmse:
        return None

    plane = RoofPlane(a=a, b=b, c=c, x0=x0, y0=y0, rmse=rmse, n_inliers=n_inliers, source="fitted")
    if plane.slope_deg > params.max_plane_slope_deg:
        return None
    return plane


def plane_for_face(
    poly: Polygon,
    dsm: RasterView,
    params: LOD2Params,
    erode_distance: float,
    building_z: float | None,
    ground_z: float,
) -> RoofPlane:
    """Best plane available for one roof face, walking down the ladder.

    fitted -> horizontal at the face median -> horizontal at the building's
    75th percentile -> ground plus the minimum building height.
    """
    shrunk = erode(poly, erode_distance)
    if shrunk is not None:
        xs, ys, zs = dsm.sample_polygon(shrunk)
        plane = fit_plane(xs, ys, zs, params)
        if plane is not None:
            return plane

    # Anything narrower than about four pixels has no samples left after erosion,
    # so small dormers become flat-topped boxes: a limit of the data.
    _, _, zs = dsm.sample_polygon(poly, all_touched=True)
    if zs.size:
        return RoofPlane.horizontal(
            float(np.median(zs)), "horizontal_median", n_inliers=int(zs.size)
        )

    if building_z is not None:
        return RoofPlane.horizontal(building_z, "horizontal_building")

    return RoofPlane.horizontal(ground_z + params.min_building_height, "fallback_flat")


def building_reference_z(footprint: Polygon, dsm: RasterView) -> float | None:
    """A single representative roof height for a whole footprint.

    The 75th percentile rather than the median: a footprint usually contains
    some ground or vegetation pixels around the eaves.
    """
    return dsm.statistic(footprint, "p75", all_touched=True)
