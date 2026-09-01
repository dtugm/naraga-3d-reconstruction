"""3D Reconstruction service."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Response

from . import jobs
from .config import get_settings

settings = get_settings()
logging.basicConfig(
    level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger(settings.service_name)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # One shared client for callback delivery. Tests swap app.state.http for an
    # httpx.MockTransport-backed client; we close the one WE created either way.
    http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
    app.state.http = http
    # Durable state: opens the SQLite store, reports jobs orphaned by a previous
    # crash, and starts the terminal-callback outbox drainer.
    await jobs.startup(app)
    try:
        yield
    finally:
        await jobs.shutdown(app)
        await http.aclose()


app = FastAPI(title="NARAGA 3D Reconstruction", version="0.1.0", lifespan=lifespan)
app.include_router(jobs.router)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Liveness. Must not touch dependencies: a failure here restarts the container."""
    return {"status": "ok"}


@app.get("/ready", tags=["health"])
async def ready(response: Response) -> dict[str, Any]:
    """Readiness. A failure pulls this instance out of the load balancer.

    Add real checks as you add dependencies — model weights loaded, GPU visible,
    storage reachable — and return 503 when any of them fail.
    """
    checks: dict[str, bool] = {}
    ok = all(checks.values())
    if not ok:
        response.status_code = 503
    return {"status": "ok" if ok else "unavailable", "checks": checks}
