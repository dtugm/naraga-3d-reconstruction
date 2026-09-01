"""Test environment. Runs before any test module imports the app.

INTERNAL_SERVICE_TOKEN has no default (missing secret == crash at boot), so tests
provide one here. Environment variables beat .env in pydantic-settings, so a local
.env cannot leak a different token into the suite.
"""

import os

os.environ["INTERNAL_SERVICE_TOKEN"] = "test-token"

from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from reconstruction_3d.config import get_settings  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test gets its own SQLite file and a fast outbox drainer.

    The store opens inside the app lifespan, so a `with TestClient(app)` block is
    one 'process lifetime' — two consecutive blocks in one test simulate a restart
    against the same state db.
    """
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("OUTBOX_DRAIN_INTERVAL_SECONDS", "0.05")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
