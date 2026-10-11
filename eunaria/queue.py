"""Durable background job queue on SQLite with leases.

A job is leased by one worker at a time. If the process dies, the lease expires and the
job becomes runnable again (up to max_attempts), so work resumes after a restart.
"""

from __future__ import annotations

import json
import logging
import threading
import traceback
from datetime import datetime, timedelta, timezone
from typing import Callable

from .db import Database, dumps, now_iso

log = logging.getLogger(__name__)

Handler = Callable[[dict], None]


class Defer(Exception):
    """Raised by a handler to retry later without counting a failed attempt (e.g. task locked)."""

    def __init__(self, seconds: float = 5.0):
        super().__init__(f"deferred {seconds}s")
        self.seconds = seconds


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class JobQueue:
    def __init__(self, db: Database, lease_seconds: int = 900):
        self.db = db
        self.lease_seconds = lease_seconds
        self.wakeup = threading.Event()

    def enqueue(self, kind: str, payload: dict | None = None, *, dedupe_key: str | None = None,
                run_at: datetime | None = None, max_attempts: int = 3) -> int | None:
        """Insert a job. Returns None when a job with the same dedupe_key already exists."""
        with self.db.tx() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO jobs(kind, payload, status, run_at, max_attempts, dedupe_key, created_at)
                   VALUES (?,?,'pending',?,?,?,?)""",
                (kind, dumps(payload or {}), _iso(run_at or datetime.now(timezone.utc)), max_attempts,
                 dedupe_key, now_iso()),
            )
            job_id = cur.lastrowid if cur.rowcount else None
        if job_id:
            self.wakeup.set()
        return job_id

    def recover(self) -> int:
        """Return expired leases to the pending pool (called at startup and periodically)."""
        with self.db.tx() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status='pending', lease_until=NULL WHERE status='leased' AND lease_until < ?",
                (now_iso(),),
            )
            return cur.rowcount

    def lease(self) -> dict | None:
        now = datetime.now(timezone.utc)
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status='pending' AND run_at <= ? ORDER BY run_at, id LIMIT 1",
                (_iso(now),),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE jobs SET status='leased', lease_until=?, attempts=attempts+1 WHERE id=?",
                (_iso(now + timedelta(seconds=self.lease_seconds)), row["id"]),
            )
        job = dict(row)
        job["attempts"] += 1
        job["payload"] = json.loads(job["payload"])
        return job

    def complete(self, job_id: int) -> None:
        self.db.execute("UPDATE jobs SET status='done', finished_at=?, lease_until=NULL WHERE id=?", (now_iso(), job_id))

    def fail(self, job: dict, error: str, retry_in: int = 30) -> bool:
        """Record a failure. Returns True if the job will be retried."""
        final = job["attempts"] >= job["max_attempts"]
        if final:
            self.db.execute(
                "UPDATE jobs SET status='failed', last_error=?, finished_at=?, lease_until=NULL WHERE id=?",
                (error[-4000:], now_iso(), job["id"]),
            )
        else:
            backoff = retry_in * (2 ** (job["attempts"] - 1))
            self.db.execute(
                "UPDATE jobs SET status='pending', last_error=?, lease_until=NULL, run_at=? WHERE id=?",
                (error[-4000:], _iso(datetime.now(timezone.utc) + timedelta(seconds=backoff)), job["id"]),
            )
        return not final

    def defer(self, job: dict, seconds: float) -> None:
        self.db.execute(
            "UPDATE jobs SET status='pending', lease_until=NULL, attempts=attempts-1, run_at=? WHERE id=?",
            (_iso(datetime.now(timezone.utc) + timedelta(seconds=seconds)), job["id"]),
        )

    def has_pending(self, kind: str, key: str, value) -> bool:
        row = self.db.one(
            f"SELECT 1 FROM jobs WHERE kind=? AND status='pending' AND json_extract(payload, '$.{key}')=? LIMIT 1",
            (kind, value),
        )
        return row is not None

    def pending_count(self) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('pending','leased')")["n"]


class Worker:
    """Pulls jobs and dispatches them to handlers. Run several in threads."""

    def __init__(self, queue: JobQueue, handlers: dict[str, Handler],
                 on_final_failure: Callable[[dict, str], None] | None = None):
        self.queue = queue
        self.handlers = handlers
        self.on_final_failure = on_final_failure
        self.stop = threading.Event()

    def run_once(self) -> bool:
        job = self.queue.lease()
        if not job:
            return False
        handler = self.handlers.get(job["kind"])
        try:
            if handler is None:
                raise RuntimeError(f"No handler for job kind {job['kind']!r}")
            handler(job["payload"])
        except Defer as d:
            self.queue.defer(job, d.seconds)
        except Exception as exc:  # noqa: BLE001 - every failure is recorded, never swallowed silently
            err = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}"
            log.error("job %s (%s) failed: %s", job["id"], job["kind"], exc)
            retrying = self.queue.fail(job, err)
            if not retrying and self.on_final_failure:
                try:
                    self.on_final_failure(job, f"{type(exc).__name__}: {exc}")
                except Exception:  # noqa: BLE001
                    log.exception("on_final_failure hook failed")
        else:
            self.queue.complete(job["id"])
        return True

    def drain(self, max_jobs: int = 1000) -> int:
        """Process jobs until none are ready (used by tests and the CLI)."""
        n = 0
        while n < max_jobs and self.run_once():
            n += 1
        return n

    def loop(self, idle_wait: float = 2.0) -> None:
        while not self.stop.is_set():
            if not self.run_once():
                self.queue.wakeup.wait(idle_wait)
                self.queue.wakeup.clear()
