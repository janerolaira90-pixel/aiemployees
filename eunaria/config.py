"""Runtime configuration loaded from environment variables (and an optional .env file).

Secrets are only ever read from the environment. Nothing in this module logs them.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SECRET_KEYS = (
    "ANTHROPIC_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "NOTION_TOKEN",
    "GHL_PRIVATE_TOKEN",
    "META_ACCESS_TOKEN",
)


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, no interpolation. Existing env wins."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _ids(value: str | None) -> frozenset[int]:
    if not value:
        return frozenset()
    return frozenset(int(v) for v in value.replace(" ", "").split(",") if v)


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None = None
    model: str = "claude-opus-5-5"                 # Atlas (lead)
    specialist_model: str = "claude-sonnet-5-5"    # creative and specialist employees
    light_model: str = "claude-haiku-5-5"          # reporting employees (Grace, Ava, Maya)
    web_search_max_uses: int = 3
    telegram_bot_token: str | None = None
    telegram_admin_ids: frozenset[int] = frozenset()
    notion_token: str | None = None
    notion_api_version: str = "2025-09-03"
    ghl_token: str | None = None
    ghl_location_id: str | None = None
    ghl_api_version: str = "2021-07-28"
    meta_access_token: str | None = None
    meta_api_version: str = "v23.0"
    meta_page_ids: tuple[str, ...] = ()
    meta_ig_user_ids: tuple[str, ...] = ()
    meta_ad_account_ids: tuple[str, ...] = ()
    db_path: Path = ROOT / "data" / "eunaria.db"
    workspace_config: Path = ROOT / "config" / "workspace.toml"
    timezone: str = "Asia/Manila"
    worker_threads: int = 3
    daily_briefing_time: str = "08:00"
    overdue_check_time: str = "09:00"
    enable_web_search: bool = True
    progress_updates: bool = True
    daily_cost_limit_usd: float = 25.0
    max_tool_iterations: int = 30
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, dotenv: Path | None = ROOT / ".env") -> "Settings":
        if dotenv is not None:
            load_dotenv(dotenv)
        def env(key: str) -> str | None:
            # Values pasted into hosting dashboards often carry stray spaces, line breaks, or
            # quotes. Left in, they make HTTP headers invalid (surfacing as "Connection error").
            value = os.environ.get(key)
            if value is None:
                return None
            return value.strip().strip('"').strip("'").strip()
        split = lambda k: tuple(v for v in (env(k) or "").replace(" ", "").split(",") if v)  # noqa: E731
        return cls(
            anthropic_api_key=env("ANTHROPIC_API_KEY") or None,
            model=env("EUNARIA_MODEL") or "claude-opus-5-5",
            specialist_model=env("EUNARIA_SPECIALIST_MODEL") or "claude-sonnet-5-5",
            light_model=env("EUNARIA_LIGHT_MODEL") or "claude-haiku-5-5",
            web_search_max_uses=int(env("EUNARIA_WEB_SEARCH_MAX_USES") or 3),
            telegram_bot_token=env("TELEGRAM_BOT_TOKEN") or None,
            telegram_admin_ids=_ids(env("TELEGRAM_ADMIN_USER_IDS")),
            notion_token=env("NOTION_TOKEN") or None,
            notion_api_version=env("NOTION_API_VERSION") or "2025-09-03",
            ghl_token=env("GHL_PRIVATE_TOKEN") or None,
            ghl_location_id=env("GHL_LOCATION_ID") or None,
            meta_access_token=env("META_ACCESS_TOKEN") or None,
            meta_api_version=env("META_API_VERSION") or "v23.0",
            meta_page_ids=split("META_PAGE_IDS"),
            meta_ig_user_ids=split("META_IG_USER_IDS"),
            meta_ad_account_ids=split("META_AD_ACCOUNT_IDS"),
            db_path=Path(env("EUNARIA_DB_PATH") or ROOT / "data" / "eunaria.db"),
            workspace_config=Path(env("EUNARIA_WORKSPACE_CONFIG") or ROOT / "config" / "workspace.toml"),
            timezone=env("EUNARIA_TIMEZONE") or "Asia/Manila",
            worker_threads=int(env("EUNARIA_WORKER_THREADS") or 3),
            daily_briefing_time=env("EUNARIA_DAILY_BRIEFING_TIME") or "08:00",
            overdue_check_time=env("EUNARIA_OVERDUE_CHECK_TIME") or "09:00",
            enable_web_search=_bool(env("EUNARIA_ENABLE_WEB_SEARCH"), True),
            progress_updates=_bool(env("EUNARIA_PROGRESS_UPDATES"), True),
            daily_cost_limit_usd=float(env("EUNARIA_DAILY_COST_LIMIT_USD") or 25.0),
            max_tool_iterations=int(env("EUNARIA_MAX_TOOL_ITERATIONS") or 30),
        )

    def model_for(self, tier: str) -> str:
        return {"lead": self.model, "light": self.light_model}.get(tier, self.specialist_model)

    def secret_values(self) -> list[str]:
        vals = [self.anthropic_api_key, self.telegram_bot_token, self.notion_token,
                self.ghl_token, self.meta_access_token]
        return [v for v in vals if v and len(v) >= 6]


@dataclass(frozen=True)
class ClientConfig:
    key: str
    name: str
    notion_page: str | None
    data_sources: dict[str, str]
    notes: str = ""
    aliases: tuple[str, ...] = ()
    auto_detect: bool = True


@dataclass(frozen=True)
class Workspace:
    clients: dict[str, ClientConfig]
    shared_data_sources: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> "Workspace":
        if not path.exists():
            return cls(clients={}, shared_data_sources={})
        data = tomllib.loads(path.read_text())
        clients = {}
        for key, c in data.get("clients", {}).items():
            clients[key] = ClientConfig(
                key=key,
                name=c.get("name", key),
                notion_page=c.get("notion_page"),
                data_sources=dict(c.get("data_sources", {})),
                notes=c.get("notes", ""),
                aliases=tuple(c.get("aliases", [])),
                auto_detect=bool(c.get("auto_detect", True)),
            )
        return cls(clients=clients, shared_data_sources=dict(data.get("shared", {}).get("data_sources", {})))

    def resolve_client(self, text: str | None) -> str | None:
        """Map a free-text client mention ("NULA", "nu.la", "Inoue") to a client key."""
        if not text:
            return None
        norm = _norm(text)
        for key, c in self.clients.items():
            if norm in {_norm(n) for n in (key, c.name, *c.aliases)}:
                return key
        return None

    def detect_client(self, text: str) -> str | None:
        """Find exactly one client mentioned in a request (word match on key, name, or alias)."""
        tokens = re.findall(r"[a-z0-9]+", text.lower())
        padded, token_set = " " + " ".join(tokens) + " ", set(tokens)
        found = set()
        for key, c in self.clients.items():
            if not c.auto_detect:
                continue
            for n in (key, c.name, *c.aliases):
                phrase = " ".join(re.findall(r"[a-z0-9]+", n.lower()))
                if phrase and (f" {phrase} " in padded or _norm(n) in token_set):
                    found.add(key)
        return found.pop() if len(found) == 1 else None


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())
