"""Run the LOD2 pipeline directly (no HTTP, no Docker) on real data and print a QA digest.

usage (from the repo root, with PROJ_LIB/GDAL_DATA unset):
  uv run python -W ignore docs/lod2-testing/run_direct.py <bo> <rs> <dsm> <dtm> <out_dir> [--ref]

  --ref  also run the reference lod_generation_v2 (needs the sam-interactive-github/ clone)
         and compare vertices and per-building geometry. Needs `uuid_bgn` in the outlines,
         otherwise both sides invent different random ids and nothing can match.

Writes <out_dir>/port.city.json and <out_dir>/report.json (the full per-building QA report).
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from reconstruction_3d.lod2 import LOD2Params, generate_lod2  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    for name in ("bo", "rs", "dsm", "dtm", "out_dir"):
        ap.add_argument(name)
    ap.add_argument("--ref", action="store_true")
    args = ap.parse_args()

    logging.disable(logging.CRITICAL)  # per-building warnings are summarised below instead
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    common = {
        "input_building": args.bo,
        "input_roof": args.rs,
        "input_dsm": args.dsm,
        "input_dtm": args.dtm,
    }

    def progress(message: str, percent: int) -> None:
        if percent in (2, 8, 10, 95, 98):
            print(f"[{percent:3d}%] {message}")

    report = generate_lod2(LOD2Params(**common, output_file=str(out / "port.city.json")), progress)
    (out / "report.json").write_text(json.dumps(report.to_dict(), indent=1), encoding="utf-8")
    bs = report.buildings

    print(f"\n{report.summary}  ({report.elapsed_seconds:.1f}s, EPSG:{report.crs_epsg})")
    print(f"ground clamped (DTM >= DSM): {report.n_ground_clamped}")
    print(f"buildings with a fallback plane: {sum(b.n_fallback_planes > 0 for b in bs)}")
    print(f"roof_source: {dict(collections.Counter(b.roof_source for b in bs))}")
    heights = sorted(b.height for b in bs)
    print(
        f"height m  p50 {statistics.median(heights):.1f}  p90 {heights[int(0.9 * len(bs))]:.1f}"
        f"  max {heights[-1]:.1f}  min {heights[0]:.1f}"
    )
    print(f"roof-plane disagreement > 1 m: {sum(b.max_z_disagreement > 1 for b in bs)} buildings")
    for b in sorted(bs, key=lambda x: -x.max_z_disagreement)[:5]:
        print(f"    {b.object_id}  dz {b.max_z_disagreement:5.2f} m  height {b.height:5.1f} m")
    flagged = [b for b in bs if not b.watertight]
    print(f"flagged by validation: {len(flagged)}")
    for b in flagged:
        print(f"    {b.object_id}: {b.problems}")

    status = 0
    if args.ref:
        sys.path.insert(0, str(REPO / "sam-interactive-github"))
        from ai.lod_generation_v2.core import generate_lod2 as ref_generate
        from ai.lod_generation_v2.interface import LOD2V2Params

        ref = ref_generate(LOD2V2Params(**common, output_file=str(out / "ref.city.json")))
        rj = json.loads((out / "ref.city.json").read_text(encoding="utf-8"))
        pj = json.loads((out / "port.city.json").read_text(encoding="utf-8"))
        same = sum(
            rj["CityObjects"].get(k, {}).get("geometry") == v["geometry"]
            for k, v in pj["CityObjects"].items()
        )
        ok = (
            rj["vertices"] == pj["vertices"]
            and rj["transform"] == pj["transform"]
            and rj["CityObjects"].keys() == pj["CityObjects"].keys()
            and same == len(pj["CityObjects"])
        )
        print(f"\nREF : {ref.summary}  ({ref.elapsed_seconds:.1f}s)")
        print(f"vertices identical: {rj['vertices'] == pj['vertices']} ({len(pj['vertices'])})")
        print(f"geometry identical: {same}/{len(pj['CityObjects'])} buildings")
        print("PARITY PASSED" if ok else "PARITY FAILED")
        status = 0 if ok else 1
    return status


if __name__ == "__main__":
    sys.exit(main())
