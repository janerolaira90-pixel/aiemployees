"""Task records: the shared state agents coordinate through (not chat history)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .db import Database, dumps, now_iso

ACTIVE = ("queued", "running", "waiting", "awaiting_approval", "blocked")
TERMINAL = ("done", "failed", "cancelled")
STATUSES = ACTIVE + TERMINAL


def fmt_id(task_id: int) -> str:
    return f"T-{task_id:04d}"


def parse_id(text: str) -> int | None:
    t = text.strip().upper().removeprefix("T-").removeprefix("#")
    return int(t) if t.isdigit() else None


@dataclass
class Task:
    id: int
    parent_id: int | None
    agent_id: str
    title: str
    instructions: str
    client: str | None
    status: str
    priority: str
    deadline: str | None
    depends_on: list[int]
    requires: list[dict]
    chat_id: int | None
    created_by: str
    outcome: str | None
    result_summary: str | None
    result_links: list[str]
    error: str | None
    created_at: str
    updated_at: str
    completed_at: str | None

    @property
    def ref(self) -> str:
        return fmt_id(self.id)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    def is_overdue(self, today: date) -> bool:
        if not self.deadline or self.is_terminal:
            return False
        try:
            return date.fromisoformat(self.deadline[:10]) < today
        except ValueError:
            return False

    def brief(self) -> dict[str, Any]:
        return {
            "id": self.ref, "agent": self.agent_id, "title": self.title, "client": self.client,
            "status": self.status, "priority": self.priority, "deadline": self.deadline,
            "parent": fmt_id(self.parent_id) if self.parent_id else None,
            "outcome": self.outcome,
        }


def _row_to_task(row) -> Task:
    return Task(
        id=row["id"], parent_id=row["parent_id"], agent_id=row["agent_id"], title=row["title"],
        instructions=row["instructions"], client=row["client"], status=row["status"],
        priority=row["priority"], deadline=row["deadline"], depends_on=json.loads(row["depends_on"]),
        requires=json.loads(row["requires"]), chat_id=row["chat_id"], created_by=row["created_by"],
        outcome=row["outcome"], result_summary=row["result_summary"],
        result_links=json.loads(row["result_links"]), error=row["error"],
        created_at=row["created_at"], updated_at=row["updated_at"], completed_at=row["completed_at"],
    )


class TaskStore:
    def __init__(self, db: Database):
        self.db = db

    def create(
        self, *, agent_id: str, title: str, instructions: str = "", client: str | None = None,
        parent_id: int | None = None, priority: str = "normal", deadline: str | None = None,
        depends_on: list[int] | None = None, requires: list[dict] | None = None,
        chat_id: int | None = None, created_by: str = "owner",
    ) -> Task:
        ts = now_iso()
        with self.db.tx() as conn:
            cur = conn.execute(
                """INSERT INTO tasks(parent_id, agent_id, title, instructions, client, status, priority,
                   deadline, depends_on, requires, chat_id, created_by, created_at, updated_at)
                   VALUES (?,?,?,?,?,'queued',?,?,?,?,?,?,?,?)""",
                (parent_id, agent_id, title, instructions, client, priority, deadline,
                 dumps(depends_on or []), dumps(requires or []), chat_id, created_by, ts, ts),
            )
            task_id = cur.lastrowid
            conn.execute(
                "INSERT INTO task_events(task_id, ts, kind, message, data) VALUES (?,?,?,?,?)",
                (task_id, ts, "created", f"Assigned to {agent_id} by {created_by}", "{}"),
            )
        return self.get(task_id)

    def get(self, task_id: int) -> Task | None:
        row = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        return _row_to_task(row) if row else None

    def children(self, task_id: int) -> list[Task]:
        return [_row_to_task(r) for r in self.db.query("SELECT * FROM tasks WHERE parent_id=? ORDER BY id", (task_id,))]

    def subtree_ids(self, task_id: int) -> list[int]:
        rows = self.db.query(
            """WITH RECURSIVE t(id) AS (SELECT ? UNION ALL SELECT tasks.id FROM tasks JOIN t ON tasks.parent_id=t.id)
               SELECT id FROM t""",
            (task_id,),
        )
        return [r["id"] for r in rows]

    def root(self, task: Task) -> Task:
        while task.parent_id:
            task = self.get(task.parent_id)
        return task

    def list(self, *, statuses: tuple[str, ...] | None = None, agent_id: str | None = None,
             client: str | None = None, roots_only: bool = False, limit: int = 50) -> list[Task]:
        sql, params = "SELECT * FROM tasks WHERE 1=1", []
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            params += list(statuses)
        if agent_id:
            sql += " AND agent_id=?"
            params.append(agent_id)
        if client:
            sql += " AND client=?"
            params.append(client)
        if roots_only:
            sql += " AND parent_id IS NULL"
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [_row_to_task(r) for r in self.db.query(sql, tuple(params))]

    def overdue(self, today: date) -> list[Task]:
        return [t for t in self.list(statuses=ACTIVE, limit=500) if t.is_overdue(today)]

    def set_status(self, task_id: int, status: str, *, message: str | None = None, **fields: Any) -> Task:
        assert status in STATUSES, status
        ts = now_iso()
        cols = {"status": status, "updated_at": ts}
        if status in TERMINAL:
            cols["completed_at"] = ts
        for key, value in fields.items():
            cols[key] = dumps(value) if key in ("result_links", "depends_on", "requires") else value
        assignments = ", ".join(f"{k}=?" for k in cols)
        with self.db.tx() as conn:
            conn.execute(f"UPDATE tasks SET {assignments} WHERE id=?", (*cols.values(), task_id))
            conn.execute(
                "INSERT INTO task_events(task_id, ts, kind, message, data) VALUES (?,?,?,?,?)",
                (task_id, ts, f"status:{status}", message or status, "{}"),
            )
        return self.get(task_id)

    def event(self, task_id: int, kind: str, message: str, data: dict | None = None) -> None:
        self.db.execute(
            "INSERT INTO task_events(task_id, ts, kind, message, data) VALUES (?,?,?,?,?)",
            (task_id, now_iso(), kind, message, dumps(data or {})),
        )

    def events(self, task_id: int, limit: int = 20) -> list[dict]:
        rows = self.db.query(
            "SELECT ts, kind, message FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit)
        )
        return [dict(r) for r in reversed(rows)]

    # --- deliverables -------------------------------------------------------------------

    def add_deliverable(self, task_id: int, *, kind: str, target: str | None, external_id: str | None,
                        url: str | None, title: str | None, verified: bool, meta: dict | None = None) -> int:
        return self.db.execute(
            """INSERT INTO deliverables(task_id, kind, target, external_id, url, title, verified, meta, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (task_id, kind, target, external_id, url, title, int(verified), dumps(meta or {}), now_iso()),
        )

    def deliverables(self, task_ids: list[int]) -> list[dict]:
        if not task_ids:
            return []
        rows = self.db.query(
            f"SELECT * FROM deliverables WHERE task_id IN ({','.join('?' * len(task_ids))}) ORDER BY id",
            tuple(task_ids),
        )
        return [dict(r) for r in rows]

    def unmet_requirements(self, task: Task) -> list[str]:
        """Check declared deliverable requirements against *verified* records in the task subtree.

        A requirement looks like {"kind": "notion_page", "target": "nula/content_ideas", "min": 20}.
        """
        if not task.requires:
            return []
        found = [d for d in self.deliverables(self.subtree_ids(task.id)) if d["verified"]]
        unmet = []
        for req in task.requires:
            kind, target, need = req.get("kind"), req.get("target"), int(req.get("min", 1))
            have = sum(1 for d in found if d["kind"] == kind and (not target or d["target"] == target))
            if have < need:
                unmet.append(f"{kind}{' → ' + target if target else ''}: {have}/{need} verified")
        return unmet


def today_in(tz: str) -> date:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(tz)).date()
