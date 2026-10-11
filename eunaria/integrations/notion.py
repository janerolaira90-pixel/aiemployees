"""Notion REST adapter (API version 2025-09-03: databases contain data sources).

Uses an internal-integration token (NOTION_TOKEN). The integration must be shared with the
client dashboards it should access. Every write is verified by reading the page back.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

BASE = "https://api.notion.com/v1"
RICH_TEXT_LIMIT = 2000


class NotionError(RuntimeError):
    pass


class NotionClient:
    def __init__(self, token: str, version: str = "2025-09-03", transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(
            base_url=BASE,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": version,
                     "Content-Type": "application/json"},
            timeout=30,
            transport=transport,
        )
        self._ds_cache: dict[str, str] = {}
        self._schema_cache: dict[str, dict] = {}

    # --- HTTP -----------------------------------------------------------------------------

    def _req(self, method: str, path: str, json: dict | None = None, params: dict | None = None) -> dict:
        for attempt in range(4):
            r = self.http.request(method, path, json=json, params=params)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            if r.status_code >= 400:
                try:
                    body = r.json()
                    msg = f"{body.get('code')}: {body.get('message')}"
                except ValueError:
                    msg = r.text[:300]
                raise NotionError(f"Notion {method} {path} failed ({r.status_code}) — {msg}")
            return r.json()
        raise NotionError(f"Notion {method} {path} failed after retries (rate limited or unavailable)")

    # --- data sources ---------------------------------------------------------------------

    def resolve_data_source(self, ref: str) -> str:
        """Accepts a database ID or 'ds:<data source id>' and returns the data source ID."""
        if ref.startswith("ds:"):
            return ref[3:]
        if ref not in self._ds_cache:
            db = self._req("GET", f"/databases/{ref}")
            sources = db.get("data_sources") or []
            if not sources:
                raise NotionError(f"Database {ref} has no data sources visible to the integration")
            self._ds_cache[ref] = sources[0]["id"]
        return self._ds_cache[ref]

    def schema(self, ds_id: str) -> dict[str, dict]:
        if ds_id not in self._schema_cache:
            ds = self._req("GET", f"/data_sources/{ds_id}")
            self._schema_cache[ds_id] = ds.get("properties", {})
        return self._schema_cache[ds_id]

    def title_property(self, ds_id: str) -> str:
        for name, prop in self.schema(ds_id).items():
            if prop.get("type") == "title":
                return name
        raise NotionError("Data source has no title property")

    def query(self, ds_id: str, *, filter: dict | None = None, page_size: int = 25) -> list[dict]:
        body: dict[str, Any] = {"page_size": page_size}
        if filter:
            body["filter"] = filter
        return self._req("POST", f"/data_sources/{ds_id}/query", json=body).get("results", [])

    def find_by_title(self, ds_id: str, text: str, page_size: int = 10) -> list[dict]:
        prop = self.title_property(ds_id)
        return self.query(ds_id, filter={"property": prop, "title": {"contains": text}}, page_size=page_size)

    # --- pages ----------------------------------------------------------------------------

    def create_page(self, ds_id: str, properties: dict[str, Any], body_markdown: str | None = None) -> dict:
        props = self.build_properties(ds_id, properties)
        children = markdown_to_blocks(body_markdown) if body_markdown else []
        page = self._req("POST", "/pages", json={
            "parent": {"type": "data_source_id", "data_source_id": ds_id},
            "properties": props,
            "children": children[:100],
        })
        if len(children) > 100:
            self.append_blocks(page["id"], children[100:])
        return page

    def update_page(self, page_id: str, ds_id: str, properties: dict[str, Any]) -> dict:
        return self._req("PATCH", f"/pages/{page_id}", json={"properties": self.build_properties(ds_id, properties)})

    def get_page(self, page_id: str) -> dict:
        return self._req("GET", f"/pages/{page_id}")

    def append_blocks(self, block_id: str, children: list[dict]) -> None:
        for i in range(0, len(children), 100):
            self._req("PATCH", f"/blocks/{block_id}/children", json={"children": children[i:i + 100]})

    def read_blocks_text(self, block_id: str, max_blocks: int = 300) -> str:
        lines, cursor, seen = [], None, 0
        while seen < max_blocks:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self._req("GET", f"/blocks/{block_id}/children", params=params)
            for b in data.get("results", []):
                seen += 1
                t = b.get("type")
                rich = (b.get(t) or {}).get("rich_text")
                if rich is not None:
                    lines.append(plain_text(rich))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return "\n".join(lines)

    # --- property conversion ----------------------------------------------------------------

    def build_properties(self, ds_id: str, values: dict[str, Any]) -> dict[str, Any]:
        schema = self.schema(ds_id)
        lower = {k.strip().lower(): k for k in schema}
        out: dict[str, Any] = {}
        unknown = []
        for name, value in values.items():
            key = name if name in schema else lower.get(name.strip().lower())
            if key is None:
                unknown.append(name)
                continue
            out[key] = convert_value(schema[key], value)
        if unknown:
            valid = ", ".join(f"{k} ({v.get('type')})" for k, v in schema.items())
            raise NotionError(f"Unknown properties {unknown}. Valid properties: {valid}")
        return out


def convert_value(prop: dict, value: Any) -> dict:
    t = prop.get("type")
    if t == "title":
        return {"title": rich_text(str(value))}
    if t == "rich_text":
        return {"rich_text": rich_text(str(value))}
    if t == "select":
        return {"select": {"name": str(value)} if value not in (None, "") else None}
    if t == "status":
        return {"status": {"name": str(value)}}
    if t == "multi_select":
        items = value if isinstance(value, list) else [v.strip() for v in str(value).split(",") if v.strip()]
        return {"multi_select": [{"name": str(v)} for v in items]}
    if t == "date":
        if isinstance(value, dict):
            return {"date": value}
        return {"date": {"start": str(value)} if value else None}
    if t == "checkbox":
        return {"checkbox": value if isinstance(value, bool) else str(value).lower() in ("true", "yes", "1")}
    if t == "number":
        return {"number": float(value) if value not in (None, "") else None}
    if t == "url":
        return {"url": str(value) or None}
    if t == "email":
        return {"email": str(value) or None}
    if t == "phone_number":
        return {"phone_number": str(value) or None}
    if t == "relation":
        ids = value if isinstance(value, list) else [value]
        return {"relation": [{"id": str(i)} for i in ids if i]}
    if t == "people":
        ids = value if isinstance(value, list) else [value]
        return {"people": [{"id": str(i)} for i in ids if i]}
    raise NotionError(f"Property type {t!r} cannot be written by the AI employees")


def rich_text(text: str) -> list[dict]:
    chunks = [text[i:i + RICH_TEXT_LIMIT] for i in range(0, len(text), RICH_TEXT_LIMIT)] or [""]
    return [{"type": "text", "text": {"content": c}} for c in chunks]


def plain_text(rich: list[dict]) -> str:
    return "".join(r.get("plain_text") or r.get("text", {}).get("content", "") for r in rich)


def page_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return plain_text(prop.get("title", []))
    return ""


def _inline(text: str) -> list[dict]:
    """Rich text with **bold** support; everything else is plain."""
    out: list[dict] = []
    for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if not part:
            continue
        for chunk in rich_text(part):
            if i % 2:
                chunk["annotations"] = {"bold": True}
            out.append(chunk)
    return out or rich_text("")


def markdown_to_blocks(md: str) -> list[dict]:
    """Small Markdown → Notion blocks converter: headings, lists, quotes, dividers, tables, paragraphs."""
    blocks: list[dict] = []
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        s = line.strip()
        if not s:
            i += 1
            continue
        if s.startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not re.match(r"^\s*\|?\s*:?-{2,}", lines[i]):
                    rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            width = max(len(r) for r in rows)
            blocks.append({"object": "block", "type": "table", "table": {
                "table_width": width, "has_column_header": True, "has_row_header": False,
                "children": [{"object": "block", "type": "table_row", "table_row": {
                    "cells": [_inline(r[c]) if c < len(r) else rich_text("") for c in range(width)]}}
                    for r in rows],
            }})
            continue
        if s in ("---", "***"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        elif m := re.match(r"^(#{1,3})\s+(.*)", s):
            kind = f"heading_{len(m.group(1))}"
            blocks.append({"object": "block", "type": kind, kind: {"rich_text": _inline(m.group(2))}})
        elif m := re.match(r"^[-*•]\s+(.*)", s):
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": _inline(m.group(1))}})
        elif m := re.match(r"^\d+[.)]\s+(.*)", s):
            blocks.append({"object": "block", "type": "numbered_list_item",
                           "numbered_list_item": {"rich_text": _inline(m.group(1))}})
        elif s.startswith(">"):
            blocks.append({"object": "block", "type": "quote", "quote": {"rich_text": _inline(s.lstrip("> "))}})
        else:
            blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": _inline(s)}})
        i += 1
    return blocks
