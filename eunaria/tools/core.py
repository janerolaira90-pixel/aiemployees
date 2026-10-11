"""Task, delegation, and team-visibility tools."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from ..agents.registry import AGENTS, BY_ID
from ..tasks import ACTIVE, TERMINAL, fmt_id, parse_id, today_in
from .base import ToolContext, ToolError, tool

TASK_ID = {"type": "string", "description": "Task ID like T-0042"}
REQUIREMENT = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["notion_page", "notion_update"]},
        "target": {"type": "string", "description": "'<client>/<database key>', e.g. 'nula/content_ideas'"},
        "min": {"type": "integer", "minimum": 1},
    },
    "required": ["kind", "target", "min"],
    "additionalProperties": False,
}


def _task_arg(ctx: ToolContext, value: str | None):
    if not value:
        return ctx.task
    tid = parse_id(value)
    task = ctx.app.tasks.get(tid) if tid else None
    if not task:
        raise ToolError(f"No task {value}")
    return task


def _visible(ctx: ToolContext, task) -> bool:
    """Atlas, Maya, and Ava see everything; others see their own task tree and dependencies."""
    if ctx.agent.id in ("atlas", "maya", "ava"):
        return True
    root = ctx.app.tasks.root(ctx.task)
    return task.id in ctx.app.tasks.subtree_ids(root.id)


@tool("get_task", "Get a task's details: brief, status, results, subtasks, deliverables, recent events. "
      "Omit task_id for your current task.", {"task_id": TASK_ID})
def get_task(ctx: ToolContext, args: dict):
    task = _task_arg(ctx, args.get("task_id"))
    if not _visible(ctx, task):
        raise ToolError("That task belongs to another project.")
    kids = ctx.app.tasks.children(task.id)
    return {
        **task.brief(),
        "instructions": task.instructions,
        "depends_on": [fmt_id(d) for d in task.depends_on],
        "requires": task.requires,
        "result_summary": task.result_summary,
        "result_links": task.result_links,
        "error": task.error,
        "subtasks": [{**k.brief(), "result_summary": k.result_summary, "result_links": k.result_links}
                     for k in kids],
        "deliverables": [{k: d[k] for k in ("kind", "target", "title", "url", "verified")}
                         for d in ctx.app.tasks.deliverables([task.id])],
        "unmet_requirements": ctx.app.tasks.unmet_requirements(task),
        "events": ctx.app.tasks.events(task.id, 10),
    }


@tool(
    "submit_result",
    "Finish your current task. Use outcome 'completed' only if every requested deliverable exists "
    "(verified records count, not drafts in chat). Use 'partial' or 'failed' honestly otherwise.",
    {
        "outcome": {"type": "string", "enum": ["completed", "partial", "failed"]},
        "summary": {"type": "string", "description": "Concise report for the owner (Telegram-friendly)."},
        "links": {"type": "array", "items": {"type": "string"}, "description": "URLs of deliverables"},
        "issues": {"type": "string", "description": "What is missing, failed, or needs the owner"},
    },
    ["outcome", "summary"],
    terminal=True,
)
def submit_result(ctx: ToolContext, args: dict):
    task = ctx.task
    open_kids = [k for k in ctx.app.tasks.children(task.id) if not k.is_terminal]
    if open_kids:
        raise ToolError("Subtasks still in progress: " + ", ".join(f"{k.ref} ({k.agent_id}, {k.status})"
                                                                for k in open_kids)
                        + ". End your turn and you will be resumed when they finish.")
    outcome = args["outcome"]
    unmet = ctx.app.tasks.unmet_requirements(task)
    if outcome == "completed" and unmet:
        raise ToolError("Cannot mark completed — required deliverables not verified: " + "; ".join(unmet)
                        + ". Create the records, or submit with outcome 'partial' and explain.")
    summary = args["summary"].strip()
    if args.get("issues"):
        summary += f"\n\nIssues: {args['issues'].strip()}"
    links = list(dict.fromkeys(args.get("links") or []))
    from ..orchestration import finalize_task
    finalize_task(ctx.app, task.id, outcome=outcome, summary=summary, links=links)
    return {"ok": True, "task": task.ref, "outcome": outcome}


@tool(
    "report_blocked",
    "Stop work on your task because something only the owner can resolve is missing (credential, access, "
    "decision, asset). Be specific about what is needed.",
    {"blocker": {"type": "string"}, "needed_from_owner": {"type": "string"}},
    ["blocker", "needed_from_owner"],
    terminal=True,
)
def report_blocked(ctx: ToolContext, args: dict):
    from ..orchestration import block_task
    block_task(ctx.app, ctx.task.id, f"{args['blocker'].strip()}\nNeeded: {args['needed_from_owner'].strip()}")
    return {"ok": True, "task": ctx.task.ref, "status": "blocked"}


@tool(
    "delegate_task",
    "Assign a subtask of your current task to a specialist. The subtask starts immediately unless it "
    "depends_on sibling subtasks, in which case it starts when they finish and receives their results.",
    {
        "agent": {"type": "string", "enum": [a.id for a in AGENTS if a.id != "atlas"]},
        "title": {"type": "string"},
        "instructions": {"type": "string", "description": "Self-contained brief: objective, deliverable, "
                                                          "where to save it, constraints."},
        "client": {"type": "string", "description": "Client key (defaults to the parent task's client)"},
        "deadline": {"type": "string", "description": "YYYY-MM-DD"},
        "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
        "depends_on": {"type": "array", "items": TASK_ID},
        "requires": {"type": "array", "items": REQUIREMENT,
                     "description": "Deliverables that must be verified before the subtask can complete"},
    },
    ["agent", "title", "instructions"],
)
def delegate_task(ctx: ToolContext, args: dict):
    app, parent = ctx.app, ctx.task
    if args["agent"] not in BY_ID or args["agent"] == "atlas":
        raise ToolError(f"Unknown agent {args['agent']}")
    client = args.get("client") or parent.client
    if client and client not in app.workspace.clients:
        resolved = app.workspace.resolve_client(client)
        if not resolved:
            raise ToolError(f"Unknown client {client!r}. Known: {', '.join(app.workspace.clients)}")
        client = resolved
    if parent.client and client != parent.client:
        raise ToolError(f"This project is for {parent.client}; subtasks cannot target another client.")
    deps = []
    for ref in args.get("depends_on") or []:
        dep = app.tasks.get(parse_id(ref) or -1)
        if not dep or dep.parent_id != parent.id:
            raise ToolError(f"depends_on {ref} must be a subtask of {parent.ref}")
        deps.append(dep.id)
    if args.get("deadline"):
        try:
            date.fromisoformat(args["deadline"])
        except ValueError:
            raise ToolError("deadline must be YYYY-MM-DD") from None
    child = app.tasks.create(
        agent_id=args["agent"], title=args["title"], instructions=args["instructions"], client=client,
        parent_id=parent.id, priority=args.get("priority") or parent.priority,
        deadline=args.get("deadline") or parent.deadline, depends_on=deps,
        requires=args.get("requires") or [], chat_id=None, created_by=ctx.agent.id,
    )
    from ..orchestration import maybe_start
    started = maybe_start(app, child.id)
    return {"task": child.ref, "agent": child.agent_id, "status": "started" if started else "waiting on dependencies"}


@tool("cancel_task", "Cancel a task and its open subtasks.", {"task_id": TASK_ID, "reason": {"type": "string"}},
      ["task_id", "reason"])
def cancel_task(ctx: ToolContext, args: dict):
    from ..orchestration import cancel_tree
    task = _task_arg(ctx, args["task_id"])
    n = cancel_tree(ctx.app, task.id, f"Cancelled by {ctx.agent.name}: {args['reason']}")
    return {"cancelled": n}


@tool("list_agents", "List the 14 AI employees and what each owns.", {})
def list_agents(ctx: ToolContext, args: dict):
    return [{"id": a.id, "name": a.name, "title": a.title, "department": a.department, "mission": a.mission}
            for a in AGENTS]


@tool(
    "list_tasks",
    "List tasks from the team tracker.",
    {
        "status": {"type": "string", "enum": ["active", "done", "all"], "description": "default active"},
        "agent": {"type": "string"},
        "client": {"type": "string"},
        "projects_only": {"type": "boolean", "description": "Only top-level projects"},
        "limit": {"type": "integer", "maximum": 200},
    },
)
def list_tasks(ctx: ToolContext, args: dict):
    status = args.get("status") or "active"
    statuses = {"active": ACTIVE, "done": TERMINAL, "all": None}[status]
    rows = ctx.app.tasks.list(statuses=statuses, agent_id=args.get("agent"), client=args.get("client"),
                              roots_only=bool(args.get("projects_only")), limit=args.get("limit") or 50)
    today = today_in(ctx.app.settings.timezone)
    return [{**t.brief(), "overdue": t.is_overdue(today), "updated_at": t.updated_at,
             "summary": (t.result_summary or "")[:300]} for t in rows]


@tool("list_overdue_tasks", "Active tasks past their deadline, with owner and days overdue.", {})
def list_overdue_tasks(ctx: ToolContext, args: dict):
    today = today_in(ctx.app.settings.timezone)
    return [{**t.brief(), "days_overdue": (today - date.fromisoformat(t.deadline[:10])).days}
            for t in ctx.app.tasks.overdue(today)]


@tool("list_approvals", "Owner approvals that are pending (or recent decisions).",
      {"include_decided": {"type": "boolean"}})
def list_approvals(ctx: ToolContext, args: dict):
    sql = "SELECT id, task_id, agent_id, action, summary, status, requested_at, decided_at FROM approvals"
    if not args.get("include_decided"):
        sql += " WHERE status='pending'"
    rows = ctx.app.db.query(sql + " ORDER BY id DESC LIMIT 50")
    return [{**dict(r), "id": f"A-{r['id']}", "task_id": fmt_id(r["task_id"])} for r in rows]


@tool("operations_snapshot", "One-call overview for briefings: active projects, overdue, blocked, pending "
      "approvals, completed in the last 24h, today's AI cost, integration status.", {})
def operations_snapshot(ctx: ToolContext, args: dict):
    app = ctx.app
    today = today_in(app.settings.timezone)
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    done = app.db.query("SELECT id, agent_id, title, outcome FROM tasks WHERE completed_at >= ? "
                        "AND parent_id IS NULL ORDER BY id DESC LIMIT 30", (since,))
    return {
        "date": today.isoformat(),
        "active_projects": [t.brief() for t in app.tasks.list(statuses=ACTIVE, roots_only=True, limit=50)],
        "overdue": [t.brief() for t in app.tasks.overdue(today)],
        "blocked": [{**t.brief(), "error": t.error} for t in app.tasks.list(statuses=("blocked",), limit=50)],
        "pending_approvals": list_approvals(ctx, {}),
        "completed_last_24h": [{**dict(r), "id": fmt_id(r["id"])} for r in done],
        "ai_cost_today_usd": round(app.cost_today_usd(), 2),
        "integrations": app.integration_status(),
    }


@tool("cost_report", "AI operating cost per employee over the last N days (from recorded API usage).",
      {"days": {"type": "integer", "minimum": 1, "maximum": 90}})
def cost_report(ctx: ToolContext, args: dict):
    days = args.get("days") or 7
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = ctx.app.db.query(
        "SELECT agent_id, COUNT(*) calls, SUM(input_tokens) inp, SUM(output_tokens) outp, "
        "SUM(cache_read) cache_read, ROUND(SUM(cost_usd), 4) usd FROM agent_runs WHERE ts >= ? "
        "GROUP BY agent_id ORDER BY usd DESC", (since,))
    rows = [dict(r) for r in rows]
    return {"days": days, "total_usd": round(sum(r["usd"] or 0 for r in rows), 4), "by_agent": rows,
            "daily_limit_usd": ctx.app.settings.daily_cost_limit_usd}

