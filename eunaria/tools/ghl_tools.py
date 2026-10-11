"""GoHighLevel tools. Reads run directly; every write is approval-gated in this phase."""

from __future__ import annotations

from .base import ToolContext, ToolError, tool


def _ghl(ctx: ToolContext):
    if ctx.app.ghl is None:
        raise ToolError("GoHighLevel is not connected (GHL_PRIVATE_TOKEN / GHL_LOCATION_ID missing). "
                        "Report this as a blocker; do not invent CRM data.")
    if ctx.task.client not in (None, "eunaria"):
        raise ToolError(f"Client separation: the connected GHL location is Eunaria's. No GHL access is "
                        f"configured for client {ctx.task.client}.")
    return ctx.app.ghl


def _contact_brief(c: dict) -> dict:
    keys = ("id", "firstName", "lastName", "companyName", "email", "phone", "tags", "source",
            "assignedTo", "dateAdded", "dnd")
    return {k: c.get(k) for k in keys if k in c}


@tool("ghl_search_contacts", "Search GHL contacts (name/email/phone/company text).",
      {"query": {"type": "string"}, "limit": {"type": "integer", "maximum": 100}})
def ghl_search_contacts(ctx: ToolContext, args: dict):
    return [_contact_brief(c) for c in _ghl(ctx).search_contacts(args.get("query"), args.get("limit") or 20)]


@tool("ghl_get_contact", "Get one GHL contact.", {"contact_id": {"type": "string"}}, ["contact_id"])
def ghl_get_contact(ctx: ToolContext, args: dict):
    return _ghl(ctx).get_contact(args["contact_id"])


@tool("ghl_list_pipelines", "List GHL pipelines and their stages.", {})
def ghl_list_pipelines(ctx: ToolContext, args: dict):
    return [{"id": p["id"], "name": p.get("name"),
             "stages": [{"id": s["id"], "name": s.get("name")} for s in p.get("stages", [])]}
            for p in _ghl(ctx).list_pipelines()]


@tool("ghl_search_opportunities", "List opportunities, optionally by pipeline and status (open/won/lost/abandoned).",
      {"pipeline_id": {"type": "string"}, "status": {"type": "string"}, "limit": {"type": "integer", "maximum": 100}})
def ghl_search_opportunities(ctx: ToolContext, args: dict):
    keys = ("id", "name", "status", "pipelineId", "pipelineStageId", "monetaryValue", "assignedTo",
            "createdAt", "updatedAt", "lastStatusChangeAt", "lastStageChangeAt", "source")
    rows = _ghl(ctx).search_opportunities(args.get("pipeline_id"), args.get("status"), args.get("limit") or 50)
    return [{**{k: o.get(k) for k in keys if k in o},
             "contact": _contact_brief(o.get("contact") or {})} for o in rows]


@tool("ghl_list_calendars", "List GHL calendars.", {})
def ghl_list_calendars(ctx: ToolContext, args: dict):
    return [{"id": c["id"], "name": c.get("name"), "isActive": c.get("isActive")} for c in _ghl(ctx).list_calendars()]


@tool("ghl_list_appointments", "Appointments for a calendar in a date window around today.",
      {"calendar_id": {"type": "string"}, "days_back": {"type": "integer", "maximum": 31},
       "days_ahead": {"type": "integer", "maximum": 31}}, ["calendar_id"])
def ghl_list_appointments(ctx: ToolContext, args: dict):
    return _ghl(ctx).list_appointments(args["calendar_id"], args.get("days_back") or 1, args.get("days_ahead") or 7)


@tool("ghl_list_workflows", "List GHL workflows (name, status). Read-only audit.", {})
def ghl_list_workflows(ctx: ToolContext, args: dict):
    return [{"id": w.get("id"), "name": w.get("name"), "status": w.get("status"), "updatedAt": w.get("updatedAt")}
            for w in _ghl(ctx).list_workflows()]


# --- writes: approval required ------------------------------------------------------------

def _record(ctx: ToolContext, kind: str, external_id: str, title: str, result: dict) -> dict:
    ctx.app.tasks.add_deliverable(ctx.task.id, kind=kind, target="ghl", external_id=external_id, url=None,
                                  title=title, verified=True, meta={"response_keys": list(result)[:10]})
    return result


@tool("ghl_add_contact_tags", "Add tags to a contact (owner approval required).",
      {"contact_id": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
       "reason": {"type": "string"}}, ["contact_id", "tags", "reason"],
      approval=True, side_effect=True,
      summarize=lambda a: f"Add tags {a['tags']} to contact {a['contact_id']} — {a['reason']}")
def ghl_add_contact_tags(ctx: ToolContext, args: dict):
    ghl = _ghl(ctx)
    result = ghl.add_tags(args["contact_id"], args["tags"])
    tags = set(ghl.get_contact(args["contact_id"]).get("tags") or [])
    missing = [t for t in args["tags"] if t.lower() not in {x.lower() for x in tags}]
    if missing:
        raise ToolError(f"Tags not present after update: {missing}")
    return _record(ctx, "ghl_tags", args["contact_id"], f"tags {args['tags']}", result)


@tool("ghl_add_contact_note", "Add a note to a contact (owner approval required).",
      {"contact_id": {"type": "string"}, "body": {"type": "string"}}, ["contact_id", "body"],
      approval=True, side_effect=True,
      summarize=lambda a: f"Add note to contact {a['contact_id']}: {a['body'][:300]}")
def ghl_add_contact_note(ctx: ToolContext, args: dict):
    result = _ghl(ctx).add_note(args["contact_id"], args["body"])
    return _record(ctx, "ghl_note", args["contact_id"], "note", result)


@tool("ghl_create_contact_task", "Create a follow-up task on a contact (owner approval required).",
      {"contact_id": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"},
       "due_date": {"type": "string", "description": "ISO 8601 datetime"}},
      ["contact_id", "title", "body", "due_date"], approval=True, side_effect=True,
      summarize=lambda a: f"Create task '{a['title']}' due {a['due_date']} on contact {a['contact_id']}")
def ghl_create_contact_task(ctx: ToolContext, args: dict):
    result = _ghl(ctx).create_task(args["contact_id"], args["title"], args["body"], args["due_date"])
    return _record(ctx, "ghl_task", args["contact_id"], args["title"], result)


@tool("ghl_update_opportunity_stage", "Move an opportunity to another pipeline stage (owner approval required).",
      {"opportunity_id": {"type": "string"}, "stage_id": {"type": "string"}, "reason": {"type": "string"}},
      ["opportunity_id", "stage_id", "reason"], approval=True, side_effect=True,
      summarize=lambda a: f"Move opportunity {a['opportunity_id']} to stage {a['stage_id']} — {a['reason']}")
def ghl_update_opportunity_stage(ctx: ToolContext, args: dict):
    result = _ghl(ctx).update_opportunity_stage(args["opportunity_id"], args["stage_id"])
    return _record(ctx, "ghl_stage", args["opportunity_id"], args["stage_id"], result)


@tool("ghl_create_contact", "Create a prospect contact from verified public sources (owner approval required).",
      {"first_name": {"type": "string"}, "last_name": {"type": "string"}, "company_name": {"type": "string"},
       "email": {"type": "string"}, "phone": {"type": "string"}, "website": {"type": "string"},
       "source_url": {"type": "string", "description": "Where the details were found"},
       "tags": {"type": "array", "items": {"type": "string"}}},
      ["company_name", "source_url"], approval=True, side_effect=True,
      summarize=lambda a: f"Create contact {a.get('company_name')} ({a.get('email') or 'no email'}) "
                          f"from {a['source_url']}")
def ghl_create_contact(ctx: ToolContext, args: dict):
    ghl = _ghl(ctx)
    if args.get("email") and ghl.search_contacts(args["email"], 5):
        raise ToolError(f"A contact with {args['email']} already exists — not creating a duplicate.")
    fields = {"firstName": args.get("first_name"), "lastName": args.get("last_name"),
              "companyName": args["company_name"], "email": args.get("email"), "phone": args.get("phone"),
              "website": args.get("website"), "source": f"Leo research: {args['source_url']}",
              "tags": args.get("tags") or ["prospect"]}
    result = ghl.create_contact({k: v for k, v in fields.items() if v})
    cid = (result.get("contact") or {}).get("id", "")
    return _record(ctx, "ghl_contact", cid, args["company_name"], result)
