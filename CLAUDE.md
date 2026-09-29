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
uv sync --extra geo        # geo wheels (rasterio, geopandas, shapely, fiona, pyproj, laspy)
uv run pytest
uv run ruff check .        # CI runs this WITHOUT --fix: it must fail, not rewrite
uv run mypy                # strict; files = src, tests
uv run uvicorn reconstruction_3d.main:app --app-dir src --port 8084 --reload
docker compose up --build  # Dockerfile.dev: GDAL 3.9 + PDAL 2.8 from conda-forge + '.[geo]'
```

`INTERNAL_SERVICE_TOKEN` has no default, so a native run needs a `.env` with it set
(or the app raises at import). Plain `uv sync` installs the API layer only: the LOD2
tests then **skip** (`pytest.importorskip("geopandas")`) and mypy fails on the missing
`numpy` import (the other geo libs are in the `ignore_missing_imports` override; numpy is
not). CI runs `uv sync --frozen --extra geo`. The geo extra is all PyPI
wheels; only **PDAL** is not pip-installable (conda-forge, Docker image only).

**`PROJ_LIB` / `GDAL_DATA` from another install (PostGIS, QGIS) break rasterio**: it
reads an older `proj.db` and every CRS lookup fails with `DATABASE.LAYOUT.VERSION.MINOR`.
`tests/conftest.py` unsets them for the suite; unset them yourself before a native run.

## Architecture

Five contract-mandated internal ops under `PREFIX = /v1/internal/reconstruction-3d`, **all** requiring
`Authorization: Bearer $INTERNAL_SERVICE_TOKEN`: `POST /jobs` (validate →
`store.insert_job` → 202 + `credits_estimated`, work runs as an asyncio task),
`DELETE /jobs/{id}` (mark cancelled, cancel the task, 202), `GET /jobs/{id}/status`,
`POST /estimate`, and `GET /capabilities` (contract_version 2.1.0, `MODELS`, `OUTPUT_FORMATS`).

`/health` and `/ready` on the app (not the router) are **unauthenticated** — `/health`
must never touch a dependency, since failing it restarts the container.

`run_job(request, report_progress, http)` in `jobs.py` is the seam; it dispatches on
`params.model` (see Current state). `http` is `sender.client`, the same shared
`httpx.AsyncClient` the callbacks use, so tests that swap `app.state.http` for a
`MockTransport` also intercept signed-URL downloads and presigned PUTs. `_execute()`
wraps `run_job` in `asyncio.timeout(max_job_duration_seconds)` and maps every outcome to
a callback.
`StateStore` (`store.py`) is a durable SQLite file at `STATE_DB_PATH` (default
`data/state.db`, WAL + `synchronous=FULL`) with two tables: `jobs` (idempotency PK,
status/progress, `last_sequence`, `callback_url`) and `outbox` (unacked terminal
callbacks). On startup `jobs.startup()` reports orphaned `processing` rows as `failed`
and launches `drain_outbox_forever`, which retries every
`OUTBOX_DRAIN_INTERVAL_SECONDS`.

## THE RULES — invariants that must not be "cleaned up"

- **Model work lives behind `run_job()`** (one branch per model). Idempotency, callback
  sequencing, the outbox, startup recovery and cancellation are contract rules that are
  easy to get wrong; they are already right. Add a model by adding a branch, not by
  touching `_execute()`.
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
- **Sync geo code (rasterio/geopandas/shapely, PDAL, external binaries) must run via
  `await asyncio.to_thread(...)` or an async subprocess.** These are the longest jobs in
  the mesh (sample `max_job_duration_seconds` is 10800); blocking the event loop freezes
  `/health` and the container is killed mid-job. A thread cannot be killed, so long
  work must also poll a cancel flag (see `generate_lod2(should_cancel=...)`).
- **Re-check the `elevation_source` cross-field rule here** — see Service specifics.
- **Never hand-edit `src/reconstruction_3d/contract/models.py` or
  `contract/openapi.yaml`** — generated/vendored, CI regenerates and diffs
  byte-for-byte. Change the canonical spec in `dtugm/naraga-contract` and let
  propagation regenerate here.
- **Look up ports and package names in `contract/services.yaml`, never derive them from
  the repo name** — this service is exactly why that rule exists (see above), and
  `landcover_detection` → `naraga-land-cover-detection` breaks the same way.
- Never log `signed_url` values or the internal token; signed URLs are credentials.
  httpx error messages embed the URL, so re-raise download/upload failures with
  `from None` and a message naming only the input key (see `cascade_mesh._download`).

## Current state: `cascade_mesh` is real, `dream3d` is a STUB

**`cascade_mesh` (implemented)** — watertight LOD2 buildings from building outline +
roof structure + DSM + DTM, written as **CityJSON 1.1** (one `Solid` per building, LoD
`"2"`, Roof/Ground/Wall semantics, integer vertices with `transform`). CPU only.

- `cascade_mesh.py` is the service glue: `validate_request()` → stream every signed URL
  into a per-job temp dir → `lod2.generate_lod2` via `asyncio.to_thread`, heartbeating
  at `min(heartbeat_interval_seconds / 2, 5)` s through `_with_heartbeat` → PUT the
  CityJSON (explicit `Content-Length`; presigned PUTs reject chunked bodies) to
  `output_upload_urls[0]`. On cancel or timeout, `_with_heartbeat` cancels the in-flight
  download/upload task (asyncio.wait alone would let a PUT land for a dead job) and a
  `threading.Event` makes the worker thread stop between buildings and before the write.
- `lod2/` is a typed rewrite of the external reference
  `sam-interactive-github/ai/lod_generation_v2` (never imported or vendored), one module
  per stage: `partition` → `plane_fit` → `reconcile` → `triangulate` → `solid` →
  `validate` → `cityjson`, orchestrated by `core.generate_lod2(params, progress_cb,
  should_cancel)`. It is synchronous, never writes to its inputs, and validates every
  shell (half-edge closure + positive signed volume + volume plausibility).
- The service runs it with **`on_invalid="skip"`**: a building that fails validation is
  left out (its vertices are rolled back out of the shared pool) and logged. The job
  fails instead when more than `MAX_SKIPPED_FRACTION` (0.5) of the buildings are left
  out. The library default stays `"warn"`, the reference's behaviour, so parity with the
  reference can still be checked.
- Inputs must share one **projected** CRS with an EPSG code (`check_crs_agreement`): the
  tolerances are metric. A DTM hole never means ground = 0 m; the ground then comes from
  the DSM 1–5 m around the footprint (`ground_source = "dsm_surroundings"`).
- Progress: download 5 → 10, pipeline 10 → 88, upload 90, terminal 100.

**`dream3d` (stub, `_run_placeholder`)** — sleeps 3 × 0.05s, emits `25/50/75`, and
reports `complete` with **one fabricated draft** (`size_bytes: 0`, `crs: "EPSG:4326"`).
It reads no input and PUTs nothing. The real pipeline wraps the external Geoflow binary
and needs a point cloud classified with ASPRS classes 2 (ground) and 6 (building).

**Contract gaps bridged in `cascade_mesh.py`** (pending in `dtugm/naraga-contract`):

- There is no `roof_structure` input key yet, so the roof structure is read from
  `remote_sensing` (`ROOF_STRUCTURE_KEY`).
- `DatasetFormat` has no `cityjson`. The CityJSON goes to `output_upload_urls[0]` and the
  output draft reports **that slot's** `dataset_format` (e.g. `gltf`). Conversion to
  glTF / 3D Tiles is `naraga-converter`'s job. `result_summary` is `null`, because
  `Reconstruction3dResult.output_formats_generated` only allows `gltf` / `3dtiles`.
- Every failure other than a timeout (`JOB_TIMEOUT`) is reported as `INTERNAL_ERROR`
  with `credits_used: 0`, including bad user input. `job-lifecycle.md` refunds user-input failures at 0%, so input errors
  should get their own codes (`VALIDATION_ERROR`, `CRS_MISMATCH`, …).

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

LOD2 tests need the geo extra (they skip without it):

- `tests/lod2_fixtures.py` writes a synthetic gable house (20 × 10 m, eaves 10 m, ridge
  15 m) on a realistic UTM 48S origin, so the int32 range of the quantised vertices is
  exercised. GeoJSON is written by hand with the legacy top-level `"crs"` member;
  without it GDAL reads the file as lon/lat.
- `tests/test_lod2.py` asserts the closed-form answer (volume 2500 m³ ± 1 %, ridge,
  eaves, ground), the CityJSON 1.1 shape, that inputs are byte-identical afterwards,
  and that `should_cancel` stops the run without writing output.
- `tests/test_cascade_mesh.py` drives the whole HTTP flow. `_install_world` is one
  `MockTransport` that is gateway (callbacks) **and** object storage (signed GETs,
  presigned PUT); same rule as `_install_gateway_sink`: call it inside the
  `with TestClient(app)` block. It also covers a missing roof structure and a failed
  download whose signed URL must not leak into `error_message`.

## Service specifics

- Models: **`dream3d`, `cascade_mesh`**. Output formats: `gltf`, `3dtiles`.
- Inputs: **`building_outline` required**; `point_cloud`, `dtm`, `dsm`,
  `remote_sensing` optional in the schema. Which optional ones are *actually* required
  is decided by `params.elevation_source`.
- Model ↔ elevation source is one-to-one: `cascade_mesh` is always `dtm_dsm` and also
  needs the roof structure (`remote_sensing` for now); `dream3d` is always `pointcloud`.
  `cascade_mesh` accepts vectors as `geojson` only and rasters as `geotiff` / `cog`.
- `InternalJobRequest.input_datasets` is `dict[str, ResolvedDataset]` — the generated
  model does **not** restrict its keys, so input-key checks must be written by hand.
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
  `details.missing_input_keys`.

  **Known gap:** `cascade_mesh.validate_request()` performs this re-check (plus the
  roof-structure key and input formats) **inside the job**, after the 202, so a bad
  request ends as a `failed` callback with `INTERNAL_ERROR` rather than a 422 at
  `POST /jobs`. Credits are debited only after the 202, so moving these no-I/O checks
  in front of `store.insert_job` would also spare the user the charge. `dream3d` has no
  re-check at all yet.
- A `geotiff` from Point Cloud Classification carrying `dataset_role: dtm`/`dsm` is the
  intended upstream for the `dtm_dsm` path.
