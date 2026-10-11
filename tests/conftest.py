from __future__ import annotations

from pathlib import Path

import pytest

from eunaria.app import App
from eunaria.config import ROOT, ClientConfig, Settings, Workspace
from eunaria.db import Database
from eunaria.integrations.ghl import GHLClient
from eunaria.integrations.notion import NotionClient
from eunaria.integrations.telegram import ConsoleMessenger
from eunaria.service import make_worker

from .fakes import FakeGHL, FakeNotion, ScriptedLLM

ADMIN = 42
CHAT = 4242


@pytest.fixture
def fake_notion():
    fn = FakeNotion()
    fn.add_db("db-nula-ideas", {"Topic": "title", "Description": "rich_text", "Visual Inspo": "url",
                                "Added to Calendar": "checkbox"})
    fn.add_db("db-nula-cal", {"Name": "title", "Caption": "rich_text", "Content Pillar": "select",
                              "Caption Approval": "select"})
    fn.add_db("db-inoue-ideas", {"Topic": "title", "Description": "rich_text"})
    return fn


@pytest.fixture
def fake_ghl():
    return FakeGHL()


@pytest.fixture
def make_app(tmp_path: Path, fake_notion, fake_ghl):
    def _make(llm: ScriptedLLM | None = None, *, notion: bool = True, ghl: bool = True, **overrides) -> App:
        settings = Settings(
            db_path=tmp_path / "test.db", telegram_admin_ids=frozenset({ADMIN}), enable_web_search=False,
            workspace_config=ROOT / "config" / "workspace.toml", progress_updates=True, **overrides,
        )
        workspace = Workspace(
            clients={
                "eunaria": ClientConfig("eunaria", "Eunaria", None, {}),
                "nula": ClientConfig("nula", "nu.la", None, {"content_ideas": "db-nula-ideas",
                                                             "content_calendar": "db-nula-cal"},
                                     aliases=("nula",)),
                "inoue": ClientConfig("inoue", "Inoue", None, {"content_ideas": "db-inoue-ideas"}),
            },
            shared_data_sources={},
        )
        return App(
            settings=settings, db=Database(settings.db_path), workspace=workspace,
            llm=llm or ScriptedLLM(), messenger=ConsoleMessenger(echo=False),
            notion=NotionClient("secret_test", transport=fake_notion.transport()) if notion else None,
            ghl=GHLClient("ghl_test", "loc1", transport=fake_ghl.transport()) if ghl else None,
        )
    return _make


def drain(app: App) -> int:
    return make_worker(app, "test").drain()


def owner_msg(text: str, *, user: int = ADMIN, chat: int = CHAT, reply_to: int | None = None, update_id: int = 1):
    msg = {"message_id": update_id, "from": {"id": user}, "chat": {"id": chat}, "text": text}
    if reply_to:
        msg["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": msg}
