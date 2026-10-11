"""SQLite persistence: tasks, conversations, jobs, approvals, deliverables, costs, audit.

SQLite in WAL mode is the smallest store that survives restarts and supports the
single-host deployment this system targets. Each thread gets its own connection.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id       INTEGER REFERENCES tasks(id),
    agent_id        TEXT NOT NULL,
    title           TEXT NOT NULL,
    instructions    TEXT NOT NULL DEFAULT '',
    client          TEXT,
    status          TEXT NOT NULL DEFAULT 'queued',
    priority        TEXT NOT NULL DEFAULT 'normal',
    deadline        TEXT,
    depends_on      TEXT NOT NULL DEFAULT '[]',
    requires        TEXT NOT NULL DEFAULT '[]',
    chat_id         INTEGER,
    created_by      TEXT NOT NULL DEFAULT 'owner',
    outcome         TEXT,
    result_summary  TEXT,
    result_links    TEXT NOT NULL DEFAULT '[]',
    error           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    completed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS task_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id   INTEGER NOT NULL REFERENCES tasks(id),
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    message   TEXT NOT NULL,
    data      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id);

-- Append-only model conversation per task (replayed verbatim, incl. thinking blocks).
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id   INTEGER NOT NULL REFERENCES tasks(id),
    role      TEXT NOT NULL,
    content   TEXT NOT NULL,
    sent      INTEGER NOT NULL DEFAULT 0,
    ts        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id);

-- Tool executions keyed by tool_use_id so a restart never silently re-runs a side effect.
CREATE TABLE IF NOT EXISTS tool_calls (
    tool_use_id TEXT PRIMARY KEY,
    task_id     INTEGER NOT NULL,
    agent_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    input       TEXT NOT NULL,
    status      TEXT NOT NULL,           -- started | done | error
    result      TEXT,
    is_error    INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    payload      TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | leased | done | failed
    run_at       TEXT NOT NULL,
    lease_until  TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error   TEXT,
    dedupe_key   TEXT UNIQUE,
    created_at   TEXT NOT NULL,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs(status, run_at);

CREATE TABLE IF NOT EXISTS approvals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          INTEGER NOT NULL REFERENCES tasks(id),
    agent_id         TEXT NOT NULL,
    action           TEXT NOT NULL,
    params           TEXT NOT NULL,
    params_hash      TEXT NOT NULL,
    summary          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|executed|failed|expired
    requested_at     TEXT NOT NULL,
    decided_at       TEXT,
    decided_by       TEXT,
    executed_at      TEXT,
    result           TEXT,
    chat_id          INTEGER,
    message_id       INTEGER
);

CREATE TABLE IF NOT EXISTS deliverables (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER NOT NULL REFERENCES tasks(id),
    kind         TEXT NOT NULL,           -- notion_page | notion_update | ghl_* | meta_* | file
    target       TEXT,                    -- e.g. "nula/content_ideas"
    external_id  TEXT,
    url          TEXT,
    title        TEXT,
    verified     INTEGER NOT NULL DEFAULT 0,
    meta         TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deliverables_task ON deliverables(task_id);

CREATE TABLE IF NOT EXISTS agent_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        INTEGER NOT NULL,
    agent_id       TEXT NOT NULL,
    model          TEXT NOT NULL,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_read     INTEGER NOT NULL DEFAULT 0,
    cache_write    INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL NOT NULL DEFAULT 0,
    stop_reason    TEXT,
    request_id     TEXT,
    ts             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT '{}'
);

-- Inputs waiting to be delivered into a task's conversation (subtask results, approval
-- decisions, owner replies). Drained by the runner, which holds the task lock.
CREATE TABLE IF NOT EXISTS inbox (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id   INTEGER NOT NULL REFERENCES tasks(id),
    text      TEXT NOT NULL,
    consumed  INTEGER NOT NULL DEFAULT 0,
    ts        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_locks (
    task_id   INTEGER PRIMARY KEY,
    owner     TEXT NOT NULL,
    until     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialized write transaction (BEGIN IMMEDIATE)."""
        conn = self.connect()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.connect().execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.connect().execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        with self.tx() as conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid

    # --- small helpers used across modules -------------------------------------------

    def audit(self, actor: str, action: str, detail: dict | None = None) -> None:
        self.execute(
            "INSERT INTO audit_log(ts, actor, action, detail) VALUES (?,?,?,?)",
            (now_iso(), actor, action, dumps(detail or {})),
        )

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
