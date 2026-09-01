"""Callback delivery to the gateway — the part every service gets subtly wrong.

Delivery model (see JobCallback in contract/openapi.yaml):
- Every callback gets a monotonic per-job sequence from the store, so the gateway
  can drop replays and out-of-order deliveries.
- TERMINAL callbacks (complete/failed) are written to the durable outbox in the
  same store as the status flip, attempted once inline, and — if the gateway is
  down — retried by the drainer every OUTBOX_DRAIN_INTERVAL_SECONDS until acked.
  The fixed cadence replaces the contract's 1→60s ladder; combined with the
  persistence the contract mandates, it is strictly at-least-once and survives
  restarts. A 4xx is never retried: identical bytes fail identically.
- PROGRESS callbacks are disposable heartbeats: one attempt, a warning on failure.
  Retrying them would only delay the next heartbeat.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import FastAPI

from .config import get_settings
from .contract.models import JobCallback

if TYPE_CHECKING:
    from .store import StateStore

log = logging.getLogger(__name__)

TERMINAL_STATUSES = ("complete", "failed")


def rfc3339_now() -> str:
    """RFC3339 UTC with millisecond precision and a literal Z, as the contract demands.

    Python's bare isoformat() emits microseconds and "+00:00" — the gateway rejects both.
    """
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def _post(client: httpx.AsyncClient, url: str, payload: dict[str, Any]) -> int | None:
    """One delivery attempt. Returns the status code, or None on a transport error."""
    try:
        r = await client.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {get_settings().internal_service_token}"},
        )
    except httpx.TransportError as exc:
        log.warning("callback to %s: transport error: %s", url, exc)
        return None
    return r.status_code


class CallbackSender:
    """One per job. Assigns sequence numbers and posts JobCallback payloads."""

    def __init__(
        self, job_id: str, callback_url: str, client: httpx.AsyncClient, store: StateStore
    ) -> None:
        self._job_id = job_id
        self._url = callback_url
        self._client = client
        self._store = store

    async def send(
        self,
        status: str,
        progress_percent: int,
        *,
        output_datasets: list[dict[str, Any]] | None = None,
        result_summary: dict[str, Any] | None = None,
        credits_used: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        payload = build_callback(
            self._store,
            self._job_id,
            status,
            progress_percent,
            output_datasets=output_datasets,
            result_summary=result_summary,
            credits_used=credits_used,
            error_code=error_code,
            error_message=error_message,
        )
        if status in TERMINAL_STATUSES:
            # Outbox row first, THEN the attempt: a crash between the two re-delivers
            # (gateway dedups on sequence); the other order silently loses the result.
            row_id = self._store.outbox_add(self._job_id, payload)
            code = await _post(self._client, self._url, payload)
            if code is not None and (code < 300 or 400 <= code < 500):
                if code >= 400:
                    log.error("terminal callback rejected with %d — 4xx is never retried", code)
                self._store.outbox_delete(row_id)
                return code < 300
            log.warning("terminal callback undelivered (%s); outbox drainer will retry", code)
            return False
        code = await _post(self._client, self._url, payload)
        if code is None or code >= 300:
            log.warning("progress callback dropped (%s) — heartbeats are not retried", code)
            return False
        return True


def build_callback(
    store: StateStore,
    job_id: str,
    status: str,
    progress_percent: int,
    *,
    output_datasets: list[dict[str, Any]] | None = None,
    result_summary: dict[str, Any] | None = None,
    credits_used: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "job_id": job_id,
        "sequence": store.next_sequence(job_id),
        "emitted_at": rfc3339_now(),
        "status": status,
        "progress_percent": progress_percent,
        "output_datasets": output_datasets or [],
        "result_summary": result_summary,
        "credits_used": credits_used,
        "error_code": error_code,
        "error_message": error_message,
    }
    # Shape-check against the generated contract model, but SEND the dict as built:
    # pydantic would re-serialize emitted_at with microsecond precision, and the
    # contract mandates milliseconds + literal Z.
    JobCallback.model_validate(payload)
    return payload


async def drain_outbox_forever(app: FastAPI) -> None:
    """Redeliver unacknowledged terminal callbacks until the gateway acks them.

    Runs for the life of the app; the first pass fires immediately, so callbacks
    stranded by a restart go out as soon as the service is back.
    """
    interval = get_settings().outbox_drain_interval_seconds
    while True:
        store: StateStore = app.state.store
        for row_id, payload, url in store.outbox_pending():
            code = await _post(app.state.http, url, payload)
            if code is None or 500 <= code:
                continue  # gateway still unhappy; next pass retries
            if code >= 400:
                log.error(
                    "outbox row %d rejected with %d — dropping (4xx is never retried)",
                    row_id,
                    code,
                )
            store.outbox_delete(row_id)
        await asyncio.sleep(interval)
