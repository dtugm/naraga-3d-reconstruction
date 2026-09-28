# naraga-3d-reconstruction

NARAGA **3D Reconstruction** service — FastAPI, port `8084`.

Boilerplate plus one worked example: health probes, the five internal endpoints the contract mandates, a job runner with **durable state** (SQLite: restart-proof idempotency, orphan recovery, and a terminal-callback outbox), and a contract-correct callback client (auth, monotonic `sequence`, RFC3339-ms-`Z` timestamps).

`run_job()` in `src/reconstruction_3d/jobs.py` dispatches per model:

| Model          | Status          | Inputs (`input_datasets` keys)                                           | Output                    |
| -------------- | --------------- | ------------------------------------------------------------------------ | ------------------------- |
| `cascade_mesh` | **implemented** | `building_outline`, `remote_sensing` (= roof structure), `dsm`, `dtm` | CityJSON 1.1, LOD2 Solids |
| `dream3d`      | placeholder     | `building_outline`, `point_cloud`                                        | —                         |

`cascade_mesh` requires `elevation_source: dtm_dsm`. Vector inputs must be `geojson`
and rasters `geotiff`/`cog`. The pipeline lives in `src/reconstruction_3d/lod2/`, a
typed rewrite of `sam-interactive-github/ai/lod_generation_v2`. Every building is a
closed, verified solid. The service only produces the CityJSON; `naraga-converter`
turns it into glTF / 3D Tiles.

> **Contract gaps (pending in naraga-contract):** there is no `roof_structure`
> input key (RS is read from `remote_sensing`), and `DatasetFormat` has no
> `cityjson`. Until those land, the CityJSON is uploaded to
> `output_upload_urls[0]` under that slot's declared format, and `result_summary`
> is `null`.

## Run

```bash
docker compose up
```

Docker is the supported path — the image installs GDAL and PDAL from conda-forge.
The PyPI `pdal` package is only a binding and won't build without them.

Native (API layer only, no GDAL/PDAL):

```bash
uv sync && uv run uvicorn reconstruction_3d.main:app --app-dir src --port 8084 --reload
```

<http://localhost:8084/health> · docs at `/docs`

## Commands

| Task                  | Command                                                        |
| --------------------- | -------------------------------------------------------------- |
| Test                  | `uv sync --extra geo && uv run pytest`                         |
| Lint / format / types | `uv run ruff check .` · `uv run ruff format .` · `uv run mypy` |
| Add a dependency      | `uv add <pkg>`                                                 |

Without the `geo` extra, the LOD2 tests are skipped, not failed. They build a
synthetic gable roof with a closed-form answer (volume 2500 m³, ridge 15 m,
eaves 10 m) and run it through the whole pipeline, including the HTTP job flow.

If `PROJ_LIB`/`GDAL_DATA` from another install (PostGIS, QGIS) are set
machine-wide, rasterio picks up the wrong `proj.db` ("DATABASE.LAYOUT.VERSION.MINOR").
`tests/conftest.py` unsets them for the suite. Do the same before running the
service natively.

## What's implemented

All five mandatory internal operations, under `PREFIX` (defined in `jobs.py`):

| Method | Path                            | Returns                                                                  |
| ------ | ------------------------------- | ------------------------------------------------------------------------ |
| POST   | `{PREFIX}/jobs`                 | `202 {accepted, job_id, credits_estimated}`; `409` on duplicate `job_id` |
| DELETE | `{PREFIX}/jobs/{job_id}`        | `202` — stops work, no further callbacks                                 |
| GET    | `{PREFIX}/jobs/{job_id}/status` | `{job_id, status, progress_percent}`                                     |
| POST   | `{PREFIX}/estimate`             | credits + breakdown (naive size-based — replace)                         |
| GET    | `{PREFIX}/capabilities`         | models, formats, contract version                                        |

The gateway calls `POST /jobs` and does **not** wait. The example accepts, returns 202, runs `run_job()` in the background, and reports back by POSTing to the job's `callback_url` via `callbacks.CallbackSender`:

```json
{
  "job_id": "...",
  "sequence": 2,
  "emitted_at": "2026-09-01T03:00:00.000Z",
  "status": "processing",
  "progress_percent": 30,
  "output_datasets": [],
  "result_summary": null,
  "credits_used": null,
  "error_code": null,
  "error_message": null
}
```

## Contract

`contract/openapi.yaml` is vendored from
[naraga-contract](https://github.com/dtugm/naraga-contract) and
`src/reconstruction_3d/contract/models.py` is generated from it. **Never hand-edit either** — CI
regenerates and fails on any difference. Change the contract there; it arrives here as a
sync PR.
