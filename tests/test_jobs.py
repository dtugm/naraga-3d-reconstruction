"""End-to-end test of the worked example: submit -> callbacks -> terminal state.

The gateway is faked with an httpx.MockTransport swapped into app.state.http, so the
full callback path (auth header, sequencing, timestamps) is exercised for real.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx
from fastapi.testclient import TestClient

from reconstruction_3d.jobs import PREFIX
from reconstruction_3d.main import app

AUTH = {"Authorization": "Bearer test-token"}  # set by tests/conftest.py
RFC3339_MS_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

SAMPLE_REQUEST: dict[str, Any] = {
    "job_id": "00000000-0000-4000-8000-000000000001",
    "service": "reconstruction_3d",
    "model": "dream3d",
    "input_datasets": {
        "building_outline": {
            "dataset_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            "name": "bo",
            "dataset_format": "geojson",
            "dataset_role": "building_outline",
            "size_bytes": 2048,
            "crs": "EPSG:4326",
            "bbox": [110.36, -7.82, 110.42, -7.78],
            "signed_url": "http://s/b",
            "signed_url_expires_at": "2026-09-02T00:00:00.000Z",
        },
        "point_cloud": {
            "dataset_id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
            "name": "pc",
            "dataset_format": "las",
            "dataset_role": "point_cloud",
            "size_bytes": 209715200,
            "crs": "EPSG:32749",
            "bbox": [110.36, -7.82, 110.42, -7.78],
            "signed_url": "http://s/p",
            "signed_url_expires_at": "2026-09-02T00:00:00.000Z",
        },
    },
    "params": {"model": "dream3d", "elevation_source": "pointcloud", "output_formats": ["gltf"]},
    "output_prefix": "jobs/00000000-0000-4000-8000-000000000001/outputs/",
    "output_upload_urls": [
        {
            "output_format": "gltf",
            "storage_key": "jobs/00000000-0000-4000-8000-000000000001/outputs/out.gltf",
            "url": "http://storage.local/put/out",
            "expires_at": "2026-09-02T00:00:00.000Z",
        }
    ],
    "callback_url": "http://gateway.invalid/v1/internal/jobs/00000000-0000-4000-8000-000000000001/callback",
    "max_job_duration_seconds": 10800,
    "heartbeat_interval_seconds": 30,
}


def _sample(job_id: str) -> dict[str, Any]:
    return {**SAMPLE_REQUEST, "job_id": job_id}


def _install_gateway_sink(events: list[dict[str, Any]]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-token"
        events.append(json.loads(request.content))
        return httpx.Response(200, json={"received": True})

    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _wait_for_status(
    client: TestClient, job_id: str, wanted: str, timeout: float = 5.0
) -> dict[str, Any]:
    deadline = time.time() + timeout
    body: dict[str, Any] = {}
    while time.time() < deadline:
        body = client.get(f"{PREFIX}/jobs/{job_id}/status", headers=AUTH).json()
        if body["status"] == wanted:
            return body
        time.sleep(0.02)
    return body


def test_job_flow_end_to_end() -> None:
    events: list[dict[str, Any]] = []
    with TestClient(app) as client:
        _install_gateway_sink(events)
        job_id = str(SAMPLE_REQUEST["job_id"])

        r = client.post(f"{PREFIX}/jobs", json=SAMPLE_REQUEST, headers=AUTH)
        assert r.status_code == 202
        accepted = r.json()
        assert accepted["accepted"] is True
        assert accepted["job_id"] == job_id
        assert accepted["credits_estimated"] >= 1

        status = _wait_for_status(client, job_id, "complete")
        assert status == {"job_id": job_id, "status": "complete", "progress_percent": 100}

    # Callback stream: starts processing, ends complete, sequence strictly monotonic,
    # timestamps in the exact RFC3339-ms-Z shape the gateway accepts.
    assert [e["status"] for e in events][0] == "processing"
    assert events[-1]["status"] == "complete"
    sequences = [e["sequence"] for e in events]
    assert sequences == sorted(set(sequences))
    assert all(RFC3339_MS_Z.match(e["emitted_at"]) for e in events)

    terminal = events[-1]
    assert terminal["progress_percent"] == 100
    assert isinstance(terminal["credits_used"], int)
    assert len(terminal["output_datasets"]) == 1
    draft = terminal["output_datasets"][0]
    assert draft["storage_key"].startswith(SAMPLE_REQUEST["output_prefix"])
    assert set(draft) == {
        "name",
        "dataset_format",
        "dataset_role",
        "storage_key",
        "size_bytes",
        "crs",
        "bbox",
    }


def test_submit_is_idempotent_on_job_id() -> None:
    events: list[dict[str, Any]] = []
    with TestClient(app) as client:
        _install_gateway_sink(events)
        payload = _sample("00000000-0000-4000-8000-000000000002")
        assert client.post(f"{PREFIX}/jobs", json=payload, headers=AUTH).status_code == 202
        r = client.post(f"{PREFIX}/jobs", json=payload, headers=AUTH)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "CONFLICT"


def test_cancel_stops_callbacks() -> None:
    events: list[dict[str, Any]] = []
    with TestClient(app) as client:
        _install_gateway_sink(events)
        payload = _sample("00000000-0000-4000-8000-000000000003")
        assert client.post(f"{PREFIX}/jobs", json=payload, headers=AUTH).status_code == 202
        r = client.delete(f"{PREFIX}/jobs/{payload['job_id']}", headers=AUTH)
        assert r.status_code == 202
        time.sleep(0.3)  # long enough for the job to have finished were it not cancelled
    assert all(e["status"] != "complete" for e in events)


def test_rejects_missing_or_wrong_token() -> None:
    with TestClient(app) as client:
        r = client.post(f"{PREFIX}/jobs", json=SAMPLE_REQUEST)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "UNAUTHENTICATED"
        r = client.get(f"{PREFIX}/capabilities", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401


def test_validation_error_is_contract_shaped() -> None:
    with TestClient(app) as client:
        r = client.post(f"{PREFIX}/jobs", json={"job_id": "not-even-a-uuid"}, headers=AUTH)
        assert r.status_code == 422
        body = r.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert RFC3339_MS_Z.match(body["meta"]["timestamp"])


def test_estimate() -> None:
    with TestClient(app) as client:
        r = client.post(
            f"{PREFIX}/estimate",
            json={
                "input_datasets": SAMPLE_REQUEST["input_datasets"],
                "params": SAMPLE_REQUEST["params"],
            },
            headers=AUTH,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["credits_estimated"] >= 1
        assert body["breakdown"]["total"] == body["credits_estimated"]


def test_capabilities() -> None:
    with TestClient(app) as client:
        r = client.get(f"{PREFIX}/capabilities", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["service"] == "reconstruction_3d"
        assert body["output_formats"] == ["gltf", "3dtiles"]
        assert body["max_input_size_bytes"] > 0


def test_restart_recovers_orphans_and_keeps_idempotency() -> None:
    """Two TestClient blocks against one state db = a service restart. The first
    'process' dies mid-job; the second must 409 the redelivered submit, report the
    orphan as failed, and drain its terminal callback from the outbox."""
    events: list[dict[str, Any]] = []
    payload = _sample("00000000-0000-4000-8000-000000000004")
    with TestClient(app) as client:
        _install_gateway_sink(events)
        assert client.post(f"{PREFIX}/jobs", json=payload, headers=AUTH).status_code == 202
        # exit immediately: the process dies while the job is still 'processing'
    events.clear()
    with TestClient(app) as client:
        _install_gateway_sink(events)
        assert client.post(f"{PREFIX}/jobs", json=payload, headers=AUTH).status_code == 409
        status = client.get(f"{PREFIX}/jobs/{payload['job_id']}/status", headers=AUTH).json()
        assert status["status"] == "failed"
        deadline = time.time() + 5
        while time.time() < deadline:
            if any(e["status"] == "failed" and e["job_id"] == payload["job_id"] for e in events):
                break
            time.sleep(0.02)
        else:
            raise AssertionError(f"orphan failure callback never delivered: {events}")


def test_terminal_callback_survives_gateway_outage() -> None:
    """The gateway 503s the first two terminal deliveries; the outbox drainer must
    keep redelivering (same sequence) until the ack lands — exactly once."""
    events: list[dict[str, Any]] = []
    outage = {"remaining": 2}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["status"] == "complete" and outage["remaining"] > 0:
            outage["remaining"] -= 1
            return httpx.Response(503)
        events.append(body)
        return httpx.Response(200, json={"received": True})

    with TestClient(app) as client:
        app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        payload = _sample("00000000-0000-4000-8000-000000000005")
        assert client.post(f"{PREFIX}/jobs", json=payload, headers=AUTH).status_code == 202
        deadline = time.time() + 5
        while time.time() < deadline and not any(e["status"] == "complete" for e in events):
            time.sleep(0.02)
    complete = [e for e in events if e["status"] == "complete"]
    assert len(complete) == 1
    assert outage["remaining"] == 0
