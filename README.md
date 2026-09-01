# naraga-3d-reconstruction

NARAGA **3D Reconstruction** service — FastAPI, port `8084`.

Boilerplate plus one worked example: health probes, the five internal endpoints the contract mandates, a job runner with **durable state** (SQLite: restart-proof idempotency, orphan recovery, and a terminal-callback outbox), and a contract-correct callback client (auth, monotonic `sequence`, RFC3339-ms-`Z` timestamps).

**Replace `run_job()` in `src/reconstruction_3d/jobs.py` with the real pipeline;**

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
| Test                  | `uv run pytest`                                                |
| Lint / format / types | `uv run ruff check .` · `uv run ruff format .` · `uv run mypy` |
| Add a dependency      | `uv add <pkg>`                                                 |

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
