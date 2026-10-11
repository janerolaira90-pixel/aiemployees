# Phase 1 audit — 2026-10-11

## Environment

| Item | Finding |
|---|---|
| Repository | Empty (no prior code). Built from scratch in Python 3.11+. |
| Claude API key | **Not provided.** The app reads `ANTHROPIC_API_KEY`; without it, tasks fail immediately with that reason. |
| Telegram bot | **Not provided.** Needs a BotFather token and the owner's numeric user ID. |
| Notion | The owner's workspace ("Justine Janerol's Notion") was inspected read-only through the Notion connector in this build session. The running service needs its **own** internal-integration token (`NOTION_TOKEN`) shared with the pages below. |
| GoHighLevel | **No credentials.** Adapter written to the documented v2 API; not yet verified live. |
| Meta | **No credentials.** Adapter written to the Graph API docs; not yet verified live. |
| Docker | No Docker daemon in the build environment, so the Dockerfile and compose file are untested. The Python package was verified with a clean virtualenv install and the full test suite. |

## Notion workspace (read-only inspection)

The workspace is already organized around a **"CLIENTS' DASHBOARD TEMPLATE"** page with one dashboard per client. The system reuses these databases; it does not create parallel ones.

| Client key | Dashboard | Databases found |
|---|---|---|
| `eunaria` | Eunaria's Marketing Dashboard | Content Ideas, Content Calendar, Growth Tracker |
| `nula` | nu.la's Dashboard | Content Ideas, Content Calendar, Stories Content Calendar, Ad Creatives, TaskBoard, Growth Tracker |
| `inoue` | Inoue's Dashboard | Content Ideas, Feed Content Calendar, Stories Content Calendar, TikTok Content Calendar, Growth Tracker |
| `locaskin` | LocaSkin's Dashboard | Content Ideas, Content Calendar, Growth Tracker |
| `escalera` | Escalera's Dashboard | Content Calendar, Stories Content Calendar, Growth Tracker |
| `asuk` | Asuk's Dashboard | Content Calendar, Stories Content Calendar, Growth Tracker |
| `noah_eve` | Noah & Eve's Dashboard | Content Calendar, Stories Content Calendar, Growth Tracker |
| `justine` | Justine's Dashboard (founder brand) | Content Ideas, Content Calendar, Growth Tracker |
| shared | — | 📌 Lead Pipeline Tracker (New → Contacted → Replied → Discovery Call → Proposal Sent → Won/Lost) |

Also present: "Eunaria's Digital Marketing / Admin Content Dashboard" (older Eunaria calendars), "Lead Generation & Cold Email System", "Team Members", weekly report meeting notes, and per-client Login Vault pages. **The agents are never given Login Vault pages.**

### Existing schemas, and how the roles map onto them

- **Content Calendar** (e.g. nu.la): `Name`, `Caption`, `Caption Approval` (Waiting For Approval / Approved / Need Revision / Revised Version), `Content Pillar`, `Video Footage` (format), `Posting Date`, `Deadline Date`, `Visual Inspo`, `Final Visual Link`, `Final Visual Approval`, `Effort For Video`, `Assign`, and an unnamed multi-select pipeline stage (`1. Content Visual Approval` … `3. Scripting` … `8. Posted`).
  - **Clara** writes captions into `Caption` and sets `Caption Approval → Waiting For Approval` on the existing record. No separate captions database is needed.
  - **Theo** writes scripts into the page body, as the team already does (existing calendar pages contain scripts).
  - **Luca** appends a scene-by-scene table to the same page.
- **Content Ideas**: only `Topic`, `Description`, `Visual Inspo`, `Added to Calendar`. **It is missing most fields the brief requires** (platform, pillar, funnel stage, format, audience, hook, objective, creative direction, priority, status, suggested date, assigned AI, campaign). Until the schema is extended, Chloe puts those fields in the page body. `python -m eunaria notion-plan --apply` adds them (additive only), and only when the owner runs it.
- **TaskBoard** (per client): `Task`, `Status`, `Assign`, `Deadline`, `Text`. This is minimal and per-client.
- **No workspace-wide master task tracker exists.** The system's SQLite task tracker is the system of record now (`/status`, `/overdue`, Maya's reports). A one-way Notion mirror ("Master Task Tracker") is planned for Phase 7, so there is one tracker with one source of truth.

### To create (only after owner approval)

- **Master Task Tracker** (mirror of the system tracker; Maya and Atlas)
- **Video Performance Reports** (Victor)
- **Creative Production** (Enzo)

Proposed schemas are in `docs/NOTION_SCHEMA.md`.

## Update — Notion fields added (2026-10-11, owner approved)

Added additively (nothing renamed or removed):

- **Content Ideas** (Eunaria, nu.la, Inoue, Justine): Platform, Content Pillar, Funnel Stage, Content Format,
  Target Audience, Hook, Objective, Creative Direction, Priority, Idea Status, Suggested Publish Date,
  Assigned AI, Campaign.
- **Content Calendar** (Eunaria, nu.la, Inoue Feed, Justine, Noah & Eve): Hook, CTA, Hashtags, Platform,
  Revision Notes, Assigned AI.

Found in Notion's trash, so unmapped in `config/workspace.toml` until restored: LocaSkin Content Ideas and
Content Calendar, Escalera Content Calendar, Asuk Content Calendar. (They also received the new calendar
fields before the trash status was noticed; harmless.)
