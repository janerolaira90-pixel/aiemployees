"""Owner approval gate for external / irreversible actions.

An approval stores the exact action and parameters plus a hash of both. Only that stored
action is ever executed — if an agent wants to change recipients, wording, or any parameter,
the tool call creates a new approval request.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from .agents.registry import get as get_agent
from .db import dumps, now_iso
from .integrations.telegram import esc
from .tasks import fmt_id

if TYPE_CHECKING:
    from .agents.registry import Agent
    from .app import App
    from .tasks import Task


def params_hash(action: str, params: dict) -> str:
    return hashlib.sha256(f"{action}\n{dumps(params)}".encode()).hexdigest()


def fmt_appr(approval_id: int) -> str:
    return f"A-{approval_id}"


def request_approval(app: "App", task: "Task", agent: "Agent", action: str, params: dict, summary: str) -> int:
    root = app.tasks.root(task)
    chat_id = root.chat_id or app.owner_chat_id
    appr_id = app.db.execute(
        """INSERT INTO approvals(task_id, agent_id, action, params, params_hash, summary, status, requested_at, chat_id)
           VALUES (?,?,?,?,?,?, 'pending', ?, ?)""",
        (task.id, agent.id, action, dumps(params), params_hash(action, params), summary, now_iso(), chat_id),
    )
    app.tasks.event(task.id, "approval:requested", f"{fmt_appr(appr_id)} {action}: {summary}")
    app.db.audit(agent.id, "approval_requested", {"approval": appr_id, "action": action, "task": task.id})
    if chat_id:
        text = (f"🔐 <b>Approval needed — {fmt_appr(appr_id)}</b>\n"
                f"{esc(agent.name)} ({fmt_id(task.id)}) wants to:\n<b>{esc(action)}</b>\n{esc(summary)}\n\n"
                f"<pre>{esc(json.dumps(params, ensure_ascii=False, indent=1)[:2500])}</pre>\n"
                "Only this exact action will run if approved.")
        mid = app.messenger.send(chat_id, text, buttons=[[("✅ Approve", f"ap:{appr_id}:y"),
                                                         ("❌ Reject", f"ap:{appr_id}:n")]])
        if mid:
            app.db.execute("UPDATE approvals SET message_id=? WHERE id=?", (mid, appr_id))
    return appr_id


def get(app: "App", approval_id: int) -> dict | None:
    row = app.db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
    return dict(row) if row else None


def pending(app: "App", task_ids: list[int] | None = None) -> list[dict]:
    sql, params = "SELECT * FROM approvals WHERE status='pending'", ()
    if task_ids is not None:
        if not task_ids:
            return []
        sql += f" AND task_id IN ({','.join('?' * len(task_ids))})"
        params = tuple(task_ids)
    return [dict(r) for r in app.db.query(sql + " ORDER BY id", params)]


def decide(app: "App", approval_id: int, approve: bool, decided_by: str) -> str:
    with app.db.tx() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if not row:
            return f"{fmt_appr(approval_id)} not found."
        if row["status"] != "pending":
            return f"{fmt_appr(approval_id)} is already {row['status']}."
        status = "approved" if approve else "rejected"
        conn.execute("UPDATE approvals SET status=?, decided_at=?, decided_by=? WHERE id=?",
                     (status, now_iso(), decided_by, approval_id))
    app.db.audit(decided_by, f"approval_{status}", {"approval": approval_id})
    app.tasks.event(row["task_id"], f"approval:{status}", f"{fmt_appr(approval_id)} {status} by {decided_by}")
    if approve:
        app.queue.enqueue("execute_approval", {"approval_id": approval_id}, dedupe_key=f"exec-approval:{approval_id}",
                          max_attempts=1)
        return f"{fmt_appr(approval_id)} approved — executing now."
    from .orchestration import deliver_input
    deliver_input(app, row["task_id"],
                  f"Owner REJECTED approval {fmt_appr(approval_id)} ({row['action']}). It was not executed. "
                  "Adjust your plan accordingly; do not retry the same action without new instructions.")
    return f"{fmt_appr(approval_id)} rejected. {get_agent(row['agent_id']).name} has been told."


def execute_approved(app: "App", approval_id: int) -> None:
    """Job handler: run exactly the approved action (verified by hash), record the outcome."""
    from .orchestration import deliver_input
    from .tools import REGISTRY, ToolContext

    appr = get(app, approval_id)
    if not appr or appr["status"] != "approved":
        return
    params = json.loads(appr["params"])
    task = app.tasks.get(appr["task_id"])
    agent = get_agent(appr["agent_id"])
    if params_hash(appr["action"], params) != appr["params_hash"]:
        _set(app, approval_id, "failed", {"error": "parameter hash mismatch — not executed"})
        deliver_input(app, task.id, f"Approved action {fmt_appr(approval_id)} was NOT executed: integrity check failed.")
        return
    spec = REGISTRY[appr["action"]]
    try:
        result = spec.handler(ToolContext(app=app, task=task, agent=agent, approved=True), params)
        _set(app, approval_id, "executed", result)
        msg = f"Approved action {fmt_appr(approval_id)} ({appr['action']}) was executed. Result: {dumps(result)[:3000]}"
        note = f"✅ {fmt_appr(approval_id)} executed."
    except Exception as exc:  # noqa: BLE001
        _set(app, approval_id, "failed", {"error": f"{type(exc).__name__}: {exc}"})
        msg = f"Approved action {fmt_appr(approval_id)} ({appr['action']}) FAILED: {type(exc).__name__}: {exc}"
        note = f"⚠️ {fmt_appr(approval_id)} failed: {esc(str(exc))[:500]}"
    if appr["chat_id"]:
        app.messenger.send(appr["chat_id"], note)
    deliver_input(app, task.id, msg)


def _set(app: "App", approval_id: int, status: str, result) -> None:
    app.db.execute("UPDATE approvals SET status=?, executed_at=?, result=? WHERE id=?",
                   (status, now_iso(), dumps(result), approval_id))
    app.db.audit("system", f"approval_{status}", {"approval": approval_id})
