"""Tool framework: definitions, permission checks, approval gating, idempotent execution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from ..db import dumps, now_iso

if TYPE_CHECKING:
    from ..agents.registry import Agent
    from ..app import App
    from ..tasks import Task

MAX_RESULT_CHARS = 24_000


class ToolError(Exception):
    """An expected, explainable failure returned to the model as an is_error tool result."""


@dataclass
class ToolContext:
    app: "App"
    task: "Task"
    agent: "Agent"
    approved: bool = False   # True only when executing an owner-approved action


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[ToolContext, dict], Any]
    approval: bool = False        # requires owner approval before execution
    side_effect: bool = False     # non-idempotent external write
    terminal: bool = False        # ends the agent's run
    summarize: Callable[[dict], str] | None = None

    def definition(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, properties: dict, required: list[str] | None = None, **flags):
    def deco(fn):
        REGISTRY[name] = Tool(
            name=name, description=description,
            schema={"type": "object", "properties": properties, "required": required or [],
                    "additionalProperties": False},
            handler=fn, **flags,
        )
        return fn
    return deco


def definitions_for(agent: "Agent", web_search: bool, max_searches: int = 3) -> list[dict]:
    defs = [REGISTRY[n].definition() for n in agent.tools if n in REGISTRY]
    if web_search and agent.web_search:
        defs.append({"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches})
    return defs


def _render(result: Any) -> str:
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str, indent=1)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n…[truncated {len(text) - MAX_RESULT_CHARS} chars]"
    return text


def execute(ctx: ToolContext, tool_use_id: str, name: str, args: dict) -> tuple[str, bool]:
    """Run one tool call for an agent. Returns (result_text, is_error).

    Idempotency: results are stored per tool_use_id. If the process died mid-execution of a
    side-effecting tool, it is NOT re-run; the agent is told to check state first.
    """
    db = ctx.app.db
    prior = db.one("SELECT status, result, is_error FROM tool_calls WHERE tool_use_id=?", (tool_use_id,))
    if prior and prior["status"] in ("done", "error"):
        return prior["result"], bool(prior["is_error"])
    spec = REGISTRY.get(name)
    if prior and prior["status"] == "started" and spec and spec.side_effect:
        msg = (f"Execution of {name} was interrupted by a restart; it may or may not have taken effect. "
               "Check the current state (e.g. search for the record) before retrying.")
        _finish(ctx, tool_use_id, msg, True)
        return msg, True

    if spec is None or name not in ctx.agent.tools:
        msg = f"Tool {name!r} is not available to {ctx.agent.name}."
        return msg, True

    db.execute(
        "INSERT OR REPLACE INTO tool_calls(tool_use_id, task_id, agent_id, name, input, status, started_at) "
        "VALUES (?,?,?,?,?,'started',?)",
        (tool_use_id, ctx.task.id, ctx.agent.id, name, dumps(args), now_iso()),
    )
    try:
        if spec.approval and not ctx.approved:
            from ..approvals import request_approval
            summary = spec.summarize(args) if spec.summarize else f"{name} {dumps(args)}"
            appr = request_approval(ctx.app, ctx.task, ctx.agent, name, args, summary)
            result: Any = {
                "status": "approval_requested", "approval_id": f"A-{appr}",
                "note": "NOT executed. The owner must approve this exact action in Telegram. "
                        "You will be resumed with the decision. Do not claim it happened.",
            }
        else:
            result = spec.handler(ctx, args)
        text, is_error = _render(result), False
    except ToolError as exc:
        text, is_error = f"Error: {exc}", True
    except Exception as exc:  # noqa: BLE001 - integration failures are reported, not hidden
        text, is_error = f"Error: {type(exc).__name__}: {exc}", True
    _finish(ctx, tool_use_id, text, is_error)
    return text, is_error


def _finish(ctx: ToolContext, tool_use_id: str, text: str, is_error: bool) -> None:
    ctx.app.db.execute(
        "UPDATE tool_calls SET status=?, result=?, is_error=?, finished_at=? WHERE tool_use_id=?",
        ("error" if is_error else "done", text, int(is_error), now_iso(), tool_use_id),
    )
