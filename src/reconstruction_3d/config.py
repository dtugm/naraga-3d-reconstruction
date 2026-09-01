from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Every env var is declared here — nothing reads os.environ directly."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "reconstruction_3d"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Shared secret for gateway<->service calls: verified on every inbound request,
    # attached as a Bearer token to every outbound callback. NO default on purpose —
    # a missing secret must crash at boot, never fall back to a guessable value.
    # (cp .env.example .env for local dev.)
    internal_service_token: str

    # Durable job state (idempotency + terminal-callback outbox). Mount the parent
    # directory on a volume: pod rescheduling loses un-mounted container disk.
    state_db_path: str = "data/state.db"
    # How often the drainer retries unacknowledged terminal callbacks.
    outbox_drain_interval_seconds: float = 30.0


@lru_cache
def get_settings() -> Settings:
    # call-arg ignored: internal_service_token is loaded from the environment/.env at
    # runtime; instantiating without it MUST raise — that is the crash-at-boot rule.
    return Settings()  # type: ignore[call-arg]
