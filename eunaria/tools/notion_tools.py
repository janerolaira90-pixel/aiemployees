"""Notion tools with client separation, duplicate prevention, and read-back verification."""

from __future__ import annotations

from ..integrations.notion import NotionError, page_title, plain_text
from .base import ToolContext, ToolError, tool

CLIENT = {"type": "string", "description": "Client key (defaults to the task's client)"}
DATABASE = {"type": "string", "description": "Database key from notion_list_databases, e.g. content_ideas"}


def _notion(ctx: ToolContext):
    if ctx.app.notion is None:
        raise ToolError("Notion is not connected (NOTION_TOKEN missing). Report this as a blocker; "
                        "do not pretend the record was saved.")
    return ctx.app.notion


def _client_key(ctx: ToolContext, requested: str | None) -> str:
    ws = ctx.app.workspace
    key = ws.resolve_client(requested) if requested else None
    if requested and not key:
        raise ToolError(f"Unknown client {requested!r}. Known: {', '.join(ws.clients)}")
    task_client = ctx.task.client
    if task_client and key and key != task_client:
        raise ToolError(f"Client separation: this task is for {task_client}, not {key}.")
    return key or task_client or "eunaria"


def _allowed_refs(ctx: ToolContext, client: str) -> dict[str, str]:
    ws = ctx.app.workspace
    refs = dict(ws.clients[client].data_sources) if client in ws.clients else {}
    if client == "eunaria" or ctx.task.client in (None, "eunaria"):
        refs.update(ws.shared_data_sources)
    return refs


def _ds(ctx: ToolContext, client: str, database: str) -> str:
    refs = _allowed_refs(ctx, client)
    if database not in refs:
        raise ToolError(f"No '{database}' database mapped for {client}. Available: {', '.join(refs) or 'none'}. "
                        "If it should exist, report it as a blocker.")
    try:
        return _notion(ctx).resolve_data_source(refs[database])
    except NotionError as exc:
        raise ToolError(f"{exc}. Is the Notion integration shared with this database?") from None


def _check_page_access(ctx: ToolContext, page: dict) -> tuple[str, str]:
    """Return (client, database key) for a page if the current task may touch it."""
    parent = page.get("parent", {})
    norm = lambda s: (s or "").replace("-", "").removeprefix("ds:")  # noqa: E731
    targets = {norm(parent.get("data_source_id")), norm(parent.get("database_id"))} - {""}
    clients = [ctx.task.client] if ctx.task.client else list(ctx.app.workspace.clients)
    notion = _notion(ctx)
    for c in clients:
        for key, ref in _allowed_refs(ctx, c).items():
            try:
                if norm(ref) in targets or norm(notion.resolve_data_source(ref)) in targets:
                    return c, key
            except NotionError:
                continue
    raise ToolError("Client separation: that page is not in a database this task may access.")


def _summary(page: dict) -> dict:
    props = {}
    for name, p in page.get("properties", {}).items():
        t = p.get("type")
        v = p.get(t)
        if t in ("title", "rich_text"):
            v = plain_text(v or [])[:300]
        elif t in ("select", "status"):
            v = (v or {}).get("name")
        elif t == "multi_select":
            v = [o.get("name") for o in v or []]
        elif t == "date":
            v = (v or {}).get("start")
        elif t in ("people", "relation", "rollup", "formula", "files", "created_by", "last_edited_by"):
            continue
        props[name] = v
    return {"id": page["id"], "url": page.get("url"), "title": page_title(page), "properties": props}


@tool("notion_list_databases", "List the Notion databases mapped for a client.", {"client": CLIENT})
def notion_list_databases(ctx: ToolContext, args: dict):
    client = _client_key(ctx, args.get("client"))
    return {"client": client, "databases": sorted(_allowed_refs(ctx, client))}


@tool("notion_get_schema", "Get a database's properties, types, and allowed select/status options.",
      {"client": CLIENT, "database": DATABASE}, ["database"])
def notion_get_schema(ctx: ToolContext, args: dict):
    client = _client_key(ctx, args.get("client"))
    ds = _ds(ctx, client, args["database"])
    out = {}
    for name, p in _notion(ctx).schema(ds).items():
        t = p.get("type")
        opts = (p.get(t) or {}).get("options") if isinstance(p.get(t), dict) else None
        out[name] = {"type": t, **({"options": [o["name"] for o in opts]} if opts else {})}
    return {"client": client, "database": args["database"], "properties": out}


@tool("notion_find_pages", "Search a client database by title text (use before creating, to avoid duplicates).",
      {"client": CLIENT, "database": DATABASE, "title_contains": {"type": "string"},
       "limit": {"type": "integer", "maximum": 50}}, ["database"])
def notion_find_pages(ctx: ToolContext, args: dict):
    client = _client_key(ctx, args.get("client"))
    ds = _ds(ctx, client, args["database"])
    notion = _notion(ctx)
    limit = args.get("limit") or 20
    pages = (notion.find_by_title(ds, args["title_contains"], limit) if args.get("title_contains")
             else notion.query(ds, page_size=limit))
    return [_summary(p) for p in pages]


@tool("notion_read_page", "Read a page's properties and body text.", {"page_id": {"type": "string"}}, ["page_id"])
def notion_read_page(ctx: ToolContext, args: dict):
    notion = _notion(ctx)
    page = notion.get_page(args["page_id"])
    _check_page_access(ctx, page)
    return {**_summary(page), "body": notion.read_blocks_text(page["id"])[:15000]}


@tool(
    "notion_create_page",
    "Create a real page in a client database. Properties use the database's own names (see notion_get_schema). "
    "Anything without a matching property goes into body_markdown (e.g. a '## Details' section). "
    "Refuses exact-title duplicates unless allow_duplicate is true. Returns the verified page URL.",
    {"client": CLIENT, "database": DATABASE,
     "properties": {"type": "object", "description": "{property name: value}"},
     "body_markdown": {"type": "string", "description": "Page body (headings, bullets, tables)"},
     "allow_duplicate": {"type": "boolean"}},
    ["database", "properties"],
    side_effect=True,
)
def notion_create_page(ctx: ToolContext, args: dict):
    client = _client_key(ctx, args.get("client"))
    db_key = args["database"]
    ds = _ds(ctx, client, db_key)
    notion = _notion(ctx)
    title_prop = notion.title_property(ds)
    props = dict(args["properties"])
    title = next((str(v) for k, v in props.items() if k.strip().lower() == title_prop.lower()), "").strip()
    if not title:
        raise ToolError(f"Missing the title property '{title_prop}'.")
    if not args.get("allow_duplicate"):
        existing = [p for p in notion.find_by_title(ds, title, 10) if page_title(p).strip().lower() == title.lower()]
        if existing:
            return {"status": "duplicate_skipped", "existing": [_summary(p) for p in existing],
                    "note": "A page with this exact title exists. Update it instead, or rename."}
    try:
        page = notion.create_page(ds, props, args.get("body_markdown"))
    except NotionError as exc:
        raise ToolError(str(exc)) from None
    check = notion.get_page(page["id"])
    verified = page_title(check).strip() == title
    ctx.app.tasks.add_deliverable(ctx.task.id, kind="notion_page", target=f"{client}/{db_key}",
                                  external_id=page["id"], url=check.get("url"), title=title, verified=verified)
    return {"status": "created", "verified": verified, "id": page["id"], "url": check.get("url"), "title": title}


@tool(
    "notion_update_page",
    "Update properties on an existing page (e.g. set Caption, Caption Approval, stage). Verified by read-back.",
    {"page_id": {"type": "string"}, "properties": {"type": "object"}},
    ["page_id", "properties"],
    side_effect=True,
)
def notion_update_page(ctx: ToolContext, args: dict):
    notion = _notion(ctx)
    page = notion.get_page(args["page_id"])
    client, db_key = _check_page_access(ctx, page)
    ds = page["parent"].get("data_source_id") or _ds(ctx, client, db_key)
    try:
        notion.update_page(page["id"], ds, args["properties"])
    except NotionError as exc:
        raise ToolError(str(exc)) from None
    check = notion.get_page(page["id"])
    ctx.app.tasks.add_deliverable(ctx.task.id, kind="notion_update", target=f"{client}/{db_key}",
                                  external_id=page["id"], url=check.get("url"), title=page_title(check),
                                  verified=True, meta={"properties": list(args["properties"])})
    return {"status": "updated", "url": check.get("url"), "now": _summary(check)["properties"]}


@tool(
    "notion_append_content",
    "Append Markdown content (scripts, scene tables, notes) to the body of an existing page.",
    {"page_id": {"type": "string"}, "markdown": {"type": "string"}},
    ["page_id", "markdown"],
    side_effect=True,
)
def notion_append_content(ctx: ToolContext, args: dict):
    from ..integrations.notion import markdown_to_blocks
    notion = _notion(ctx)
    page = notion.get_page(args["page_id"])
    client, db_key = _check_page_access(ctx, page)
    blocks = markdown_to_blocks(args["markdown"])
    if not blocks:
        raise ToolError("Nothing to append.")
    notion.append_blocks(page["id"], blocks)
    ctx.app.tasks.add_deliverable(ctx.task.id, kind="notion_update", target=f"{client}/{db_key}",
                                  external_id=page["id"], url=page.get("url"), title=page_title(page),
                                  verified=True, meta={"appended_blocks": len(blocks)})
    return {"status": "appended", "blocks": len(blocks), "url": page.get("url")}
