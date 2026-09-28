"""Parity check: reference lod_generation_v2 vs reconstruction_3d.lod2 on identical inputs.

Builds five synthetic buildings with closed-form answers (gable, hip, courtyard, no roof
structure, L-shape with a stepped roof), runs BOTH implementations and compares the QA
report and the CityJSON geometry byte for byte.

Needs the untracked reference clone at sam-interactive-github/ (read-only, never imported
by the service). Run from the repo root:

    uv run python -W ignore docs/lod2-testing/parity_check.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.transform import from_origin

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "sam-interactive-github"))
sys.path.insert(0, str(REPO / "src"))

from ai.lod_generation_v2.core import generate_lod2 as ref_generate  # noqa: E402
from ai.lod_generation_v2.interface import LOD2V2Params  # noqa: E402

from reconstruction_3d.lod2 import LOD2Params  # noqa: E402
from reconstruction_3d.lod2 import generate_lod2 as port_generate  # noqa: E402

EPSG = 32748
X0, Y0 = 699000.0, 9309000.0
GSD = 0.25
GROUND = 2.0

Point = tuple[float, float]


def rect(x: float, y: float, w: float, h: float) -> list[Point]:
    return [(X0 + x, Y0 + y), (X0 + x + w, Y0 + y), (X0 + x + w, Y0 + y + h), (X0 + x, Y0 + y + h)]


def ring(pts: list[Point]) -> list[list[float]]:
    return [list(p) for p in pts] + [list(pts[0])]


COURT_OUTER = rect(0, 30, 30, 30)
COURT_HOLE = list(reversed(rect(10, 40, 10, 10)))
L_SHAPE = [
    (X0 + 60, Y0 + 0),
    (X0 + 80, Y0 + 0),
    (X0 + 80, Y0 + 8),
    (X0 + 68, Y0 + 8),
    (X0 + 68, Y0 + 20),
    (X0 + 60, Y0 + 20),
]
APEX = (X0 + 46, Y0 + 6)

BUILDINGS = [
    ("gable", [ring(rect(0, 0, 20, 10))]),
    ("hip", [ring(rect(40, 0, 12, 12))]),
    ("courtyard", [ring(COURT_OUTER), ring(COURT_HOLE)]),
    ("no_rs", [ring(rect(40, 30, 8, 8))]),
    ("l_step", [ring(L_SHAPE)]),
]

ROOFS = [
    [ring(rect(0, 0, 20, 5))],  # gable, south slope
    [ring(rect(0, 5, 20, 5))],  # gable, north slope
    [ring([(X0 + 40, Y0 + 0), (X0 + 52, Y0 + 0), APEX])],  # hip: four triangles
    [ring([(X0 + 52, Y0 + 0), (X0 + 52, Y0 + 12), APEX])],
    [ring([(X0 + 52, Y0 + 12), (X0 + 40, Y0 + 12), APEX])],
    [ring([(X0 + 40, Y0 + 12), (X0 + 40, Y0 + 0), APEX])],
    [ring(COURT_OUTER), ring(COURT_HOLE)],  # courtyard: one flat segment with the hole
    [ring(rect(60, 0, 20, 8))],  # L: long wing at 9 m
    [ring(rect(60, 8, 8, 12))],  # L: short wing at 6 m
]

# Closed-form volumes above GROUND (l_step is excluded: reconciliation deliberately
# averages the 3 m step along the shared edge, so it lands slightly below 1504).
EXPECTED_VOLUME = {"gable": 2100.0, "hip": 1200.0, "courtyard": 4800.0, "no_rs": 256.0}


def dsm_value(x: float, y: float) -> float:
    lx, ly = x - X0, y - Y0
    if 0 <= lx <= 20 and 0 <= ly <= 10:  # gable: eave 10, ridge 15
        return 10 + (ly if ly <= 5 else 10 - ly)
    if 40 <= lx <= 52 and 0 <= ly <= 12:  # hip pyramid: eave 9, apex 13
        return 13 - 4 * max(abs(lx - 46), abs(ly - 6)) / 6
    if 0 <= lx <= 30 and 30 <= ly <= 60 and not (10 <= lx <= 20 and 40 <= ly <= 50):
        return 8.0  # courtyard: flat roof, the hole is ground
    if 40 <= lx <= 48 and 30 <= ly <= 38:
        return 6.0  # box without roof structure
    if 60 <= lx <= 80 and 0 <= ly <= 8:
        return 9.0
    if 60 <= lx <= 68 and 8 <= ly <= 20:
        return 6.0
    return GROUND


def write_geojson(path: Path, features: list[dict[str, Any]]) -> None:
    doc = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": f"urn:ogc:def:crs:EPSG::{EPSG}"}},
        "features": features,
    }
    path.write_text(json.dumps(doc), encoding="utf-8")


def write_inputs(d: Path) -> dict[str, Path]:
    paths = {"bo": d / "bo.geojson", "rs": d / "rs.geojson", "dsm": d / "dsm.tif"}
    paths["dtm"] = d / "dtm.tif"
    write_geojson(
        paths["bo"],
        [
            {
                "type": "Feature",
                "properties": {"uuid_bgn": uid, "Id": i + 1},
                "geometry": {"type": "Polygon", "coordinates": rings},
            }
            for i, (uid, rings) in enumerate(BUILDINGS)
        ],
    )
    write_geojson(
        paths["rs"],
        [
            {
                "type": "Feature",
                "properties": {"seg": i},
                "geometry": {"type": "Polygon", "coordinates": rings},
            }
            for i, rings in enumerate(ROOFS)
        ],
    )
    pad = 3.0
    w, h = int((80 + 2 * pad) / GSD), int((60 + 2 * pad) / GSD)
    xs = (X0 - pad) + (np.arange(w) + 0.5) * GSD
    ys = (Y0 + 60 + pad) - (np.arange(h) + 0.5) * GSD
    dsm = np.array([[dsm_value(x, y) for x in xs] for y in ys], dtype="float32")
    profile = {
        "driver": "GTiff",
        "height": h,
        "width": w,
        "count": 1,
        "dtype": "float32",
        "crs": f"EPSG:{EPSG}",
        "transform": from_origin(X0 - pad, Y0 + 60 + pad, GSD, GSD),
    }
    for key, array in (("dsm", dsm), ("dtm", np.full_like(dsm, GROUND))):
        with rasterio.open(paths[key], "w", **profile) as ds:
            ds.write(array, 1)
    return paths


def main() -> int:
    d = Path(tempfile.mkdtemp(prefix="lod2-parity-"))
    p = write_inputs(d)
    common = {
        "input_building": str(p["bo"]),
        "input_roof": str(p["rs"]),
        "input_dsm": str(p["dsm"]),
        "input_dtm": str(p["dtm"]),
    }
    ref = ref_generate(LOD2V2Params(**common, output_file=str(d / "ref.json")))
    port = port_generate(LOD2Params(**common, output_file=str(d / "port.json")))

    failures: list[str] = []
    rd, pd = ref.to_dict(), port.to_dict()
    for report in (rd, pd):
        report.pop("elapsed_seconds")
        report.pop("output_file")
    if rd != pd:
        failures.append("QA reports differ")

    print("REF :", ref.summary)
    print("PORT:", port.summary)
    for b in port.buildings:
        print(
            f"  {b.object_id:10s} watertight={b.watertight} faces={b.n_roof_faces} "
            f"src={b.roof_source} roof={b.roof_min_z:.2f}..{b.roof_max_z:.2f} "
            f"ground={b.ground_z:.2f} vol={b.volume:.1f} dz={b.max_z_disagreement:.2f}"
        )
        if not b.watertight:
            failures.append(f"{b.object_id} not watertight: {b.problems}")
        expected = EXPECTED_VOLUME.get(b.object_id)
        if expected is not None and abs(b.volume - expected) > expected * 0.01:
            failures.append(f"{b.object_id} volume {b.volume:.1f} != {expected:.1f}")

    rj = json.loads((d / "ref.json").read_text(encoding="utf-8"))
    pj = json.loads((d / "port.json").read_text(encoding="utf-8"))
    for key in ("vertices", "transform", "metadata"):
        if rj[key] != pj[key]:
            failures.append(f"CityJSON {key} differ")
    if rj["CityObjects"].keys() != pj["CityObjects"].keys():
        failures.append("CityObject ids differ")
    else:
        for oid, obj in pj["CityObjects"].items():
            if rj["CityObjects"][oid]["geometry"] != obj["geometry"]:
                failures.append(f"geometry of {oid} differs")

    print(f"workdir: {d}")
    if failures:
        print("\nPARITY FAILED")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nPARITY PASSED: report, vertices, transform, metadata and geometry identical")
    return 0


if __name__ == "__main__":
    sys.exit(main())
