"""Durable job state — SQLite, one file per service instance.

Two tables cover everything the contract requires to survive a restart:
  jobs    — idempotency on job_id (a redelivered submit 409s after a reboot),
            status/progress for the reaper's GET /status, the per-job callback
            sequence counter, and the callback_url (needed to report orphans).
  outbox  — terminal callbacks not yet acknowledged by the gateway. The contract
            mandates these survive a restart and resume retrying; see the drainer
            in callbacks.py.

Deliberately synchronous: at this write rate (a few writes per second per replica,
each microseconds-to-low-milliseconds) blocking the event loop is harmless. The
PIPELINE is a different story — see run_job's docstring about asyncio.to_thread.

Mount /app/data on a volume: a container restart keeps local disk, but a pod being
rescheduled to another node does not.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .callbacks import rfc3339_now

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    progress      INTEGER NOT NULL DEFAULT 0,
    cancelled     INTEGER NOT NULL DEFAULT 0,
    callback_url  TEXT NOT NULL,
    last_sequence INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


class StateStore:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: created under the lifespan, used from the event
        # loop — effectively single-threaded access, never concurrent.
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        # FULL, not NORMAL: the outbox exists to survive power loss, and at this
        # write rate the extra fsync cost is unmeasurable.
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ---- jobs ----

    def insert_job(self, job_id: str, callback_url: str) -> bool:
        """False if the job_id was already accepted (idempotency, restart-proof)."""
        try:
            self._conn.execute(
                "INSERT INTO jobs (job_id, status, callback_url, created_at)"
                " VALUES (?, 'processing', ?, ?)",
                (job_id, callback_url, rfc3339_now()),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT status, progress, cancelled, callback_url FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "status": row[0],
            "progress_percent": row[1],
            "cancelled": bool(row[2]),
            "callback_url": row[3],
        }

    def set_status(self, job_id: str, status: str, progress: int) -> None:
        self._conn.execute(
            "UPDATE jobs SET status = ?, progress = ? WHERE job_id = ?",
            (status, progress, job_id),
        )

    def set_cancelled(self, job_id: str) -> None:
        self._conn.execute("UPDATE jobs SET cancelled = 1 WHERE job_id = ?", (job_id,))

    def next_sequence(self, job_id: str) -> int:
        row = self._conn.execute(
            "UPDATE jobs SET last_sequence = last_sequence + 1"
            " WHERE job_id = ? RETURNING last_sequence",
            (job_id,),
        ).fetchone()
        return int(row[0])

    def orphaned_processing(self) -> list[str]:
        """Jobs left 'processing' by a previous life of this process — their asyncio
        tasks died with it, so no callback will ever come. Recovery reports them
        failed rather than leaving the gateway to wait for the reaper timeout.
        Cancelled jobs are excluded: the contract forbids further callbacks after
        a cancel, and the gateway would ignore them anyway."""
        rows = self._conn.execute(
            "SELECT job_id FROM jobs WHERE status = 'processing' AND cancelled = 0"
        ).fetchall()
        return [str(r[0]) for r in rows]

    # ---- outbox ----

    def outbox_add(self, job_id: str, payload: dict[str, Any]) -> int:
        cur = self._conn.execute(
            "INSERT INTO outbox (job_id, payload) VALUES (?, ?)",
            (job_id, json.dumps(payload)),
        )
        return int(cur.lastrowid or 0)

    def outbox_delete(self, row_id: int) -> None:
        self._conn.execute("DELETE FROM outbox WHERE id = ?", (row_id,))

    def outbox_pending(self) -> list[tuple[int, dict[str, Any], str]]:
        """(row_id, payload, callback_url) for every unacknowledged terminal callback."""
        rows = self._conn.execute(
            "SELECT o.id, o.payload, j.callback_url FROM outbox o"
            " JOIN jobs j ON j.job_id = o.job_id ORDER BY o.id"
        ).fetchall()
        return [(int(r[0]), json.loads(r[1]), str(r[2])) for r in rows]
