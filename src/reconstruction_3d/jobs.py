"""The worked example: accept a job, run it in the background, report back.

This implements the five internal operations every service MUST expose (see
contract/openapi.yaml, "INTERNAL: gateway -> service"), backed by the durable
StateStore: idempotency and the terminal-callback outbox survive a restart, and
startup recovery reports jobs orphaned by a crash instead of leaving the gateway
to wait out the reaper timeout.

REPLACE run_job() with the real 3D Reconstruction pipeline; keep everything around it — the
auth check, idempotent 409, callback sequencing, cancellation, timeout and recovery
are the contract rules that are easy to get wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import __version__
from .callbacks import CallbackSender, build_callback, drain_outbox_forever, rfc3339_now
from .config import get_settings
from .contract.models import (
    EstimateResponse,
    InternalEstimateRequest,
    InternalJobAccepted,
    InternalJobRequest,
    InternalJobStatus,
    ServiceCapabilities,
)
from .store import StateStore

log = logging.getLogger(__name__)

PREFIX = "/v1/internal/reconstruction-3d"
CONTRACT_VERSION = "1.0.1"  # tests assert this matches contract/openapi.yaml
MODELS: list[str] = ["dream3d", "cascade_mesh"]
OUTPUT_FORMATS: list[str] = ["gltf", "3dtiles"]
MAX_INPUT_SIZE_BYTES = 10 * 2**30  # 10 GiB placeholder — tune per real model limits

router = APIRouter(prefix=PREFIX, tags=["jobs"])

# asyncio.Task handles can't be persisted; everything else lives in the StateStore.
_tasks: dict[str, asyncio.Task[None]] = {}


async def startup(app: FastAPI) -> None:
    """Open the store, report orphans, start the outbox drainer. Called by lifespan."""
    store = StateStore(get_settings().state_db_path)
    app.state.store = store
    for job_id in store.orphaned_processing():
        # This process died mid-job in a previous life. The work is gone; say so
        # now instead of leaving the gateway to wait for the reaper timeout.
        job = store.get_job(job_id)
        progress = job["progress_percent"] if job else 0
        store.set_status(job_id, "failed", progress)
        store.outbox_add(
            job_id,
            build_callback(
                store,
                job_id,
                "failed",
                progress,
                error_code="INTERNAL_ERROR",
                error_message="service restarted while the job was running",
                credits_used=0,
            ),
        )
        log.warning("recovered orphaned job %s as failed", job_id)
    app.state.outbox_drainer = asyncio.create_task(drain_outbox_forever(app))


async def shutdown(app: FastAPI) -> None:
    for task in [app.state.outbox_drainer, *_tasks.values()]:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    _tasks.clear()
    app.state.store.close()


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    """Contract-shaped ErrorResponse — clients branch on error.code, never on text."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {"code": code, "message": message, "details": None},
            "meta": {"request_id": str(uuid4()), "timestamp": rfc3339_now()},
        },
    )


def _check_auth(authorization: str | None) -> JSONResponse | None:
    if authorization != f"Bearer {get_settings().internal_service_token}":
        return _error(401, "UNAUTHENTICATED", "Missing or invalid internal service token.")
    return None


def _estimate_credits(input_datasets: Any) -> tuple[int, int]:
    """Naive pricing: 1 credit per started 100 MiB of input. Replace with real pricing."""
    total_bytes = sum(int(d.size_bytes) for d in input_datasets.values())
    return max(1, math.ceil(total_bytes / (100 * 2**20))), total_bytes


async def run_job(
    request: Any, report_progress: Callable[[int], Awaitable[None]]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, int]:
    """REPLACE THIS with the real 3D Reconstruction pipeline.

    Returns (output dataset drafts, result_summary or None, credits_used).
    A real implementation streams inputs from request.input_datasets[...].signed_url,
    writes each output with an HTTP PUT to request.output_upload_urls[n].url (always
    under request.output_prefix), returns this service's result model as a dict, and
    MUST call report_progress at least every request.heartbeat_interval_seconds.

    CRITICAL: PyTorch/GDAL/PDAL code is synchronous. Run it via
    `await asyncio.to_thread(...)` (or a process pool) — blocking the event loop
    freezes /health and the orchestrator kills the container mid-job.
    """
    for pct in (25, 50, 75):
        await asyncio.sleep(0.05)  # simulated work; also gives cancellation a window
        await report_progress(pct)
    upload = request.output_upload_urls[0]
    draft: dict[str, Any] = {
        "name": "example-output",
        "dataset_format": upload.output_format,
        "dataset_role": None,
        "storage_key": upload.storage_key,  # MUST be under request.output_prefix
        "size_bytes": 0,
        "crs": "EPSG:4326",
        "bbox": None,
    }
    credits_used, _ = _estimate_credits(request.input_datasets)
    return [draft], None, credits_used


async def _execute(job_id: str, request: Any, sender: CallbackSender, store: StateStore) -> None:
    """Run one job and report every outcome the contract defines."""

    async def report_progress(pct: int) -> None:
        job = store.get_job(job_id)
        if job is not None and job["cancelled"]:
            raise asyncio.CancelledError
        store.set_status(job_id, "processing", pct)
        await sender.send("processing", pct)

    try:
        async with asyncio.timeout(float(request.max_job_duration_seconds)):
            await sender.send("processing", 0)
            drafts, result_summary, credits_used = await run_job(request, report_progress)
        store.set_status(job_id, "complete", 100)
        await sender.send(
            "complete",
            100,
            output_datasets=drafts,
            result_summary=result_summary,
            credits_used=credits_used,
        )
    except asyncio.CancelledError:
        # DELETE /jobs/{id} cancelled us: stop work, send NOTHING further (contract).
        log.info("job %s cancelled; no further callbacks will be sent", job_id)
    except TimeoutError:
        job = store.get_job(job_id)
        progress = job["progress_percent"] if job else 0
        store.set_status(job_id, "failed", progress)
        await sender.send(
            "failed",
            progress,
            error_code="JOB_TIMEOUT",
            error_message="exceeded max_job_duration_seconds",
            credits_used=0,
        )
    except Exception as exc:
        job = store.get_job(job_id)
        progress = job["progress_percent"] if job else 0
        store.set_status(job_id, "failed", progress)
        log.exception("job %s failed", job_id)
        await sender.send(
            "failed",
            progress,
            error_code="INTERNAL_ERROR",
            error_message=str(exc),
            credits_used=0,
        )
    finally:
        _tasks.pop(job_id, None)


@router.post("/jobs")
async def submit_job(
    request: Request,
    payload: dict[str, Any],
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    if (denied := _check_auth(authorization)) is not None:
        return denied
    try:
        job_request = InternalJobRequest.model_validate(payload)
    except ValidationError as exc:
        return _error(422, "VALIDATION_ERROR", str(exc))

    store: StateStore = request.app.state.store
    job_id = str(job_request.job_id)
    if not store.insert_job(job_id, str(job_request.callback_url)):
        # Idempotent on job_id — restart-proof: the store, not memory, remembers.
        return _error(409, "CONFLICT", f"job {job_id} was already accepted")

    sender = CallbackSender(
        job_id=job_id,
        callback_url=str(job_request.callback_url),
        client=request.app.state.http,
        store=store,
    )
    _tasks[job_id] = asyncio.create_task(_execute(job_id, job_request, sender, store))

    credits_estimated, _ = _estimate_credits(job_request.input_datasets)
    accepted: dict[str, Any] = InternalJobAccepted.model_validate(
        {"accepted": True, "job_id": job_id, "credits_estimated": credits_estimated}
    ).model_dump(mode="json")
    return JSONResponse(status_code=202, content=accepted)


@router.delete("/jobs/{job_id}")
async def cancel_job(
    request: Request, job_id: str, authorization: Annotated[str | None, Header()] = None
) -> Response:
    if (denied := _check_auth(authorization)) is not None:
        return denied
    store: StateStore = request.app.state.store
    if store.get_job(job_id) is None:
        return _error(404, "NOT_FOUND", f"no job {job_id}")
    store.set_cancelled(job_id)
    task = _tasks.get(job_id)
    if task is not None and not task.done():
        task.cancel()
    return Response(status_code=202)


@router.get("/jobs/{job_id}/status")
async def job_status(
    request: Request, job_id: str, authorization: Annotated[str | None, Header()] = None
) -> Response:
    if (denied := _check_auth(authorization)) is not None:
        return denied
    job = request.app.state.store.get_job(job_id)
    if job is None:
        return _error(404, "NOT_FOUND", f"no job {job_id}")
    status: dict[str, Any] = InternalJobStatus.model_validate(
        {"job_id": job_id, "status": job["status"], "progress_percent": job["progress_percent"]}
    ).model_dump(mode="json")
    return JSONResponse(status)


@router.post("/estimate")
async def estimate(
    payload: dict[str, Any], authorization: Annotated[str | None, Header()] = None
) -> Response:
    if (denied := _check_auth(authorization)) is not None:
        return denied
    try:
        req = InternalEstimateRequest.model_validate(payload)
    except ValidationError as exc:
        return _error(422, "VALIDATION_ERROR", str(exc))
    credits, total_bytes = _estimate_credits(req.input_datasets)
    response: dict[str, Any] = EstimateResponse.model_validate(
        {
            "credits_estimated": credits,
            "estimated_output_bytes": total_bytes,  # naive: assume output ≈ input
            "breakdown": {
                "line_items": [
                    {
                        "label": "input volume",
                        "unit": "100 MiB",
                        "quantity": round(total_bytes / (100 * 2**20), 3),
                        "unit_credits": 1,
                        "subtotal": credits,
                    }
                ],
                "total": credits,
            },
        }
    ).model_dump(mode="json")
    return JSONResponse(response)


@router.get("/capabilities")
async def capabilities(authorization: Annotated[str | None, Header()] = None) -> Response:
    if (denied := _check_auth(authorization)) is not None:
        return denied
    caps: dict[str, Any] = ServiceCapabilities.model_validate(
        {
            "service": "reconstruction_3d",
            "version": __version__,
            "contract_version": CONTRACT_VERSION,
            "models": MODELS,
            "output_formats": OUTPUT_FORMATS,
            "max_input_size_bytes": MAX_INPUT_SIZE_BYTES,
        }
    ).model_dump(mode="json", exclude_none=True)
    return JSONResponse(caps)
