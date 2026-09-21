# CLAUDE.md

Guidance for Claude Code in this repository.

## What this is

NARAGA **3D Reconstruction** — FastAPI service on port **8084**, package
`reconstruction_3d`, path segment `reconstruction-3d`. One of five Python AI/geo
services in the NARAGA mesh. The NestJS gateway at **:8080** owns the database, mints
signed URLs, and dispatches jobs here over the internal contract; this service does the
work and **calls back** to `request.callback_url`. It never touches Postgres.

**Naming trap:** the repo reorders the words (`naraga-3d-reconstruction`) while the
package is `reconstruction_3d` — a Python identifier **cannot start with a digit**, so
`3d_reconstruction` is impossible. The path segment is `reconstruction-3d`. All three
are listed in `contract/services.yaml`; never derive one from another.

## Commands

```bash
uv sync                    # API layer only; geo/ML wheels: uv sync --extra geo
uv run pytest
uv run ruff check .        # CI runs this WITHOUT --fix: it must fail, not rewrite
uv run mypy                # strict; files = src, tests
uv run uvicorn reconstruction_3d.main:app --app-dir src --port 8084 --reload
docker compose up          # Dockerfile.dev: GDAL 3.9 + PDAL 2.8 + laspy from conda-forge
```

`INTERNAL_SERVICE_TOKEN` has no default, so a native run needs a `.env` with it set
(or the app raises at import). GDAL/PDAL/open3d are **not** pip-installable — mesh and
point-cloud code runs in Docker.

## Architecture

Five contract-mandated internal ops under `PREFIX = /v1/internal/reconstruction-3d`, **all** requiring
`Authorization: Bearer $INTERNAL_SERVICE_TOKEN`: `POST /jobs` (validate →
`store.insert_job` → 202 + `credits_estimated`, work runs as an asyncio task),
`DELETE /jobs/{id}` (mark cancelled, cancel the task, 202), `GET /jobs/{id}/status`,
`POST /estimate`, and `GET /capabilities` (contract_version 2.1.0, `MODELS`, `OUTPUT_FORMATS`).

`/health` and `/ready` on the app (not the router) are **unauthenticated** — `/health`
must never touch a dependency, since failing it restarts the container.

`run_job(request, report_progress)` in `jobs.py` is the seam. `_execute()` wraps it in
`asyncio.timeout(max_job_duration_seconds)` and maps every outcome to a callback.
`StateStore` (`store.py`) is a durable SQLite file at `STATE_DB_PATH` (default
`data/state.db`, WAL + `synchronous=FULL`) with two tables: `jobs` (idempotency PK,
status/progress, `last_sequence`, `callback_url`) and `outbox` (unacked terminal
callbacks). On startup `jobs.startup()` reports orphaned `processing` rows as `failed`
and launches `drain_outbox_forever`, which retries every
`OUTBOX_DRAIN_INTERVAL_SECONDS`.

## THE RULES — invariants that must not be "cleaned up"

- **`run_job()` is the ONLY thing to replace.** Idempotency, callback sequencing, the
  outbox, startup recovery and cancellation are contract rules that are easy to get
  wrong; they are already right.
- **Terminal callbacks go into the outbox BEFORE the first send attempt**
  (`callbacks.py` `CallbackSender.send`). Reversing the order means a crash between
  send and persist silently loses finished GPU work — and a mesh reconstruction is the
  most expensive thing in the mesh to lose. The gateway dedups on `sequence`, so a
  duplicate delivery is free; a lost one is not.
- **Idempotency on `job_id` via the SQLite PRIMARY KEY → 409 on replay.** The gateway
  treats 409 as **success** (it means "I already have it"), so never downgrade it to
  200 or re-accept the job.
- **Report ONLY `processing` | `complete` | `failed`.** `draft`/`queued`/`cancelled` are
  gateway-owned states; emitting one corrupts the gateway's state machine.
- **Progress callbacks are disposable** — one attempt, warn on failure; retrying only
  delays the next heartbeat. **Terminal callbacks retry forever until acked.** A **4xx
  is never retried** in either path: identical bytes fail identically.
- **`emitted_at` must be RFC3339 with exactly milliseconds and a literal `Z`.**
  `build_callback()` validates against `JobCallback` but **sends the hand-built dict**,
  because `model_dump()` would re-serialize to microseconds and `+00:00`, which the
  gateway rejects. Do not "simplify" this to `model_dump()`.
- **After a cancel, send NOTHING further** — not a `cancelled`, not a `failed`. The
  `asyncio.CancelledError` branch in `_execute` deliberately logs and returns.
- **Write outputs ONLY under `request.output_prefix`** (use the `storage_key` from
  `output_upload_urls[n]`). The gateway will not mint a dataset outside that prefix.
- **Sync PDAL/GDAL/open3d/PyTorch must run via `await asyncio.to_thread(...)`.** These
  are the longest jobs in the mesh (sample `max_job_duration_seconds` is 10800);
  blocking the event loop freezes `/health` and the container is killed mid-job.
- **Re-check the `elevation_source` cross-field rule here** — see Service specifics.
- **Never hand-edit `src/reconstruction_3d/contract/models.py` or
  `contract/openapi.yaml`** — generated/vendored, CI regenerates and diffs
  byte-for-byte. Change the canonical spec in `dtugm/naraga-contract` and let
  propagation regenerate here.
- **Look up ports and package names in `contract/services.yaml`, never derive them from
  the repo name** — this service is exactly why that rule exists (see above), and
  `landcover_detection` → `naraga-land-cover-detection` breaks the same way.
- Never log `signed_url` values or the internal token; signed URLs are credentials.

## Current state: `run_job()` is a STUB

It sleeps 3 × 0.05s, emits progress `25/50/75`, and returns **one fabricated output
draft** with `size_bytes: 0` and `crs: "EPSG:4326"`. It **never reads
`input_datasets["building_outline"].signed_url`** (or the elevation input) and **never
PUTs to `output_upload_urls[0].url`**. A green job proves the control plane (dispatch,
sequencing, credits, dataset minting) and nothing about the data plane.

## Config (`config.py` — nothing reads `os.environ` directly)

`INTERNAL_SERVICE_TOKEN` (**no default — crashes at boot by design**),
`SERVICE_NAME` (`reconstruction_3d`), `LOG_LEVEL` (`INFO`), `STATE_DB_PATH`
(`data/state.db` — mount a volume), `OUTBOX_DRAIN_INTERVAL_SECONDS` (`30.0`).

## Testing

`uv run pytest`. `tests/conftest.py` sets `INTERNAL_SERVICE_TOKEN=test-token` at import
time and has an **autouse `_fresh_state` fixture** giving every test its own
`STATE_DB_PATH` under `tmp_path`, a 0.05s drain interval, and a cleared
`get_settings` cache.

- `_install_gateway_sink(events)` swaps `app.state.http` for an `httpx.MockTransport`
  client that asserts the bearer header and records payloads. **Call it INSIDE the
  `with TestClient(app)` block** — the lifespan overwrites `app.state.http` on entry,
  so installing it before the block is silently discarded.
- **Two sequential `TestClient(app)` blocks against the same state db simulate a
  restart** (`test_restart_recovers_orphans_and_keeps_idempotency`): the first exits
  mid-job, the second must 409 the resubmit, report the orphan failed, and drain it.

`tests/test_contract.py` cross-checks `CONTRACT_VERSION` against
`contract/openapi.yaml` and `MODELS`/`OUTPUT_FORMATS` against `contract/services.yaml`.

## Service specifics

- Models: **`dream3d`, `cascade_mesh`**. Output formats: `gltf`, `3dtiles`.
- Inputs: **`building_outline` required**; `point_cloud`, `dtm`, `dsm`,
  `remote_sensing` optional in the schema. Which optional ones are *actually* required
  is decided by `params.elevation_source`.
- **`params` is the only one in the mesh with a third required field:**
  `{model, elevation_source, output_formats[], output_crs?}` where `elevation_source`
  is `pointcloud | dtm_dsm`, `additionalProperties: false`.
- **The `elevation_source` cross-field rule** (`x-cross-field-validation` in the spec):
  - `pointcloud` → `point_cloud` required; `dtm` and `dsm` forbidden
  - `dtm_dsm` → `dtm` **and** `dsm` required; `point_cloud` forbidden

  JSON Schema cannot express this (the constraint spans sibling objects, and an earlier
  `if/then` attempt made the closed schema unsatisfiable), so **no generated model
  enforces it**. The gateway must enforce it at `POST /jobs`, `PATCH /jobs/{id}` and
  `/submit`, and **this service MUST re-check it** on the `InternalJobRequest` it
  receives, failing with `422 MISSING_REQUIRED_INPUT` and
  `details.missing_input_keys`. That re-check does not exist yet — add it when you
  replace `run_job()`.
- A `geotiff` from Point Cloud Classification carrying `dataset_role: dtm`/`dsm` is the
  intended upstream for the `dtm_dsm` path.
