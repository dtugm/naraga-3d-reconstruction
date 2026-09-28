"""Watertight LOD2 building generation (the `cascade_mesh` model).

A typed rewrite of sam-interactive-github/ai/lod_generation_v2 -- that repo is a
read-only reference and is never imported. Inputs: building outline (BO), roof
structure (RS), DSM and optional DTM. Output: CityJSON 1.1, one Solid per
building. Conversion to glTF / 3D Tiles is naraga-converter's job, not ours.

Importing this package pulls in geopandas/rasterio/shapely (the `geo` extra).
"""

from .core import LOD2Cancelled, LOD2Error, generate_lod2
from .params import BuildingReport, LOD2Params, QAReport

__all__ = [
    "BuildingReport",
    "LOD2Cancelled",
    "LOD2Error",
    "LOD2Params",
    "QAReport",
    "generate_lod2",
]
