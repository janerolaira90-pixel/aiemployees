"""The agent loop: one Claude conversation per task, persisted step by step.

Every assistant response and every batch of tool results is written to SQLite before the
next step, so a restart resumes from the last completed step instead of starting over.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .agents.prompts import system_prompt
from .agents.registry import get as get_agent
from .approvals import pending as pending_approvals
from .db import now_iso
from .llm import PermanentLLMError
from .orchestration import block_task, fail_task
from .queue import Defer
from .tasks import ACTIVE, fmt_id, today_in
from .tools import REGISTRY, ToolContext, definitions_for, execute

if TYPE_CHECKING:
    from .app import App
    from .tasks import Task

log = logging.getLogger(__name__)

NUDGE = ("You ended your turn without calling submit_result or report_blocked, and nothing is pending. "
         "Finish the task now: create any required records, then call submit_result (or report_blocked).")


def run_task(app: "App", task_id: int, worker_id: str) -> None:
    if not app.locks.acquire(task_id, worker_id):
        raise Defer(10)
    try:
        _run(app, task_id)
    finally:
        app.locks.release(task_id, worker_id)


def build_brief(app: "App", task: "Task") -> str:
    agent = get_agent(task.agent_id)
    ws = app.workspace
    client = ws.clients.get(task.client) if task.client else None
    lines = [
        f"Task {task.ref} assigned to you ({agent.name}) by {task.created_by}.",
        f"Title: {task.title}",
        f"Client: {f'{client.name} (key: {client.key})' if client else 'not specified (Eunaria internal unless the request names a client)'}",
        f"Priority: {task.priority}. Deadline: {task.deadline or 'none set'}.",
        f"Today: {today_in(app.settings.timezone).isoformat()} ({app.settings.timezone}).",
    ]
    if client and client.notes:
        lines.append(f"Client notes: {client.notes}")
    if task.parent_id:
        parent = app.tasks.get(task.parent_id)
        lines.append(f"Part of project {parent.ref}: {parent.title}")
    if task.requires:
        lines.append("Required verified deliverables: " + "; ".join(
            f"{r['min']}× {r['kind']} in {r['target']}" for r in task.requires))
    if agent.id == "atlas":
        active = app.tasks.list(statuses=ACTIVE, roots_only=True, limit=15)
        others = [t for t in active if t.id != task.id]
        lines.append("Known clients: " + ", ".join(f"{c.key} ({c.name})" for c in ws.clients.values()))
        lines.append("Integrations: " + ", ".join(f"{k}={v}" for k, v in app.integration_status().items()))
        if others:
            lines.append("Other active projects: " + "; ".join(f"{t.ref} {t.agent_id}: {t.title} [{t.status}]"
                                                               for t in others))
    lines += ["", "Request / instructions:", task.instructions or task.title]
    return "\n".join(lines)


def _settle(app: "App", task_id: int) -> bool:
    """Park a task that is waiting on subtasks or approvals. Returns True if parked."""
    task = app.tasks.get(task_id)
    open_kids = [k for k in app.tasks.children(task_id) if not k.is_terminal]
    if open_kids:
        if task.status != "waiting":
            app.tasks.set_status(task_id, "waiting", message="Waiting on " + ", ".join(k.ref for k in open_kids))
        return True
    if pending_approvals(app, [task_id]):
        if task.status != "awaiting_approval":
            app.tasks.set_status(task_id, "awaiting_approval", message="Waiting for owner approval")
        return True
    return False


def _record_run(app: "App", task: "Task", resp) -> None:
    u = resp.usage
    app.db.execute(
        """INSERT INTO agent_runs(task_id, agent_id, model, input_tokens, output_tokens, cache_read, cache_write,
           cost_usd, stop_reason, request_id, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (task.id, task.agent_id, resp.model, u.get("input_tokens", 0), u.get("output_tokens", 0),
         u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0), resp.cost_usd(),
         resp.stop_reason, resp.request_id, now_iso()),
    )


def _run(app: "App", task_id: int) -> None:
    task = app.tasks.get(task_id)
    if task is None or task.is_terminal:
        return
    if task.status == "blocked" and not app.inbox.pending(task_id):
        return
    limit = app.settings.daily_cost_limit_usd
    if limit and app.cost_today_usd() >= limit:
        block_task(app, task_id, f"Daily AI cost limit (${limit:.2f}) reached. Raise EUNARIA_DAILY_COST_LIMIT_USD "
                                 "or wait until tomorrow; reply to resume.")
        return

    agent = get_agent(task.agent_id)
    convo = app.convo
    if convo.last_role(task_id) is None:
        convo.append(task_id, "user", build_brief(app, task))
    for text in app.inbox.drain(task_id):
        convo.append(task_id, "user", text)
    if convo.last_role(task_id) == "assistant":
        _settle(app, task_id)   # nothing new to respond to (duplicate wake-up)
        return

    app.tasks.set_status(task_id, "running", message=f"{agent.name} working")
    system = system_prompt(agent)
    tools = definitions_for(agent, app.settings.enable_web_search, app.settings.web_search_max_uses)
    model = app.settings.model_for(agent.tier)
    nudges = 0
    for _ in range(app.settings.max_tool_iterations):
        try:
            resp = app.llm.create(system=system, messages=convo.load(task_id), tools=tools,
                                  effort=agent.effort, agent_id=agent.id, model=model)
        except PermanentLLMError as exc:
            fail_task(app, task_id, f"Could not call Claude: {exc}")
            return
        _record_run(app, task, resp)
        convo.append(task_id, "assistant", resp.content)

        if resp.stop_reason == "refusal":
            details = resp.stop_details or {}
            fail_task(app, task_id, "The model declined this request "
                                    f"(category: {details.get('category') or 'unspecified'}). Rephrase or adjust it.")
            return
        tool_uses = [b for b in resp.content if b.get("type") == "tool_use"]
        if not tool_uses:
            if resp.stop_reason == "pause_turn":
                continue
            if resp.stop_reason == "max_tokens":
                convo.append(task_id, "user", "Your last response hit the length limit. Continue, more concisely.")
                continue
            new = app.inbox.drain(task_id)
            if new:
                for text in new:
                    convo.append(task_id, "user", text)
                continue
            if _settle(app, task_id):
                return
            if nudges < 2:
                nudges += 1
                convo.append(task_id, "user", NUDGE)
                continue
            fail_task(app, task_id, f"{agent.name} stopped without submitting a result.")
            return

        results, finished = [], False
        for tu in tool_uses:
            ctx = ToolContext(app=app, task=app.tasks.get(task_id), agent=agent)
            text, is_error = execute(ctx, tu["id"], tu["name"], tu.get("input") or {})
            block = {"type": "tool_result", "tool_use_id": tu["id"], "content": text}
            if is_error:
                block["is_error"] = True
            results.append(block)
            spec = REGISTRY.get(tu["name"])
            if spec and spec.terminal and not is_error:
                finished = True
            app.tasks.event(task_id, "tool", f"{tu['name']}{' (error)' if is_error else ''}")
        convo.append(task_id, "user", results)
        if finished:
            return
        for text in app.inbox.drain(task_id):
            convo.append(task_id, "user", text)

    block_task(app, task_id, f"{agent.name} reached the step limit ({app.settings.max_tool_iterations}) without "
                             f"finishing {fmt_id(task_id)}. Reply to continue or narrow the request.")
