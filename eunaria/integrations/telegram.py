"""Telegram Bot API adapter (long polling — no public webhook endpoint required)."""

from __future__ import annotations

import html
import logging
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)
MAX_LEN = 4000


class Messenger(Protocol):
    def send(self, chat_id: int, text: str, *, buttons: list[list[tuple[str, str]]] | None = None) -> int | None: ...
    def answer_callback(self, callback_id: str, text: str = "") -> None: ...
    def edit_buttons(self, chat_id: int, message_id: int, buttons: list[list[tuple[str, str]]] | None) -> None: ...


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts or [""]


class TelegramClient:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(base_url=f"https://api.telegram.org/bot{token}", timeout=40, transport=transport)

    def _call(self, method: str, **payload: Any) -> Any:
        r = self.http.post(f"/{method}", json=payload)
        data = r.json()
        if not data.get("ok"):
            # Never include the URL (it contains the token) in errors.
            raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
        return data["result"]

    def get_me(self) -> dict:
        return self._call("getMe")

    def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", **payload)

    def send(self, chat_id: int, text: str, *, buttons: list[list[tuple[str, str]]] | None = None) -> int | None:
        last_id = None
        chunks = split_message(text)
        for i, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                                       "disable_web_page_preview": True}
            if buttons and i == len(chunks) - 1:
                payload["reply_markup"] = _markup(buttons)
            try:
                last_id = self._call("sendMessage", **payload)["message_id"]
            except RuntimeError as exc:
                if "parse" in str(exc).lower():  # fall back to plain text if HTML was malformed
                    payload.pop("parse_mode")
                    last_id = self._call("sendMessage", **payload)["message_id"]
                else:
                    raise
        return last_id

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", callback_query_id=callback_id, text=text[:200])

    def edit_buttons(self, chat_id: int, message_id: int, buttons: list[list[tuple[str, str]]] | None) -> None:
        self._call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                   reply_markup=_markup(buttons or []))

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        self._call("setMyCommands", commands=[{"command": c, "description": d} for c, d in commands])


def _markup(buttons: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}


class ConsoleMessenger:
    """Prints instead of sending — used by the local CLI and tests."""

    def __init__(self, echo: bool = True):
        self.sent: list[dict] = []
        self.echo = echo
        self._next = 1

    def send(self, chat_id: int, text: str, *, buttons=None) -> int | None:
        mid = self._next
        self._next += 1
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons, "message_id": mid})
        if self.echo:
            import re
            plain = html.unescape(re.sub(r"<[^>]+>", "", text))
            print(f"\n── to chat {chat_id} ──\n{plain}")
            if buttons:
                print("   [" + "] [".join(t for row in buttons for t, _ in row) + "]")
        return mid

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        pass

    def edit_buttons(self, chat_id: int, message_id: int, buttons=None) -> None:
        pass
