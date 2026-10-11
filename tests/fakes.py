"""Test doubles: scripted Claude, in-memory Notion and GHL APIs (via httpx.MockTransport)."""

from __future__ import annotations

import copy
import itertools
import json
import re
import uuid
from typing import Callable

import httpx

from eunaria.llm import LLMResponse

_ids = itertools.count(1)


def tool_use(name: str, **inp) -> dict:
    return {"type": "tool_use", "id": f"toolu_{next(_ids):05d}", "name": name, "input": inp}


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def thinking(t: str = "") -> dict:
    return {"type": "thinking", "thinking": t, "signature": f"sig-{uuid.uuid4().hex[:8]}"}


Turn = list[dict] | Callable[[list[dict]], list[dict]]


class ScriptedLLM:
    """Plays back per-agent scripted turns and records every request."""

    model = "claude-opus-5-5"

    def __init__(self, scripts: dict[str, list[Turn]] | None = None):
        self.scripts = {k: list(v) for k, v in (scripts or {}).items()}
        self.calls: list[dict] = []

    def add(self, agent_id: str, *turns: Turn) -> None:
        self.scripts.setdefault(agent_id, []).extend(turns)

    def create(self, *, system, messages, tools, effort, agent_id, model=None) -> LLMResponse:
        self.calls.append({"agent": agent_id, "model": model, "messages": copy.deepcopy(messages),
                           "tools": [t["name"] for t in tools], "effort": effort, "system": system})
        queue = self.scripts.get(agent_id) or []
        if not queue:
            raise AssertionError(f"No scripted turn left for {agent_id}")
        turn = queue.pop(0)
        content = turn(messages) if callable(turn) else turn
        stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
        return LLMResponse(content=content, stop_reason=stop, model=self.model,
                           usage={"input_tokens": 1000, "output_tokens": 200})


def last_tool_results(messages: list[dict]) -> list[dict]:
    last = messages[-1]
    return [b for b in last["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]


# --- Notion ------------------------------------------------------------------------------------

class FakeNotion:
    def __init__(self):
        self.databases: dict[str, str] = {}          # database id -> data source id
        self.schemas: dict[str, dict] = {}           # ds id -> properties
        self.pages: dict[str, dict] = {}
        self.blocks: dict[str, list[dict]] = {}
        self.requests: list[tuple[str, str]] = []

    def add_db(self, db_id: str, properties: dict[str, str]) -> str:
        ds = f"ds-{db_id}"
        self.databases[db_id] = ds
        schema = {}
        for name, t in properties.items():
            schema[name] = {"id": name, "name": name, "type": t, t: {"options": []} if t in ("select", "multi_select") else {}}
        self.schemas[ds] = schema
        return ds

    def seed_page(self, ds: str, title: str, **props) -> str:
        pid = f"page-{uuid.uuid4().hex[:12]}"
        title_prop = next(n for n, p in self.schemas[ds].items() if p["type"] == "title")
        properties = {title_prop: {"type": "title", "title": [{"plain_text": title, "text": {"content": title}}]}}
        for k, v in props.items():
            properties[k] = {"type": "rich_text", "rich_text": [{"plain_text": v}]}
        self.pages[pid] = {"id": pid, "url": f"https://www.notion.so/{pid}", "parent": {"type": "data_source_id",
                           "data_source_id": ds}, "properties": properties}
        return pid

    def _title(self, page):
        for p in page["properties"].values():
            if p["type"] == "title":
                return "".join(x.get("plain_text") or x["text"]["content"] for x in p["title"])
        return ""

    def _materialize(self, ds: str, props: dict) -> dict:
        out = {}
        for name, val in props.items():
            t = self.schemas[ds][name]["type"]
            v = val[t]
            if t in ("title", "rich_text"):
                v = [{"plain_text": x["text"]["content"], **x} for x in v]
            out[name] = {"type": t, t: v}
        return out

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path.removeprefix("/v1"), request.method
        self.requests.append((method, path))
        body = json.loads(request.content) if request.content else {}
        if m := re.fullmatch(r"/databases/([\w-]+)", path):
            ds = self.databases.get(m.group(1))
            if not ds:
                return httpx.Response(404, json={"code": "object_not_found", "message": "not shared"})
            return httpx.Response(200, json={"id": m.group(1), "data_sources": [{"id": ds, "name": "x"}]})
        if m := re.fullmatch(r"/data_sources/([\w-]+)", path):
            return httpx.Response(200, json={"id": m.group(1), "properties": self.schemas[m.group(1)]})
        if m := re.fullmatch(r"/data_sources/([\w-]+)/query", path):
            ds = m.group(1)
            res = [p for p in self.pages.values() if p["parent"]["data_source_id"] == ds]
            f = body.get("filter")
            if f and "title" in f:
                needle = f["title"]["contains"].lower()
                res = [p for p in res if needle in self._title(p).lower()]
            return httpx.Response(200, json={"results": res[: body.get("page_size", 100)], "has_more": False})
        if path == "/pages" and method == "POST":
            ds = body["parent"]["data_source_id"]
            pid = f"page-{uuid.uuid4().hex[:12]}"
            self.pages[pid] = {"id": pid, "url": f"https://www.notion.so/{pid}",
                               "parent": {"type": "data_source_id", "data_source_id": ds},
                               "properties": self._materialize(ds, body["properties"])}
            self.blocks[pid] = list(body.get("children", []))
            return httpx.Response(200, json=self.pages[pid])
        if m := re.fullmatch(r"/pages/([\w-]+)", path):
            page = self.pages.get(m.group(1))
            if not page:
                return httpx.Response(404, json={"code": "object_not_found", "message": "no page"})
            if method == "PATCH":
                ds = page["parent"]["data_source_id"]
                page["properties"].update(self._materialize(ds, body["properties"]))
            return httpx.Response(200, json=page)
        if m := re.fullmatch(r"/blocks/([\w-]+)/children", path):
            if method == "PATCH":
                self.blocks.setdefault(m.group(1), []).extend(body["children"])
                return httpx.Response(200, json={"results": body["children"]})
            return httpx.Response(200, json={"results": self.blocks.get(m.group(1), []), "has_more": False})
        return httpx.Response(400, json={"code": "unhandled", "message": f"{method} {path}"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


# --- GHL --------------------------------------------------------------------------------------------

class FakeGHL:
    def __init__(self):
        self.contacts = {"c1": {"id": "c1", "firstName": "Ana", "companyName": "Glow Clinic", "tags": []}}
        self.writes: list[tuple[str, str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        if path == "/contacts/search":
            return httpx.Response(200, json={"contacts": list(self.contacts.values())})
        if m := re.fullmatch(r"/contacts/(\w+)/tags", path):
            self.writes.append((method, path, body))
            self.contacts[m.group(1)]["tags"] += body["tags"]
            return httpx.Response(200, json={"tags": self.contacts[m.group(1)]["tags"]})
        if m := re.fullmatch(r"/contacts/(\w+)", path):
            return httpx.Response(200, json={"contact": self.contacts[m.group(1)]})
        if path == "/opportunities/pipelines":
            return httpx.Response(200, json={"pipelines": [{"id": "p1", "name": "Sales", "stages": []}]})
        return httpx.Response(404, json={"message": "unhandled"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
