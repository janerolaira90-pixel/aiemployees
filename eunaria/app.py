"""Service container wiring settings, storage, integrations, and the model client together."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .config import Settings, Workspace
from .db import Database, dumps, now_iso
from .integrations.ghl import GHLClient
from .integrations.meta import MetaClient
from .integrations.notion import NotionClient
from .integrations.telegram import ConsoleMessenger, Messenger, TelegramClient
from .llm import LLM, ClaudeLLM, UnconfiguredLLM
from .queue import JobQueue
from .tasks import TaskStore

log = logging.getLogger(__name__)


class Conversations:
    """Append-only per-task model transcript."""

    def __init__(self, db: Database):
        self.db = db

    def load(self, task_id: int) -> list[dict]:
        rows = self.db.query("SELECT role, content FROM messages WHERE task_id=? ORDER BY id", (task_id,))
        return [{"role": r["role"], "content": json.loads(r["content"])} for r in rows]

    def append(self, task_id: int, role: str, content: list[dict] | str) -> None:
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        last = self.db.one("SELECT id, role, content FROM messages WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,))
        if role == "user" and last and last["role"] == "user":
            # Consecutive user content is merged into the tail message (which the model has not
            # answered yet), keeping the transcript a valid alternation without rewriting history.
            merged = json.loads(last["content"]) + content
            self.db.execute("UPDATE messages SET content=? WHERE id=?", (dumps(merged), last["id"]))
            return
        self.db.execute("INSERT INTO messages(task_id, role, content, ts) VALUES (?,?,?,?)",
                        (task_id, role, dumps(content), now_iso()))

    def last_role(self, task_id: int) -> str | None:
        row = self.db.one("SELECT role FROM messages WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,))
        return row["role"] if row else None


class Inbox:
    def __init__(self, db: Database):
        self.db = db

    def put(self, task_id: int, text: str) -> None:
        self.db.execute("INSERT INTO inbox(task_id, text, ts) VALUES (?,?,?)", (task_id, text, now_iso()))

    def drain(self, task_id: int) -> list[str]:
        with self.db.tx() as conn:
            rows = conn.execute("SELECT id, text FROM inbox WHERE task_id=? AND consumed=0 ORDER BY id",
                                (task_id,)).fetchall()
            if rows:
                conn.execute(f"UPDATE inbox SET consumed=1 WHERE id IN ({','.join('?' * len(rows))})",
                             tuple(r["id"] for r in rows))
        return [r["text"] for r in rows]

    def pending(self, task_id: int) -> int:
        return self.db.one("SELECT COUNT(*) n FROM inbox WHERE task_id=? AND consumed=0", (task_id,))["n"]


class TaskLocks:
    def __init__(self, db: Database, ttl_seconds: int = 900):
        self.db = db
        self.ttl = ttl_seconds

    def acquire(self, task_id: int, owner: str) -> bool:
        now = datetime.now(timezone.utc)
        until = (now + timedelta(seconds=self.ttl)).isoformat(timespec="seconds")
        with self.db.tx() as conn:
            row = conn.execute("SELECT owner, until FROM task_locks WHERE task_id=?", (task_id,)).fetchone()
            if row and row["until"] > now.isoformat(timespec="seconds") and row["owner"] != owner:
                return False
            conn.execute("INSERT INTO task_locks(task_id, owner, until) VALUES (?,?,?) "
                         "ON CONFLICT(task_id) DO UPDATE SET owner=excluded.owner, until=excluded.until",
                         (task_id, owner, until))
        return True

    def release(self, task_id: int, owner: str) -> None:
        self.db.execute("DELETE FROM task_locks WHERE task_id=? AND owner=?", (task_id, owner))


@dataclass
class App:
    settings: Settings
    db: Database
    workspace: Workspace
    llm: LLM
    messenger: Messenger
    notion: NotionClient | None = None
    ghl: GHLClient | None = None
    meta: MetaClient | None = None
    tasks: TaskStore = field(init=False)
    queue: JobQueue = field(init=False)
    convo: Conversations = field(init=False)
    inbox: Inbox = field(init=False)
    locks: TaskLocks = field(init=False)

    def __post_init__(self):
        self.tasks = TaskStore(self.db)
        self.queue = JobQueue(self.db)
        self.convo = Conversations(self.db)
        self.inbox = Inbox(self.db)
        self.locks = TaskLocks(self.db)

    @property
    def owner_chat_id(self) -> int | None:
        ids = sorted(self.settings.telegram_admin_ids)
        return ids[0] if ids else None

    def integration_status(self) -> dict[str, str]:
        s = self.settings
        return {
            "claude": "configured" if not isinstance(self.llm, UnconfiguredLLM) else "MISSING ANTHROPIC_API_KEY",
            "telegram": "configured" if s.telegram_bot_token else "MISSING TELEGRAM_BOT_TOKEN",
            "telegram_admins": f"{len(s.telegram_admin_ids)} admin(s)" if s.telegram_admin_ids
                               else "MISSING TELEGRAM_ADMIN_USER_IDS",
            "notion": "configured" if self.notion else "MISSING NOTION_TOKEN",
            "ghl": "configured" if self.ghl else "MISSING GHL_PRIVATE_TOKEN / GHL_LOCATION_ID",
            "meta": "configured" if self.meta else "MISSING META_ACCESS_TOKEN",
        }

    def cost_today_usd(self) -> float:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        row = self.db.one("SELECT COALESCE(SUM(cost_usd),0) c FROM agent_runs WHERE ts >= ?",
                          (start.isoformat(timespec="seconds"),))
        return float(row["c"])


def build_app(settings: Settings, *, messenger: Messenger | None = None, llm: LLM | None = None) -> App:
    db = Database(settings.db_path)
    workspace = Workspace.load(settings.workspace_config)
    if llm is None:
        llm = ClaudeLLM(settings.anthropic_api_key, settings.model) if settings.anthropic_api_key else UnconfiguredLLM()
    if messenger is None:
        messenger = TelegramClient(settings.telegram_bot_token) if settings.telegram_bot_token else ConsoleMessenger()
    notion = NotionClient(settings.notion_token, settings.notion_api_version) if settings.notion_token else None
    ghl = (GHLClient(settings.ghl_token, settings.ghl_location_id, settings.ghl_api_version)
           if settings.ghl_token and settings.ghl_location_id else None)
    meta = MetaClient(settings.meta_access_token, settings.meta_api_version) if settings.meta_access_token else None
    return App(settings=settings, db=db, workspace=workspace, llm=llm, messenger=messenger,
               notion=notion, ghl=ghl, meta=meta)
