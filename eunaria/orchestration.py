"""Task lifecycle: starting, dependency handling, parent resumption, completion, notifications."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .agents.registry import get as get_agent
from .integrations.telegram import esc
from .tasks import TERMINAL, fmt_id

if TYPE_CHECKING:
    from .app import App
    from .tasks import Task

log = logging.getLogger(__name__)

OUTCOME_ICON = {"completed": "✅", "partial": "🟡", "failed": "❌"}


def enqueue_run(app: "App", task_id: int) -> None:
    """Schedule the agent for a task, unless a run is already waiting in the queue."""
    if not app.queue.has_pending("run_task", "task_id", task_id):
        app.queue.enqueue("run_task", {"task_id": task_id}, max_attempts=3)


def maybe_start(app: "App", task_id: int) -> bool:
    task = app.tasks.get(task_id)
    if task.status != "queued":
        return False
    deps = [app.tasks.get(d) for d in task.depends_on]
    failed = [d for d in deps if d.status in ("failed", "cancelled") or d.outcome == "failed"]
    if failed:
        cancel_tree(app, task.id, "Upstream task(s) failed: " + ", ".join(d.ref for d in failed))
        return False
    if any(not d.is_terminal for d in deps):
        return False
    if deps:
        lines = ["Results from upstream tasks you depend on:"]
        for d in deps:
            lines.append(f"\n[{d.ref} · {get_agent(d.agent_id).name} · {d.outcome}] {d.title}\n{d.result_summary or ''}")
            if d.result_links:
                lines.append("Links: " + ", ".join(d.result_links))
        app.inbox.put(task.id, "\n".join(lines))
    enqueue_run(app, task.id)
    return True


def deliver_input(app: "App", task_id: int, text: str, *, reopen: bool = False) -> None:
    """Queue a message for a task's agent (subtask results, approval decisions, owner replies)."""
    task = app.tasks.get(task_id)
    if task.is_terminal and not reopen:
        app.tasks.event(task_id, "input:ignored", "Input arrived after completion: " + text[:200])
        return
    if task.is_terminal and reopen:
        app.tasks.set_status(task_id, "queued", message="Reopened by owner reply", outcome=None, error=None,
                             completed_at=None)
    app.inbox.put(task_id, text)
    enqueue_run(app, task_id)


def finalize_task(app: "App", task_id: int, *, outcome: str, summary: str, links: list[str]) -> None:
    from .approvals import pending
    if pending(app, [task_id]):
        app.tasks.set_status(task_id, "awaiting_approval", message="Result submitted; approvals pending",
                             outcome=outcome, result_summary=summary, result_links=links)
        return
    status = "failed" if outcome == "failed" else "done"
    app.tasks.set_status(task_id, status, message=f"Submitted ({outcome})", outcome=outcome,
                         result_summary=summary, result_links=links)
    on_terminal(app, task_id)


def block_task(app: "App", task_id: int, reason: str) -> None:
    task = app.tasks.set_status(task_id, "blocked", message="Blocked", error=reason)
    chat = app.tasks.root(task).chat_id or app.owner_chat_id
    if chat:
        mid = app.messenger.send(chat, f"⛔ <b>{task.ref} · {esc(get_agent(task.agent_id).name)} is blocked</b>\n"
                                       f"{esc(task.title)}\n\n{esc(reason)}\n\n"
                                       f"Reply to this message with what's needed, or /cancel {task.ref}.")
        _remember_message(app, chat, task.id, mid)
    if task.parent_id:
        deliver_input(app, task.parent_id, f"Subtask {task.ref} ({task.agent_id}) is BLOCKED: {reason}")


def fail_task(app: "App", task_id: int, error: str) -> None:
    task = app.tasks.get(task_id)
    if task.is_terminal:
        return
    app.tasks.set_status(task_id, "failed", message="Failed", outcome="failed", error=error,
                         result_summary=f"Failed: {error}")
    on_terminal(app, task_id)


def cancel_tree(app: "App", task_id: int, reason: str) -> int:
    n = 0
    for tid in reversed(app.tasks.subtree_ids(task_id)):
        t = app.tasks.get(tid)
        if not t.is_terminal:
            app.tasks.set_status(tid, "cancelled", message=reason, error=reason, outcome="failed")
            n += 1
    app.db.execute("UPDATE approvals SET status='expired' WHERE status='pending' AND task_id IN (%s)"
                   % ",".join(str(i) for i in app.tasks.subtree_ids(task_id)))
    on_terminal(app, task_id, notify=False)
    return n


def on_terminal(app: "App", task_id: int, notify: bool = True) -> None:
    task = app.tasks.get(task_id)
    if task.parent_id is None:
        if notify:
            notify_owner(app, task)
        return
    # Start siblings that were waiting on this task.
    for sib in app.tasks.children(task.parent_id):
        if task.id in sib.depends_on and sib.status == "queued":
            maybe_start(app, sib.id)
    if notify and app.settings.progress_updates:
        root = app.tasks.root(task)
        chat = root.chat_id or app.owner_chat_id
        if chat:
            icon = OUTCOME_ICON.get(task.outcome or "", "⏹")
            first = (task.result_summary or task.error or "").strip().split("\n")[0][:220]
            app.messenger.send(chat, f"{icon} {task.ref} · {esc(get_agent(task.agent_id).name)} — "
                                     f"{esc(task.title)}\n<i>{esc(first)}</i>")
    siblings = app.tasks.children(task.parent_id)
    if all(s.is_terminal for s in siblings):
        parent = app.tasks.get(task.parent_id)
        if parent.is_terminal:
            return
        lines = ["All subtasks have finished. Review each result critically before reporting:"]
        for s in siblings:
            lines.append(f"\n[{s.ref} · {get_agent(s.agent_id).name} · {s.status}/{s.outcome}] {s.title}\n"
                         f"{s.result_summary or s.error or '(no summary)'}")
            links = _all_links(app, s)
            if links:
                lines.append("Links: " + ", ".join(links))
            unmet = app.tasks.unmet_requirements(s)
            if unmet:
                lines.append("UNMET REQUIREMENTS: " + "; ".join(unmet))
        app.tasks.set_status(parent.id, "queued", message="Subtasks finished; resuming")
        deliver_input(app, parent.id, "\n".join(lines))


def notify_owner(app: "App", task: "Task") -> None:
    chat = task.chat_id or app.owner_chat_id
    if not chat:
        return
    agent = get_agent(task.agent_id)
    if task.status == "cancelled":
        text = f"⏹ <b>{task.ref} cancelled</b> — {esc(task.title)}\n{esc(task.error or '')}"
    else:
        icon = OUTCOME_ICON.get(task.outcome or "", "⏹")
        text = f"{icon} <b>{task.ref} · {esc(agent.name)} — {esc((task.outcome or task.status).title())}</b>\n"
        text += f"<i>{esc(task.title)}</i>\n\n{esc(task.result_summary or task.error or '')}"
        links = _all_links(app, task)
        if links:
            text += "\n\n" + "\n".join(f"• {esc(link)}" for link in links[:30])
        kids = app.tasks.children(task.id)
        if kids:
            text += "\n\n<b>Team</b>: " + ", ".join(
                f"{k.ref} {get_agent(k.agent_id).name} {OUTCOME_ICON.get(k.outcome or '', '⏹')}" for k in kids)
    _remember_message(app, chat, task.id, app.messenger.send(chat, text))


def _all_links(app: "App", task: "Task") -> list[str]:
    """Links the agent reported plus every verified deliverable URL in the task's subtree."""
    verified = [d["url"] for d in app.tasks.deliverables(app.tasks.subtree_ids(task.id)) if d["verified"] and d["url"]]
    return list(dict.fromkeys([*task.result_links, *verified]))


def _remember_message(app: "App", chat_id: int, task_id: int, message_id: int | None = None) -> None:
    """Map a bot message to its task so the owner can reply to it to continue that task."""
    if message_id:
        app.db.kv_set(f"tgmsg:{chat_id}:{message_id}", str(task_id))


def remember(app: "App", chat_id: int, task_id: int, message_id: int | None) -> None:
    _remember_message(app, chat_id, task_id, message_id)


def task_for_message(app: "App", chat_id: int, message_id: int) -> int | None:
    v = app.db.kv_get(f"tgmsg:{chat_id}:{message_id}")
    return int(v) if v else None


__all__ = ["enqueue_run", "maybe_start", "deliver_input", "finalize_task", "block_task", "fail_task",
           "cancel_tree", "on_terminal", "notify_owner", "remember", "task_for_message", "TERMINAL", "fmt_id"]
