"""Write request.json (+ estimate.json) for a cascade_mesh job against mock_world.py.

usage (from the repo root):
  uv run python docs/lod2-testing/make_request.py <dir> <base_url> [--synthetic]
  <dir>        folder with bo.geojson, rs.geojson, dsm.tif, dtm.tif
  <base_url>   how the SERVICE reaches mock_world, e.g. http://localhost:9000
               (native) or http://host.docker.internal:9000 (docker compose)
  --synthetic  first write the synthetic gable inputs from tests/lod2_fixtures.py
"""

import json
import sys
import uuid
from pathlib import Path

root = Path(sys.argv[1]).resolve()
base = sys.argv[2].rstrip("/")
root.mkdir(parents=True, exist_ok=True)
if "--synthetic" in sys.argv:
    sys.path.insert(0, str(Path.cwd()))  # run from the repo root
    from tests.lod2_fixtures import write_gable_inputs

    write_gable_inputs(root)

job_id = str(uuid.uuid4())

# The dataset CRS is read from the DSM, so real data in another UTM zone needs no edit.
import rasterio  # noqa: E402

with rasterio.open(root / "dsm.tif") as _dsm:
    EPSG = f"EPSG:{_dsm.crs.to_epsg()}"


def dataset(role: str, fmt: str, dataset_role: str | None, file: str) -> dict:
    return {
        "dataset_id": str(uuid.uuid4()),
        "name": role,
        "dataset_format": fmt,
        "dataset_role": dataset_role,
        "size_bytes": (root / file).stat().st_size,
        "crs": EPSG,
        "bbox": None,
        "signed_url": f"{base}/get/{role}",
        "signed_url_expires_at": "2099-01-01T00:00:00.000Z",
    }


inputs = {
    "building_outline": dataset("bo", "geojson", "building_outline", "bo.geojson"),
    "remote_sensing": dataset("rs", "geojson", None, "rs.geojson"),  # = roof structure
    "dsm": dataset("dsm", "geotiff", "dsm", "dsm.tif"),
    "dtm": dataset("dtm", "geotiff", "dtm", "dtm.tif"),
}
params = {"model": "cascade_mesh", "elevation_source": "dtm_dsm", "output_formats": ["gltf"]}
request = {
    "job_id": job_id,
    "service": "reconstruction_3d",
    "model": "cascade_mesh",
    "input_datasets": inputs,
    "params": params,
    "output_prefix": f"jobs/{job_id}/outputs/",
    "output_upload_urls": [
        {
            "output_format": "gltf",
            "storage_key": f"jobs/{job_id}/outputs/lod2.city.json",
            "url": f"{base}/put/out",
            "expires_at": "2099-01-01T00:00:00.000Z",
        }
    ],
    "callback_url": f"{base}/callback",
    "max_job_duration_seconds": 3600,
    "heartbeat_interval_seconds": 30,
}
(root / "request.json").write_text(json.dumps(request, indent=2), encoding="utf-8")
(root / "estimate.json").write_text(
    json.dumps({"input_datasets": inputs, "params": params}, indent=2), encoding="utf-8"
)
print(f"job_id {job_id}  crs {EPSG}")
