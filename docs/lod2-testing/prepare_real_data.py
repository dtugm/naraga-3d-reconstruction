"""Turn a folder of real BO/RS/DSM/DTM files into the layout mock_world.py serves.

cascade_mesh over HTTP only accepts GeoJSON vectors (shapefile is not supported yet), so
the two shapefiles are converted; the rasters are copied unchanged. Sources are only read.

usage (from the repo root):
  uv run python docs/lod2-testing/prepare_real_data.py <src_dir> <out_dir> \
      [--bo "Building Outline.shp"] [--rs "Roof Structure.shp"] \
      [--dsm DSM.tif] [--dtm DTM.tif]

Writes <out_dir>/{bo.geojson, rs.geojson, dsm.tif, dtm.tif} and prints a sanity summary.
Exits 1 when the inputs cannot work together (mixed CRS, empty layers, ...).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import geopandas as gpd
import rasterio


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--bo", default="Building Outline.shp")
    ap.add_argument("--rs", default="Roof Structure.shp")
    ap.add_argument("--dsm", default="DSM.tif")
    ap.add_argument("--dtm", default="DTM.tif")
    args = ap.parse_args()

    src, out = Path(args.src), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []

    epsgs: dict[str, int | None] = {}
    for role, name in (("bo", args.bo), ("rs", args.rs)):
        path = src / name
        if not path.exists():
            problems.append(f"{path} not found")
            continue
        gdf = gpd.read_file(path)
        if gdf.empty:
            problems.append(f"{name} has no features")
            continue
        if gdf.crs is None:
            problems.append(f"{name} has no CRS (missing .prj?)")
            continue
        epsgs[role] = gdf.crs.to_epsg()
        gdf.to_file(out / f"{role}.geojson", driver="GeoJSON")
        holes = sum(len(g.interiors) for g in gdf.geometry if g.geom_type == "Polygon")
        print(
            f"{role}: {len(gdf)} features, EPSG:{epsgs[role]}, "
            f"{holes} interior rings, {int((~gdf.is_valid).sum())} invalid geometries"
        )

    for role, name in (("dsm", args.dsm), ("dtm", args.dtm)):
        path = src / name
        if not path.exists():
            problems.append(f"{path} not found")
            continue
        shutil.copyfile(path, out / f"{role}.tif")
        with rasterio.open(path) as r:
            epsgs[role] = r.crs.to_epsg() if r.crs else None
            print(f"{role}: {r.width}x{r.height}, GSD {r.res[0]:.3f} m, EPSG:{epsgs[role]}")

    if len({v for v in epsgs.values()}) > 1:
        problems.append(f"mixed reference systems: {epsgs}")
    if None in epsgs.values():
        problems.append(f"an input has no EPSG code: {epsgs}")

    if problems:
        print("\nNOT READY")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"\nREADY in {out}  (all inputs EPSG:{next(iter(epsgs.values()))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
