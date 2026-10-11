"""Approval gates, integrity checks, restart recovery, scheduling, and adapters."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from eunaria import approvals, scheduler
from eunaria.bot import Bot
from eunaria.integrations.notion import markdown_to_blocks
from eunaria.orchestration import enqueue_run
from eunaria.service import RedactingFilter, recover_on_startup
from eunaria.tools import ToolContext, execute
from eunaria.agents import get as get_agent

from .conftest import ADMIN, CHAT, drain, owner_msg
from .fakes import ScriptedLLM, last_tool_results, text, tool_use


def _grace_tag_script(llm: ScriptedLLM):
    llm.add(
        "grace",
        [tool_use("ghl_add_contact_tags", contact_id="c1", tags=["hot-lead"], reason="Replied asking for pricing")],
        [text("Waiting for the owner's approval.")],
        lambda m: [tool_use("submit_result", outcome="completed" if "was executed" in json.dumps(m[-1]) else "partial",
                            summary="Tagging " + ("done" if "was executed" in json.dumps(m[-1]) else "rejected"))],
    )


def _callback(appr_id: int, choice: str, user: int = ADMIN):
    return {"update_id": 99, "callback_query": {"id": "cb1", "from": {"id": user}, "data": f"ap:{appr_id}:{choice}",
                                                 "message": {"message_id": 5, "chat": {"id": CHAT}}}}


def test_external_write_waits_for_approval_then_runs_exact_action(make_app, fake_ghl):
    llm = ScriptedLLM()
    _grace_tag_script(llm)
    app = make_app(llm)
    bot = Bot(app)
    bot.handle_update(owner_msg("Grace, tag Ana from Glow Clinic as a hot lead"))
    drain(app)

    task = app.tasks.list()[0]
    assert task.status == "awaiting_approval"
    assert fake_ghl.writes == []                                   # nothing happened yet
    pend = approvals.pending(app)
    assert len(pend) == 1 and pend[0]["action"] == "ghl_add_contact_tags"
    approval_msg = next(m for m in app.messenger.sent if m["buttons"])
    assert "Approval needed" in approval_msg["text"] and "hot-lead" in approval_msg["text"]

    bot.handle_update(_callback(pend[0]["id"], "n", user=999))     # non-admin click is ignored
    assert approvals.get(app, pend[0]["id"])["status"] == "pending"

    bot.handle_update(_callback(pend[0]["id"], "y"))
    drain(app)
    assert fake_ghl.writes == [("POST", "/contacts/c1/tags", {"tags": ["hot-lead"]})]
    assert approvals.get(app, pend[0]["id"])["status"] == "executed"
    task = app.tasks.get(task.id)
    assert task.status == "done" and task.result_summary == "Tagging done"
    # A second click cannot run it twice.
    assert "already executed" in approvals.decide(app, pend[0]["id"], True, "tg:42")


def test_rejected_action_never_executes(make_app, fake_ghl):
    llm = ScriptedLLM()
    _grace_tag_script(llm)
    app = make_app(llm)
    bot = Bot(app)
    bot.handle_update(owner_msg("Grace, tag Ana as hot lead"))
    drain(app)
    appr = approvals.pending(app)[0]
    bot.handle_update(owner_msg(f"/reject A-{appr['id']}", update_id=3))
    drain(app)
    assert fake_ghl.writes == []
    t = app.tasks.list()[0]
    assert t.outcome == "partial" and t.result_summary == "Tagging rejected"


def test_tampered_approval_parameters_are_refused(make_app, fake_ghl):
    llm = ScriptedLLM()
    llm.add("grace",
            [tool_use("ghl_add_contact_tags", contact_id="c1", tags=["hot-lead"], reason="r")],
            [text("waiting")],
            [tool_use("submit_result", outcome="failed", summary="integrity failure")])
    app = make_app(llm)
    Bot(app).handle_update(owner_msg("Grace, tag Ana"))
    drain(app)
    appr = approvals.pending(app)[0]
    app.db.execute("UPDATE approvals SET params=? WHERE id=?",
                   (json.dumps({"contact_id": "c1", "tags": ["vip", "spam"], "reason": "r"}), appr["id"]))
    approvals.decide(app, appr["id"], True, "tg:42")
    drain(app)
    assert fake_ghl.writes == []
    assert approvals.get(app, appr["id"])["status"] == "failed"


def test_changed_parameters_need_a_new_approval(make_app):
    app = make_app(ScriptedLLM())
    task = app.tasks.create(agent_id="grace", title="t", chat_id=CHAT)
    ctx = ToolContext(app=app, task=task, agent=get_agent("grace"))
    execute(ctx, "toolu_a", "ghl_add_contact_tags", {"contact_id": "c1", "tags": ["a"], "reason": "r"})
    execute(ctx, "toolu_b", "ghl_add_contact_tags", {"contact_id": "c1", "tags": ["a", "b"], "reason": "r"})
    rows = approvals.pending(app)
    assert len(rows) == 2 and rows[0]["params_hash"] != rows[1]["params_hash"]


def test_restart_recovery_requeues_work_and_does_not_repeat_side_effects(make_app, fake_notion):
    llm = ScriptedLLM()
    app = make_app(llm)
    task = app.tasks.create(agent_id="chloe", title="ideas", client="nula", chat_id=CHAT)
    enqueue_run(app, task.id)
    job = app.queue.lease()                         # a worker took the job…
    app.tasks.set_status(task.id, "running")        # …and the process died mid-run.
    # A side-effecting tool call had started but never finished:
    app.db.execute("INSERT INTO tool_calls(tool_use_id, task_id, agent_id, name, input, status, started_at) "
                   "VALUES ('toolu_x', ?, 'chloe', 'notion_create_page', '{}', 'started', '2026-01-01')", (task.id,))
    assert job and app.queue.lease() is None

    info = recover_on_startup(app)                  # new process starts
    assert info["jobs_requeued"] == 1

    ctx = ToolContext(app=app, task=app.tasks.get(task.id), agent=get_agent("chloe"))
    result, is_error = execute(ctx, "toolu_x", "notion_create_page",
                               {"database": "content_ideas", "properties": {"Topic": "dup?"}})
    assert is_error and "interrupted" in result
    assert not fake_notion.pages                     # not silently re-created

    llm.add("chloe", [tool_use("submit_result", outcome="completed", summary="resumed")])
    drain(app)
    assert app.tasks.get(task.id).status == "done"


def test_scheduler_fires_once_per_day(make_app):
    app = make_app(ScriptedLLM())
    tz = ZoneInfo(app.settings.timezone)
    monday_9am = datetime(2026, 10, 12, 9, 5, tzinfo=tz)
    names = {r.name for r, _ in scheduler.due_routines(app, monday_9am)}
    assert {"daily_briefing", "overdue_check", "daily_ghl_report"} <= names
    first = scheduler.tick(app, monday_9am)
    assert first >= 3 and scheduler.tick(app, monday_9am) == 0
    app.llm.add("atlas", [tool_use("submit_result", outcome="completed", summary="Briefing")])
    app.llm.add("maya", [tool_use("submit_result", outcome="completed", summary="Nothing overdue")])
    app.llm.add("grace", [tool_use("submit_result", outcome="completed", summary="GHL report")])
    drain(app)
    titles = sorted(t.title for t in app.tasks.list())
    assert any(t.startswith("Daily executive briefing — 2026-10-12") for t in titles)
    assert all(t.chat_id == ADMIN for t in app.tasks.list())


def test_ghl_report_routine_skipped_without_ghl(make_app):
    app = make_app(ScriptedLLM(), ghl=False)
    names = {r.name for r, _ in scheduler.due_routines(app, datetime(2026, 10, 12, 9, 5,
                                                                     tzinfo=ZoneInfo(app.settings.timezone)))}
    assert "daily_ghl_report" not in names


def test_overdue_listing(make_app):
    app = make_app()
    app.tasks.create(agent_id="theo", title="Scripts", deadline="2020-01-01", chat_id=CHAT)
    app.tasks.create(agent_id="luca", title="Sequences", deadline="2999-01-01", chat_id=CHAT)
    Bot(app).handle_update(owner_msg("/overdue"))
    out = app.messenger.sent[-1]["text"]
    assert "Scripts" in out and "Sequences" not in out


def test_markdown_tables_become_notion_tables():
    md = ("## Scene plan\n| # | Visual | VO |\n|---|---|---|\n| 1 | Close-up | Hook |\n| 2 | B-roll | Proof |\n"
          "- **Note:** keep it under 30s")
    blocks = markdown_to_blocks(md)
    assert [b["type"] for b in blocks] == ["heading_2", "table", "bulleted_list_item"]
    table = blocks[1]["table"]
    assert table["table_width"] == 3 and len(table["children"]) == 3
    assert blocks[2]["bulleted_list_item"]["rich_text"][0]["annotations"]["bold"]


def test_log_redaction():
    import logging
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "token %s and https://api.telegram.org/bot123:ABCDEFGHIJKLMNOPQRSTUVWX/x",
                            ("sk-ant-secret-value",), None)
    RedactingFilter(["sk-ant-secret-value"]).filter(rec)
    assert "secret" not in rec.getMessage() and "ABCDEFGH" not in rec.getMessage()


def test_tool_results_of_unknown_tool_are_errors(make_app):
    app = make_app()
    task = app.tasks.create(agent_id="ava", title="t")
    res, err = execute(ToolContext(app=app, task=task, agent=get_agent("ava")), "toolu_z", "delegate_task", {})
    assert err and "not available to Ava" in res
    _ = last_tool_results
