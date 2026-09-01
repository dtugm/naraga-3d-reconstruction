import re
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from reconstruction_3d.contract.models import JobCallback
from reconstruction_3d.jobs import CONTRACT_VERSION, MODELS, OUTPUT_FORMATS
from reconstruction_3d.main import app

client = TestClient(app)


def test_health() -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ready() -> None:
    r = client.get("/ready")
    assert r.status_code in (200, 503)
    body = r.json()
    assert body["status"] in ("ok", "unavailable")
    assert isinstance(body["checks"], dict)


def test_generated_models_round_trip() -> None:
    """The generated models must be importable AND semantically usable — the CI byte
    diff alone cannot catch a generator release that emits self-inconsistent code."""
    payload = {
        "job_id": "00000000-0000-4000-8000-00000000abcd",
        "sequence": 3,
        "emitted_at": "2026-09-01T03:00:00.000Z",
        "status": "processing",
        "progress_percent": 40,
        "output_datasets": [],
        "result_summary": None,
        "credits_used": None,
        "error_code": None,
        "error_message": None,
    }
    cb = JobCallback.model_validate(payload)
    dumped = cb.model_dump(mode="json")
    assert str(dumped["job_id"]) == payload["job_id"]
    assert dumped["sequence"] == 3
    assert dumped["status"] == "processing"


def test_contract_version_matches_vendored_spec() -> None:
    """capabilities.contract_version is a constant; a contract sync PR that bumps the
    vendored spec without this constant moving must fail CI here."""
    spec = (Path(__file__).parent.parent / "contract" / "openapi.yaml").read_text()
    m = re.search(r"x-contract-version:\s*([0-9][0-9.]*)", spec)
    assert m is not None
    assert CONTRACT_VERSION == m.group(1)


def test_capabilities_match_vendored_registry() -> None:
    """MODELS/OUTPUT_FORMATS are baked in at scaffold time, but the registry arrives by
    contract sync PR. Without this cross-check, adding a model to services.yaml would
    land while /capabilities keeps advertising the old list — and CI would stay green."""
    registry = yaml.safe_load(
        (Path(__file__).parent.parent / "contract" / "services.yaml").read_text()
    )
    entry = registry["services"]["reconstruction_3d"]
    assert MODELS == entry["models"]
    assert OUTPUT_FORMATS == entry["output_formats"]
