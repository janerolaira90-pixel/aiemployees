"""Telegram command center: authorization, routing, commands, approval buttons, replies."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from . import approvals
from .agents.registry import AGENTS, get as get_agent
from .integrations.telegram import esc
from .orchestration import cancel_tree, deliver_input, enqueue_run, remember, task_for_message
from .routing import route, title_from
from .tasks import ACTIVE, fmt_id, parse_id, today_in

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger(__name__)


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat(timespec="seconds")

COMMANDS = [
    ("help", "How to talk to the team"),
    ("agents", "The 14 AI employees"),
    ("status", "Active projects"),
    ("task", "Details for a task, e.g. /task T-0007"),
    ("approvals", "Pending approvals"),
    ("overdue", "Overdue deliverables"),
    ("cancel", "Cancel a task, e.g. /cancel T-0007"),
    ("costs", "AI cost, last 7 days"),
    ("health", "Integrations and queue health"),
]

HELP = """<b>Eunaria AI team</b>
Talk to Atlas (Chief of Staff) by default, or address anyone by name:
• <i>Atlas, plan next week's content for NULA.</i>
• <i>Chloe, add 20 content ideas to Notion for Inoue.</i>
• <i>Grace, give me today's GHL report.</i>

Every request becomes a task (T-0001…) that runs in the background; results come back here with Notion links.
Reply to any task message to give that task more input.

/status · /task T-0007 · /approvals · /overdue · /cancel T-0007 · /costs · /health · /agents"""


def implied_requirements(agent_id: str, text: str, client: str | None) -> list[dict]:
    """Direct "Chloe, add 20 ideas to Notion" requests must end with 20 verified Notion records."""
    if agent_id != "chloe" or not client or "notion" not in text.lower():
        return []
    m = re.search(r"\b(\d{1,3})\b[^.\n]{0,40}\bideas?\b", text, re.IGNORECASE)
    if not m or int(m.group(1)) < 1:
        return []
    return [{"kind": "notion_page", "target": f"{client}/content_ideas", "min": int(m.group(1))}]


class Bot:
    def __init__(self, app: "App"):
        self.app = app

    # --- entry points ------------------------------------------------------------------------

    def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            self._callback(update["callback_query"])
        elif "message" in update:
            self._message(update["message"])

    def _authorized(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.app.settings.telegram_admin_ids

    def _message(self, msg: dict) -> None:
        app = self.app
        user = msg.get("from") or {}
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not self._authorized(user.get("id")):
            app.db.audit(f"tg:{user.get('id')}", "unauthorized_message", {"chat": chat_id})
            if not app.db.kv_get(f"unauth-notified:{user.get('id')}"):
                app.db.kv_set(f"unauth-notified:{user.get('id')}", "1")
                app.messenger.send(chat_id, "This is a private assistant. You are not authorized.")
            return
        if not text:
            app.messenger.send(chat_id, "I can only read text right now — please describe the request in words.")
            return
        if text.startswith("/"):
            cmd, _, arg = text[1:].partition(" ")
            cmd = cmd.split("@")[0].lower()
            handler = getattr(self, f"cmd_{cmd}", None)
            if handler:
                handler(chat_id, arg.strip())
                return
        reply_to = msg.get("reply_to_message")
        if reply_to:
            task_id = task_for_message(app, chat_id, reply_to["message_id"])
            if task_id:
                self._continue_task(chat_id, task_id, text)
                return
        self.new_request(chat_id, text, user_id=user.get("id"))

    def _callback(self, cq: dict) -> None:
        app = self.app
        user_id = (cq.get("from") or {}).get("id")
        if not self._authorized(user_id):
            app.messenger.answer_callback(cq["id"], "Not authorized")
            return
        data = cq.get("data") or ""
        msg = cq.get("message") or {}
        if data.startswith("ap:"):
            _, appr_id, choice = data.split(":")
            result = approvals.decide(app, int(appr_id), choice == "y", decided_by=f"tg:{user_id}")
            app.messenger.answer_callback(cq["id"], result)
            if msg:
                app.messenger.edit_buttons(msg["chat"]["id"], msg["message_id"], None)
                app.messenger.send(msg["chat"]["id"], esc(result))
        else:
            app.messenger.answer_callback(cq["id"])

    # --- requests -------------------------------------------------------------------------------

    def new_request(self, chat_id: int, text: str, user_id: int | None = None):
        app = self.app
        r = route(text)
        client = app.workspace.detect_client(r.text)
        task = app.tasks.create(agent_id=r.agent.id, title=title_from(r.text), instructions=r.text,
                                client=client, chat_id=chat_id, created_by="owner",
                                requires=implied_requirements(r.agent.id, r.text, client))
        app.db.audit(f"tg:{user_id}", "task_created", {"task": task.id, "agent": r.agent.id})
        enqueue_run(app, task.id)
        who = r.agent.display if r.agent.id != "atlas" else "Atlas — Chief of Staff"
        client_note = f" · client: {app.workspace.clients[client].name}" if client else ""
        mid = app.messenger.send(chat_id, f"🗂 <b>{task.ref}</b> → {esc(who)}{esc(client_note)}\n"
                                          f"Working on it in the background. Reply to this message to add details.")
        remember(app, chat_id, task.id, mid)
        return task

    def _continue_task(self, chat_id: int, task_id: int, text: str) -> None:
        task = self.app.tasks.get(task_id)
        deliver_input(self.app, task_id, f"Message from the owner: {text}", reopen=True)
        mid = self.app.messenger.send(chat_id, f"↩️ Added to <b>{task.ref}</b> ({esc(get_agent(task.agent_id).name)}).")
        remember(self.app, chat_id, task_id, mid)

    # --- commands -------------------------------------------------------------------------------

    def cmd_start(self, chat_id: int, arg: str) -> None:
        self.cmd_help(chat_id, arg)

    def cmd_help(self, chat_id: int, arg: str) -> None:
        self.app.messenger.send(chat_id, HELP)

    def cmd_agents(self, chat_id: int, arg: str) -> None:
        lines = ["<b>Your AI employees</b>"]
        dept = None
        for a in sorted(AGENTS, key=lambda a: a.number):
            if a.department != dept:
                dept = a.department
                lines.append(f"\n<b>{esc(dept)}</b>")
            lines.append(f"{a.number:02d}. <b>{esc(a.name)}</b> — {esc(a.title)}")
        self.app.messenger.send(chat_id, "\n".join(lines))

    def cmd_status(self, chat_id: int, arg: str) -> None:
        app = self.app
        today = today_in(app.settings.timezone)
        projects = app.tasks.list(statuses=ACTIVE, roots_only=True, limit=30)
        if not projects:
            app.messenger.send(chat_id, "No active projects. 🌿")
            return
        lines = ["<b>Active projects</b>"]
        for p in projects:
            kids = app.tasks.children(p.id)
            done = sum(1 for k in kids if k.is_terminal)
            prog = f" · {done}/{len(kids)} subtasks" if kids else ""
            late = " · ⚠️ overdue" if p.is_overdue(today) else ""
            lines.append(f"• <b>{p.ref}</b> {esc(get_agent(p.agent_id).name)}: {esc(p.title)} "
                         f"[{p.status}{prog}{late}]")
        app.messenger.send(chat_id, "\n".join(lines))

    cmd_tasks = cmd_status

    def cmd_task(self, chat_id: int, arg: str) -> None:
        app = self.app
        tid = parse_id(arg) if arg else None
        task = app.tasks.get(tid) if tid else None
        if not task:
            app.messenger.send(chat_id, "Usage: /task T-0007")
            return
        lines = [f"<b>{task.ref} · {esc(get_agent(task.agent_id).display)}</b>", esc(task.title),
                 f"Status: {task.status}{' / ' + task.outcome if task.outcome else ''}"
                 f"{' · due ' + task.deadline if task.deadline else ''}"]
        if task.result_summary:
            lines += ["", esc(task.result_summary[:1500])]
        if task.error and task.status != "done":
            lines += ["", f"⚠️ {esc(task.error[:800])}"]
        for k in app.tasks.children(task.id):
            lines.append(f"  └ {k.ref} {esc(get_agent(k.agent_id).name)}: {esc(k.title)} [{k.status}]")
        for d in app.tasks.deliverables(app.tasks.subtree_ids(task.id))[:20]:
            if d["url"]:
                lines.append(f"• {'✔︎' if d['verified'] else '?'} {esc(d['title'] or d['kind'])}: {esc(d['url'])}")
        mid = app.messenger.send(chat_id, "\n".join(lines))
        remember(app, chat_id, task.id, mid)

    def cmd_approvals(self, chat_id: int, arg: str) -> None:
        app = self.app
        rows = approvals.pending(app)
        if not rows:
            app.messenger.send(chat_id, "No pending approvals.")
            return
        for a in rows[:10]:
            app.messenger.send(
                chat_id,
                f"🔐 <b>A-{a['id']}</b> · {esc(get_agent(a['agent_id']).name)} · {fmt_id(a['task_id'])}\n"
                f"<b>{esc(a['action'])}</b>\n{esc(a['summary'])}",
                buttons=[[("✅ Approve", f"ap:{a['id']}:y"), ("❌ Reject", f"ap:{a['id']}:n")]])

    def cmd_approve(self, chat_id: int, arg: str) -> None:
        self._decide(chat_id, arg, True)

    def cmd_reject(self, chat_id: int, arg: str) -> None:
        self._decide(chat_id, arg, False)

    def _decide(self, chat_id: int, arg: str, approve: bool) -> None:
        num = arg.upper().removeprefix("A-").strip()
        if not num.isdigit():
            self.app.messenger.send(chat_id, f"Usage: /{'approve' if approve else 'reject'} A-12")
            return
        self.app.messenger.send(chat_id, esc(approvals.decide(self.app, int(num), approve, decided_by=f"tg-chat:{chat_id}")))

    def cmd_overdue(self, chat_id: int, arg: str) -> None:
        app = self.app
        today = today_in(app.settings.timezone)
        rows = app.tasks.overdue(today)
        if not rows:
            app.messenger.send(chat_id, "Nothing overdue. ✨")
            return
        lines = ["<b>Overdue</b>"] + [
            f"• {t.ref} {esc(get_agent(t.agent_id).name)}: {esc(t.title)} — due {t.deadline} [{t.status}]" for t in rows]
        app.messenger.send(chat_id, "\n".join(lines))

    def cmd_cancel(self, chat_id: int, arg: str) -> None:
        tid = parse_id(arg) if arg else None
        if not tid or not self.app.tasks.get(tid):
            self.app.messenger.send(chat_id, "Usage: /cancel T-0007")
            return
        n = cancel_tree(self.app, tid, "Cancelled by owner")
        self.app.messenger.send(chat_id, f"⏹ Cancelled {fmt_id(tid)} ({n} open task(s)).")

    def cmd_costs(self, chat_id: int, arg: str) -> None:
        rows = self.app.db.query(
            "SELECT agent_id, ROUND(SUM(cost_usd),2) usd, COUNT(*) calls FROM agent_runs "
            "WHERE ts >= ? GROUP BY agent_id ORDER BY usd DESC", (_ago(days=7),))
        total = sum(r["usd"] or 0 for r in rows)
        lines = [f"<b>AI cost, last 7 days: ${total:.2f}</b> (today ${self.app.cost_today_usd():.2f}, "
                 f"daily cap ${self.app.settings.daily_cost_limit_usd:.2f})"]
        lines += [f"• {esc(get_agent(r['agent_id']).name)}: ${r['usd']:.2f} ({r['calls']} calls)" for r in rows]
        self.app.messenger.send(chat_id, "\n".join(lines))

    def cmd_health(self, chat_id: int, arg: str) -> None:
        app = self.app
        from .service import startup_checks
        lines = ["<b>System health</b> (live checks)"]
        for name, ok, detail in startup_checks(app):
            lines.append(f"{'🟢' if ok else '🔴'} {esc(name)}: {esc(detail)}")
        failed = app.db.one("SELECT COUNT(*) n FROM jobs WHERE status='failed' AND finished_at >= ?",
                            (_ago(days=1),))
        lines.append(f"Queue: {app.queue.pending_count()} pending · {failed['n']} failed (24h)")
        app.messenger.send(chat_id, "\n".join(lines))

    def cmd_brief(self, chat_id: int, arg: str) -> None:
        self.new_request(chat_id, "Atlas, give me my daily briefing.")
