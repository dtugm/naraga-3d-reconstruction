"""cascade_mesh through the real HTTP surface: submit -> download -> LOD2 -> PUT -> callbacks.

One MockTransport plays gateway (callbacks) and object storage (signed GETs of the
synthetic gable inputs, presigned PUT of the output).
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("geopandas")  # needs the `geo` extra: uv sync --extra geo

from reconstruction_3d import cascade_mesh  # noqa: E402
from reconstruction_3d.jobs import PREFIX  # noqa: E402
from reconstruction_3d.main import app  # noqa: E402

from .lod2_fixtures import EPSG, write_gable_inputs  # noqa: E402
from .test_jobs import AUTH, SAMPLE_REQUEST, _wait_for_status  # noqa: E402

PUT_URL = "http://storage.local/put/out"


def _dataset(name: str, fmt: str, role: str | None, size: int) -> dict[str, Any]:
    return {
        "dataset_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        "name": name,
        "dataset_format": fmt,
        "dataset_role": role,
        "size_bytes": size,
        "crs": f"EPSG:{EPSG}",
        "bbox": None,
        "signed_url": f"http://storage.local/get/{name}",
        "signed_url_expires_at": "2026-09-02T00:00:00.000Z",
    }


def _cascade_request(job_id: str, files: dict[str, Path]) -> dict[str, Any]:
    request = copy.deepcopy(SAMPLE_REQUEST)
    request["job_id"] = job_id
    request["model"] = "cascade_mesh"
    request["params"] = {
        "model": "cascade_mesh",
        "elevation_source": "dtm_dsm",
        "output_formats": ["gltf"],
    }
    request["output_prefix"] = f"jobs/{job_id}/outputs/"
    request["output_upload_urls"][0]["storage_key"] = f"jobs/{job_id}/outputs/lod2.city.json"
    request["input_datasets"] = {
        "building_outline": _dataset(
            "bo", "geojson", "building_outline", files["bo"].stat().st_size
        ),
        "roof_structure": _dataset("rs", "geojson", None, files["rs"].stat().st_size),
        "dsm": _dataset("dsm", "geotiff", "dsm", files["dsm"].stat().st_size),
        "dtm": _dataset("dtm", "geotiff", "dtm", files["dtm"].stat().st_size),
    }
    return request


def _install_world(files: dict[str, Path], events: list[dict[str, Any]], puts: list[bytes]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and url.startswith("http://storage.local/get/"):
            name = request.url.path.rsplit("/", 1)[1]
            if name not in files:
                return httpx.Response(403)  # what an expired/forged signature gets
            return httpx.Response(200, content=files[name].read_bytes())
        if request.method == "PUT" and url == PUT_URL:
            assert request.headers["content-length"] == str(len(request.content))
            puts.append(request.content)
            return httpx.Response(200)
        assert request.headers["authorization"] == "Bearer test-token"
        events.append(json.loads(request.content))
        return httpx.Response(200, json={"received": True})

    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_cascade_mesh_end_to_end(tmp_path: Path) -> None:
    files = write_gable_inputs(tmp_path)
    job_id = "00000000-0000-4000-8000-0000000000c1"
    events: list[dict[str, Any]] = []
    puts: list[bytes] = []

    with TestClient(app) as client:
        _install_world(files, events, puts)
        request = _cascade_request(job_id, files)
        assert client.post(f"{PREFIX}/jobs", json=request, headers=AUTH).status_code == 202
        status = _wait_for_status(client, job_id, "complete", timeout=30)
        assert status["status"] == "complete", events

    terminal = events[-1]
    assert terminal["status"] == "complete"
    progress = [e["progress_percent"] for e in events]
    assert progress == sorted(progress)

    (draft,) = terminal["output_datasets"]
    assert draft["storage_key"].startswith(request["output_prefix"])
    assert draft["dataset_role"] == "mesh"
    assert draft["crs"] == f"EPSG:{EPSG}"
    assert terminal["result_summary"] is None

    (body,) = puts
    assert draft["size_bytes"] == len(body)
    doc = json.loads(body)
    assert doc["type"] == "CityJSON" and doc["version"] == "1.1"
    assert len(doc["CityObjects"]) == 1


def test_cascade_mesh_without_roof_structure_fails_clearly(tmp_path: Path) -> None:
    files = write_gable_inputs(tmp_path)
    job_id = "00000000-0000-4000-8000-0000000000c2"
    events: list[dict[str, Any]] = []
    puts: list[bytes] = []

    with TestClient(app) as client:
        _install_world(files, events, puts)
        request = _cascade_request(job_id, files)
        del request["input_datasets"]["roof_structure"]
        assert client.post(f"{PREFIX}/jobs", json=request, headers=AUTH).status_code == 202
        _wait_for_status(client, job_id, "failed")

    terminal = events[-1]
    assert terminal["status"] == "failed"
    assert "roof_structure" in terminal["error_message"]
    assert puts == []


def test_download_failure_never_leaks_signed_url(tmp_path: Path) -> None:
    files = write_gable_inputs(tmp_path)
    job_id = "00000000-0000-4000-8000-0000000000c3"
    events: list[dict[str, Any]] = []
    puts: list[bytes] = []

    with TestClient(app) as client:
        _install_world(files, events, puts)
        request = _cascade_request(job_id, files)
        request["input_datasets"]["dsm"]["signed_url"] = (
            "http://storage.local/get/missing?sig=s3cr3t"
        )
        assert client.post(f"{PREFIX}/jobs", json=request, headers=AUTH).status_code == 202
        _wait_for_status(client, job_id, "failed")

    terminal = events[-1]
    assert terminal["status"] == "failed"
    assert "s3cr3t" not in terminal["error_message"]
    assert "dsm" in terminal["error_message"]


def test_cancel_mid_upload_never_lands_a_put(tmp_path: Path) -> None:
    """DELETE while the output PUT is in flight must abort the PUT, not let it finish.

    The storage stub holds each PUT open for a moment before recording it. Before the
    fix, cancelling the job left the upload task running, so the PUT still landed for
    a job the gateway already considered cancelled.
    """
    files = write_gable_inputs(tmp_path)
    job_id = "00000000-0000-4000-8000-0000000000c4"
    events: list[dict[str, Any]] = []
    puts: list[bytes] = []
    put_started = threading.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and url.startswith("http://storage.local/get/"):
            name = request.url.path.rsplit("/", 1)[1]
            return httpx.Response(200, content=files[name].read_bytes())
        if request.method == "PUT" and url == PUT_URL:
            put_started.set()
            await asyncio.sleep(0.5)  # the upload is in flight
            puts.append(request.content)
            return httpx.Response(200)
        events.append(json.loads(request.content))
        return httpx.Response(200, json={"received": True})

    with TestClient(app) as client:
        app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        request = _cascade_request(job_id, files)
        assert client.post(f"{PREFIX}/jobs", json=request, headers=AUTH).status_code == 202
        assert put_started.wait(timeout=30), "the job never reached the upload"

        assert client.delete(f"{PREFIX}/jobs/{job_id}", headers=AUTH).status_code == 202
        time.sleep(1.5)  # well past the moment the stub would have recorded the PUT

        assert puts == []
        assert all(e["status"] == "processing" for e in events)  # nothing after cancel


def test_too_many_invalid_buildings_fail_the_job() -> None:
    cascade_mesh._check_skipped(0, 0)  # an empty tile is LOD2Error's job, not this check
    cascade_mesh._check_skipped(5, 10)  # exactly the threshold still completes
    with pytest.raises(RuntimeError, match="6 of 10 buildings failed validation"):
        cascade_mesh._check_skipped(6, 10)
