"""Synthetic gable-roof inputs with closed-form answers, for the LOD2 tests.

Ported from lod_generation_v2/__main__.py's --selftest: one 20 x 10 m building
with a symmetric gable (eaves 10 m, ridge 15 m) on flat ground, split by two RS
segments along the ridge. GeoJSON is written by hand (not through GDAL) and
carries the legacy top-level "crs" member, which GDAL reads to recover a
projected CRS -- without it the file would be interpreted as lon/lat.
"""

from __future__ import annotations

import json
from pathlib import Path

EAVE_Z = 10.0
RIDGE_Z = 15.0
WIDTH = 10.0  # across the ridge
LENGTH = 20.0  # along the ridge
ORIGIN = (699000.0, 9309000.0)  # realistic UTM 48S, so the int32 range is exercised
EPSG = 32748

# Closed form: a box up to the eaves plus a triangular prism above.
EXPECTED_VOLUME = LENGTH * WIDTH * EAVE_Z + 0.5 * WIDTH * (RIDGE_Z - EAVE_Z) * LENGTH

Point = tuple[float, float]


def _rectangle(ya: float, yb: float) -> list[Point]:
    x0, y0 = ORIGIN
    return [
        (x0, y0 + ya),
        (x0 + LENGTH, y0 + ya),
        (x0 + LENGTH, y0 + yb),
        (x0, y0 + yb),
        (x0, y0 + ya),
    ]


def _write_geojson(path: Path, rings: list[list[Point]]) -> None:
    document = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": f"urn:ogc:def:crs:EPSG::{EPSG}"}},
        "features": [
            {
                "type": "Feature",
                "properties": {"Id": index + 1},
                "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in ring]]},
            }
            for index, ring in enumerate(rings)
        ],
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def write_gable_inputs(directory: Path) -> dict[str, Path]:
    """Write bo.geojson, rs.geojson, dsm.tif and dtm.tif; return their paths by role."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    x0, y0 = ORIGIN
    paths = {
        "bo": directory / "bo.geojson",
        "rs": directory / "rs.geojson",
        "dsm": directory / "dsm.tif",
        "dtm": directory / "dtm.tif",
    }
    _write_geojson(paths["bo"], [_rectangle(0.0, WIDTH)])
    _write_geojson(paths["rs"], [_rectangle(0.0, WIDTH / 2), _rectangle(WIDTH / 2, WIDTH)])

    gsd = 0.25
    pad = 2.0
    width = int((LENGTH + 2 * pad) / gsd)
    height = int((WIDTH + 2 * pad) / gsd)
    transform = from_origin(x0 - pad, y0 + WIDTH + pad, gsd, gsd)

    xs = (x0 - pad) + (np.arange(width) + 0.5) * gsd
    ys = (y0 + WIDTH + pad) - (np.arange(height) + 0.5) * gsd
    gx, gy = np.meshgrid(xs, ys)

    # Exact gable: linear in the distance from the nearer eave.
    local_y = gy - y0
    slope = (RIDGE_Z - EAVE_Z) / (WIDTH / 2)
    dsm = np.where(
        local_y <= WIDTH / 2, EAVE_Z + slope * local_y, EAVE_Z + slope * (WIDTH - local_y)
    )
    inside = (gx >= x0) & (gx <= x0 + LENGTH) & (local_y >= 0) & (local_y <= WIDTH)
    dsm = np.where(inside, dsm, 0.0).astype("float32")
    dtm = np.zeros_like(dsm)

    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": f"EPSG:{EPSG}",
        "transform": transform,
    }
    for key, array in (("dsm", dsm), ("dtm", dtm)):
        with rasterio.open(paths[key], "w", **profile) as dataset:
            dataset.write(array, 1)
    return paths
