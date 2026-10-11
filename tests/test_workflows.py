"""End-to-end behaviour of the Telegram → Atlas → specialists → Notion → report loop."""

from __future__ import annotations

import json
import re

from eunaria.bot import Bot
from eunaria.routing import route

from .conftest import ADMIN, CHAT, drain, owner_msg
from .fakes import ScriptedLLM, last_tool_results, text, thinking, tool_use


def _result_json(messages, i=0):
    return json.loads(last_tool_results(messages)[i]["content"])


def sent_texts(app):
    return [m["text"] for m in app.messenger.sent]


# --- routing ---------------------------------------------------------------------------------------

def test_named_agent_routing():
    assert route("Sofia, create a social media strategy for Client A.").agent.id == "sofia"
    assert route("@chloe add 20 content ideas").agent.id == "chloe"
    assert route("/theo write five talking-head scripts").agent.id == "theo"
    assert route("Hey Grace: today's GHL report?").agent.id == "grace"
    assert route("MAYA - which deliverables are overdue?").agent.id == "maya"
    r = route("Atlas, give me my daily briefing.")
    assert r.agent.id == "atlas" and r.text == "give me my daily briefing."
    # A name used inside a sentence is not an address; default goes to Atlas.
    assert route("Plan content like Sofia suggested").agent.id == "atlas"
    assert route("Leonardo's clinic needs ads").agent.id == "atlas"


def test_unauthorized_users_are_ignored(make_app):
    app = make_app()
    Bot(app).handle_update(owner_msg("Atlas, list everything", user=999))
    assert app.tasks.list() == []
    assert "not authorized" in sent_texts(app)[0].lower()
    Bot(app).handle_update(owner_msg("again", user=999, update_id=2))
    assert len(app.messenger.sent) == 1  # told once, then silent


# --- the core coordinated workflow ----------------------------------------------------------------

def test_atlas_coordinates_specialists_and_verifies_notion_records(make_app, fake_notion):
    cal_page = fake_notion.seed_page("ds-db-nula-cal", "Botox myths reel")
    llm = ScriptedLLM()
    llm.add(
        "atlas",
        [thinking(), tool_use("delegate_task", agent="chloe", title="2 content ideas for nu.la",
                              instructions="Add 2 ideas to nu.la Content Ideas in Notion.",
                              requires=[{"kind": "notion_page", "target": "nula/content_ideas", "min": 2}])],
        lambda m: [tool_use("delegate_task", agent="clara", title="Caption for Botox myths reel",
                            instructions=f"Write the caption on page {cal_page}.",
                            depends_on=[_result_json(m)["task"]])],
        [tool_use("submit_result", outcome="completed", summary="All done")],   # premature: must be refused
        [text("Subtasks are running; I'll review when they finish.")],
        lambda m: [tool_use("submit_result", outcome="completed",
                            summary="nu.la: 2 ideas in Notion and the reel caption is ready for approval.",
                            links=re.findall(r"https://www\.notion\.so/[\w-]+", json.dumps(m[-1])))],
    )
    llm.add(
        "chloe",
        [tool_use("notion_find_pages", database="content_ideas", title_contains="Botox")],
        [tool_use("notion_create_page", database="content_ideas",
                  properties={"Topic": "3 Botox myths", "Description": "TOFU myth-busting reel"},
                  body_markdown="## Details\n- Funnel: TOFU\n- Platform: Instagram")],
        [tool_use("notion_create_page", database="content_ideas", properties={"Topic": "3 Botox myths"})],
        [tool_use("notion_create_page", database="content_ideas",
                  properties={"Topic": "What a consultation looks like", "Description": "MOFU"})],
        [tool_use("submit_result", outcome="completed", summary="Added 2 ideas (1 duplicate skipped).")],
    )
    llm.add(
        "clara",
        [tool_use("notion_update_page", page_id=cal_page,
                  properties={"Caption": "Myth: Botox freezes your face. Truth: …", "Caption Approval": "Waiting For Approval"})],
        [tool_use("submit_result", outcome="completed", summary="Caption saved, waiting for approval.")],
    )
    app = make_app(llm)
    bot = Bot(app)
    bot.handle_update(owner_msg("Atlas, plan next week's content for NULA."))
    root = app.tasks.list()[0]
    assert root.agent_id == "atlas" and root.client == "nula"
    assert "T-0001" in sent_texts(app)[0]

    drain(app)

    root = app.tasks.get(root.id)
    kids = app.tasks.children(root.id)
    assert [k.agent_id for k in kids] == ["chloe", "clara"]
    assert all(k.status == "done" and k.outcome == "completed" for k in kids)
    assert kids[1].depends_on == [kids[0].id]
    assert root.status == "done" and root.outcome == "completed"

    # Premature completion was refused with an honest error.
    atlas_calls = [c for c in llm.calls if c["agent"] == "atlas"]
    premature = last_tool_results(atlas_calls[3]["messages"])[0]
    assert premature.get("is_error") and "Subtasks still in progress" in premature["content"]

    # Real Notion records: 2 created (duplicate refused), 1 updated; all verified.
    ideas = [p for p in fake_notion.pages.values() if p["parent"]["data_source_id"] == "ds-db-nula-ideas"]
    assert sorted(fake_notion._title(p) for p in ideas) == ["3 Botox myths", "What a consultation looks like"]
    caption = fake_notion.pages[cal_page]["properties"]["Caption"]["rich_text"][0]["plain_text"]
    assert caption.startswith("Myth: Botox")
    dels = app.tasks.deliverables(app.tasks.subtree_ids(root.id))
    assert len(dels) == 3 and all(d["verified"] for d in dels)

    # Clara received Chloe's results as upstream context.
    clara_first = [c for c in llm.calls if c["agent"] == "clara"][0]["messages"]
    assert "Results from upstream tasks" in json.dumps(clara_first)

    # Thinking blocks are replayed unchanged on the next request (append-only history).
    first_atlas_reply = atlas_calls[0]
    second = atlas_calls[1]["messages"]
    assert second[1]["role"] == "assistant" and second[1]["content"][0]["type"] == "thinking"
    assert first_atlas_reply["effort"] == "high"

    # The owner got: ack, two progress updates, and a final report with Notion links.
    final = sent_texts(app)[-1]
    assert "T-0001" in final and "Completed" in final and "notion.so" in final
    assert sum("✅ T-000" in t for t in sent_texts(app)) >= 2


def test_cannot_complete_without_verified_deliverables(make_app):
    llm = ScriptedLLM()
    llm.add(
        "chloe",
        [tool_use("submit_result", outcome="completed", summary="Here are 20 ideas: …")],
        [tool_use("submit_result", outcome="partial", summary="Wrote ideas but did not save them.",
                  issues="Notion records not created")],
    )
    app = make_app(llm)
    task = app.tasks.create(agent_id="chloe", title="20 ideas", client="nula", chat_id=CHAT,
                            requires=[{"kind": "notion_page", "target": "nula/content_ideas", "min": 20}])
    from eunaria.orchestration import enqueue_run
    enqueue_run(app, task.id)
    drain(app)
    t = app.tasks.get(task.id)
    refusal = last_tool_results(llm.calls[1]["messages"])[0]
    assert refusal["is_error"] and "0/20 verified" in refusal["content"]
    assert t.status == "done" and t.outcome == "partial"


def test_notion_not_connected_is_reported_honestly(make_app):
    llm = ScriptedLLM()
    llm.add("chloe",
            [tool_use("notion_create_page", database="content_ideas", properties={"Topic": "x"})],
            lambda m: [tool_use("report_blocked", blocker=last_tool_results(m)[0]["content"],
                                needed_from_owner="NOTION_TOKEN")])
    app = make_app(llm, notion=False)
    bot = Bot(app)
    bot.handle_update(owner_msg("Chloe, add 1 idea for NULA to Notion"))
    drain(app)
    t = app.tasks.list()[0]
    assert t.status == "blocked" and "Notion is not connected" in t.error
    assert "is blocked" in sent_texts(app)[-1]


def test_client_separation_enforced(make_app):
    llm = ScriptedLLM()
    llm.add("chloe",
            [tool_use("notion_create_page", client="inoue", database="content_ideas", properties={"Topic": "x"})],
            [tool_use("submit_result", outcome="failed", summary="Wrong client")])
    app = make_app(llm)
    Bot(app).handle_update(owner_msg("Chloe, add an idea for NULA"))
    drain(app)
    res = last_tool_results(llm.calls[1]["messages"])[0]
    assert res["is_error"] and "Client separation" in res["content"]


def test_tool_permissions_enforced(make_app):
    llm = ScriptedLLM()
    llm.add("clara", [tool_use("ghl_add_contact_tags", contact_id="c1", tags=["x"], reason="r")],
            [tool_use("submit_result", outcome="failed", summary="no access")])
    app = make_app(llm)
    Bot(app).handle_update(owner_msg("Clara, tag contact c1"))
    drain(app)
    res = last_tool_results(llm.calls[1]["messages"])[0]
    assert res["is_error"] and "not available to Clara" in res["content"]
    assert "ghl_add_contact_tags" not in llm.calls[0]["tools"]


def test_owner_reply_continues_the_task(make_app):
    llm = ScriptedLLM()
    llm.add("sofia", [tool_use("submit_result", outcome="completed", summary="Draft strategy v1")],
            lambda m: [tool_use("submit_result", outcome="completed",
                                summary="v2 with " + ("TikTok" if "TikTok" in json.dumps(m[-1]) else "?"))])
    app = make_app(llm)
    bot = Bot(app)
    bot.handle_update(owner_msg("Sofia, strategy for NULA"))
    drain(app)
    final_mid = app.messenger.sent[-1]["message_id"]
    bot.handle_update(owner_msg("Add TikTok please", reply_to=final_mid, update_id=2))
    drain(app)
    t = app.tasks.list()[0]
    assert t.result_summary == "v2 with TikTok" and len(app.tasks.list()) == 1


def test_daily_cost_limit_blocks_new_runs(make_app):
    app = make_app(ScriptedLLM(), daily_cost_limit_usd=0.0001)
    app.db.execute("INSERT INTO agent_runs(task_id, agent_id, model, cost_usd, ts) VALUES (0,'atlas','m',1.0,?)",
                   (__import__("eunaria.db", fromlist=["now_iso"]).now_iso(),))
    Bot(app).handle_update(owner_msg("Atlas, briefing"))
    drain(app)
    assert app.tasks.list()[0].status == "blocked"
    assert "cost limit" in sent_texts(app)[-1]


def test_status_and_task_commands(make_app):
    app = make_app()
    bot = Bot(app)
    bot.handle_update(owner_msg("Leo, find 10 dental clinics in Makati"))
    bot.handle_update(owner_msg("/status", update_id=2))
    assert "Leo" in sent_texts(app)[-1] and "T-0001" in sent_texts(app)[-1]
    bot.handle_update(owner_msg("/task T-0001", update_id=3))
    assert "Lead Generation" in sent_texts(app)[-1]
    bot.handle_update(owner_msg("/agents", update_id=4))
    assert sent_texts(app)[-1].count("—") == 14
    _ = ADMIN


def test_direct_chloe_request_implies_verified_notion_records():
    from eunaria.bot import implied_requirements
    assert implied_requirements("chloe", "add 20 content ideas to Notion", "nula") == [
        {"kind": "notion_page", "target": "nula/content_ideas", "min": 20}]
    assert implied_requirements("chloe", "brainstorm 20 ideas", "nula") == []        # not a Notion request
    assert implied_requirements("chloe", "add 20 ideas to Notion", None) == []       # client unknown
    assert implied_requirements("sofia", "add 20 ideas to Notion", "nula") == []
