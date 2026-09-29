"""Vector input -- read only, entirely in memory.

The single most important property of this module is what it does *not* do:
it never writes to the input files (lod_generation v1 rewrote BO and RS in place).

Ported from lod_generation_v2/_io_vector.py without the Cascade3D 3D-Viewer
contract (forced level_0/level_1/Id attributes, filename-safe ids): this service
hands CityJSON to naraga-converter, not to that viewer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any
from uuid import uuid4

import geopandas as gpd
from pyproj import CRS
from shapely import make_valid, set_precision
from shapely.errors import GEOSException
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient

from .params import LOD2Params

log = logging.getLogger(__name__)


def _unique_id(raw: object, used: set[str]) -> str:
    """A unique, non-empty string id; CityObject ids must not collide.

    Suffixes are checked against every id already handed out, not just against
    repeats of the same source value: with source ids `a_1, a, a` a per-value counter
    would give the second `a` the id `a_1` again.
    """
    text = "" if raw is None else str(raw).strip()
    if text in ("", "None", "nan"):
        text = uuid4().hex
    candidate, n = text, 0
    while candidate in used:
        n += 1
        candidate = f"{text}_{n}"
    used.add(candidate)
    return candidate


def _explode_to_singlepart(gdf: Any) -> Any:
    """Explode multipart geometries; level_0/level_1 record source feature and part."""
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    gdf = gdf.drop(columns=[c for c in ("level_0", "level_1") if c in gdf.columns])
    gdf = gdf.reset_index(drop=True)

    exploded = gdf.explode(index_parts=True)
    exploded.index = exploded.index.set_names(["level_0", "level_1"])
    return exploded.reset_index()


def snap_geometry(geom: BaseGeometry, tolerance: float) -> BaseGeometry | None:
    """set_precision with a repair-and-retry ladder.

    Real data breaks the plain call (GEOS "unable to assign free hole to a shell"
    on already-invalid outlines), so repair first and retry. Returns None when
    every rung fails, so one pathological feature costs one feature, not the run.
    """
    repairs: list[Callable[[BaseGeometry], BaseGeometry] | None] = [
        None,
        make_valid,
        lambda g: g.buffer(0),
    ]
    for repair in repairs:
        try:
            candidate = geom if repair is None else repair(geom)
            if candidate is None or candidate.is_empty:
                continue
            snapped: BaseGeometry = set_precision(candidate, tolerance)
            return snapped
        except (GEOSException, ValueError):
            continue
    return None


def _prepare_polygons(
    gdf: Any,
    tolerance: float,
    min_area: float = 0.0,
    label: str = "layer",
) -> Any:
    """Snap to the topology grid, repair, keep oriented single polygons.

    set_precision also repairs: it collapses sub-tolerance edges and removes the
    near-coincident vertex configurations that make overlay operations emit
    spurious slivers, which is what makes the noded partition tractable.
    """
    if gdf.empty:
        return gdf

    area_before = float(gdf.geometry.area.sum())

    # Repair before snapping: set_precision needs a well-formed input to collapse.
    invalid = ~gdf.geometry.is_valid
    if invalid.any():
        log.info("%s: repairing %d invalid geometries", label, int(invalid.sum()))
        gdf = gdf.assign(
            geometry=[
                g if v else make_valid(g) for g, v in zip(gdf.geometry, ~invalid, strict=True)
            ]
        )

    snapped = [snap_geometry(g, tolerance) for g in gdf.geometry]
    n_failed = sum(1 for g in snapped if g is None)
    if n_failed:
        log.warning(
            "%s: %d geometries could not be snapped to %.3f m and were dropped",
            label,
            n_failed,
            tolerance,
        )
    gdf = gdf.assign(geometry=snapped)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]

    # make_valid and set_precision can both demote a Polygon to a collection.
    gdf = gdf.explode(index_parts=False)
    gdf = gdf[gdf.geometry.geom_type == "Polygon"]
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]

    if min_area > 0 and not gdf.empty:
        gdf = gdf[gdf.geometry.area >= min_area]

    if not gdf.empty:
        # One invariant for the whole pipeline: exterior CCW, interiors CW.
        gdf = gdf.assign(geometry=[orient(g, 1.0) for g in gdf.geometry])

    area_after = float(gdf.geometry.area.sum()) if not gdf.empty else 0.0
    if area_before > 0:
        lost = (area_before - area_after) / area_before
        if lost > 0.001:
            log.warning(
                "%s: %.2f%% of area lost to precision snapping at %.3f m",
                label,
                lost * 100.0,
                tolerance,
            )

    return gdf.reset_index(drop=True)


def load_buildings(params: LOD2Params) -> Any:
    """Load building outlines: single polygons, snapped, oriented, with a unique id."""
    gdf = gpd.read_file(params.input_building)
    if gdf.empty:
        raise ValueError("building outline has no features")
    if gdf.crs is None:
        raise ValueError("building outline has no CRS")

    gdf = _explode_to_singlepart(gdf)
    gdf = _prepare_polygons(gdf, params.topology_tolerance, min_area=0.0, label="building outlines")
    if gdf.empty:
        raise ValueError(
            "every building outline collapsed during snapping at "
            f"topology_tolerance={params.topology_tolerance}"
        )

    field = params.id_field
    used: set[str] = set()
    if field in gdf.columns:
        # Preserve ids already assigned; only fill the blanks.
        gdf[field] = [_unique_id(v, used) for v in gdf[field]]
    else:
        gdf[field] = [_unique_id(None, used) for _ in range(len(gdf))]

    log.info("loaded %d building outlines", len(gdf))
    return gdf


def load_roofs(params: LOD2Params) -> Any:
    """Load roof structure segments, ready for clipping against the outlines."""
    gdf = gpd.read_file(params.input_roof)
    if gdf.crs is None:
        raise ValueError("roof structure has no CRS")

    gdf = _explode_to_singlepart(gdf)
    gdf = _prepare_polygons(
        gdf,
        params.topology_tolerance,
        min_area=params.min_segment_area,
        label="roof segments",
    )
    log.info("loaded %d roof segments", len(gdf))
    return gdf


def assign_roofs_to_buildings(buildings: Any, roofs: Any) -> dict[int, list[int]]:
    """Map each building's positional index to the roof segments touching it.

    gpd.sjoin goes through an STRtree instead of v1's O(n*m) cross product. Not
    winner-take-all: each segment is later clipped to each footprint, so a
    segment straddling two buildings legitimately contributes to both.
    """
    if roofs.empty or buildings.empty:
        return {}

    left = roofs[["geometry"]].reset_index(drop=True)
    right = buildings[["geometry"]].reset_index(drop=True)

    pairs = gpd.sjoin(left, right, how="inner", predicate="intersects")

    mapping: dict[int, list[int]] = {}
    for roof_idx, building_idx in zip(pairs.index, pairs["index_right"], strict=True):
        mapping.setdefault(int(building_idx), []).append(int(roof_idx))

    n_pairs = sum(len(v) for v in mapping.values())
    log.info(
        "%d of %d buildings have roof segments (%d candidate pairs)",
        len(mapping),
        len(buildings),
        n_pairs,
    )
    return mapping


def check_crs_agreement(buildings: Any, roofs: Any, rasters: Mapping[str, int | None]) -> int:
    """Fail loudly on unusable or mixed reference systems, before any expensive work.

    `rasters` maps each raster that is present ("DSM", and "DTM" when given) to its
    EPSG code. Every input must have one: an input with no CRS, or one GDAL cannot
    match to an EPSG code, would otherwise be silently assumed to agree. The shared
    CRS must also be projected, because every tolerance in LOD2Params is in metres
    (0.01 m snapping, 1 m² segments, 2 m minimum height).
    """
    found: dict[str, int | None] = {"building outline": buildings.crs.to_epsg()}
    if not roofs.empty:
        found["roof structure"] = roofs.crs.to_epsg()
    found.update(rasters)

    missing = [name for name, code in found.items() if code is None]
    if missing:
        raise ValueError(
            f"no EPSG code for: {', '.join(missing)}; every input needs a CRS "
            "that maps to an EPSG code"
        )
    distinct = set(found.values())
    if len(distinct) > 1:
        detail = ", ".join(f"{k}=EPSG:{v}" for k, v in found.items())
        raise ValueError(f"inputs are in different reference systems: {detail}")

    epsg = distinct.pop()
    assert epsg is not None  # `missing` above already rejected None
    if CRS.from_epsg(epsg).is_geographic:
        raise ValueError(
            f"EPSG:{epsg} is geographic (degrees), but LOD2 tolerances are in metres; "
            "reproject every input to a projected CRS such as UTM"
        )
    return epsg
