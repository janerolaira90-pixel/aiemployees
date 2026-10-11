"""Telegram adapter against a mocked Bot API."""

from __future__ import annotations

import json

import httpx

from eunaria.integrations.telegram import TelegramClient, split_message


def test_send_falls_back_to_plain_text_and_attaches_buttons_to_last_chunk():
    calls = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        calls.append((request.url.path.rsplit("/", 1)[-1], body))
        if body.get("parse_mode") == "HTML" and "<b" in body["text"] and "</b>" not in body["text"]:
            return httpx.Response(200, json={"ok": False, "description": "Bad Request: can't parse entities"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(calls)}})

    tg = TelegramClient("123:ABC", transport=httpx.MockTransport(handler))
    mid = tg.send(7, "<b>broken", buttons=[[("✅ Approve", "ap:1:y")]])
    assert mid == 2 and "parse_mode" not in calls[-1][1]
    assert calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "ap:1:y"


def test_long_messages_are_split():
    parts = split_message("line\n" * 2000, limit=4000)
    assert len(parts) == 3 and all(len(p) <= 4000 for p in parts)
