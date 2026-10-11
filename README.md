# Eunaria AI Workforce

Fourteen named AI employees for **Eunaria Digital Marketing Services**, coordinated by **Atlas (Chief of Staff)** and managed entirely from Telegram. Notion is the content and operations hub, GoHighLevel is the CRM, and Meta is the social and ads data source.

```
You (Telegram) ──▶ Atlas ──▶ Sofia · Victor · Chloe · Clara · Theo · Luca      (Marketing & Content)
                         ──▶ Gabriel · Grace · Felix                            (GHL & Conversion)
                         ──▶ Enzo                                               (Creative Production)
                         ──▶ Leo                                                (Sales & Lead Gen)
                         ──▶ Ava · Maya                                         (Operations)
```

## What works now (first milestone)

- **Telegram command center.** Message Atlas or anyone by name ("Chloe, add 20 ideas for NULA to Notion"). Each request gets a task ID, runs in the background, reports progress, and returns a final report with links. Reply to any task message to continue that task.
- **Atlas orchestration.** Delegation with dependencies (strategy → ideas → scripts → sequences → captions), parent resume, and **verified** completion: a project cannot be marked complete while subtasks are open or required Notion records don't exist.
- **Persistent task management.** SQLite task tracker, durable job queue, and resume after restart.
- **Approval gates.** External actions (CRM writes now; outreach, publishing, and live automations later) wait for Approve or Reject in Telegram, and only the exact approved action runs.
- **Notion, GHL, and Meta adapters** with client separation, duplicate prevention, and read-back verification.
- **Scheduled routines.** Daily briefing (Atlas), overdue check and weekly accountability (Maya), daily GHL report (Grace).
- 25 automated tests covering these paths against mocked APIs.

Status per phase and per acceptance criterion: [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env            # fill in at least the three "first milestone" values
python -m eunaria doctor        # checks every integration against its live API
python -m eunaria chat "Atlas, what can the team do?"   # local dry run in the terminal
python -m eunaria run           # start the Telegram service
```

Or run it in a container: `docker compose up -d --build` (the task database persists in `./data`).

Credentials and the exact permissions each needs: [`docs/CREDENTIALS.md`](docs/CREDENTIALS.md).

## Telegram commands

| Command | |
|---|---|
| *any message* | goes to Atlas, or to the employee you name first |
| reply to a task message | adds input to that task (reopens it if finished) |
| `/status` | active projects with subtask progress |
| `/task T-0007` | details, subtasks, verified deliverable links |
| `/approvals`, `/approve A-3`, `/reject A-3` | pending external actions |
| `/overdue` | late deliverables |
| `/cancel T-0007` | cancel a project and its open subtasks |
| `/costs` | AI cost per employee, last 7 days |
| `/health` | integrations and queue |
| `/agents`, `/brief` | roster; run the daily briefing now |

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): components, flows, approvals, reliability, security
- [`docs/EMPLOYEES.md`](docs/EMPLOYEES.md): the 14 employees and their tools, databases, boundaries, and KPIs
- [`docs/AUDIT.md`](docs/AUDIT.md): Phase 1 audit of the environment and the Notion workspace
- [`docs/NOTION_SCHEMA.md`](docs/NOTION_SCHEMA.md): database mapping and proposed fields
- [`docs/CREDENTIALS.md`](docs/CREDENTIALS.md): what to provide and where to get it
- [`docs/ROADMAP.md`](docs/ROADMAP.md): phases and acceptance status

## Layout

```
eunaria/
  agents/        14 employee definitions + system prompts
  tools/         34 tools (core/delegation, Notion, GHL, Meta) with permissions and approval flags
  integrations/  Notion, GoHighLevel, Meta, Telegram adapters
  bot.py         Telegram handling      runner.py      per-task Claude loop
  orchestration.py  task lifecycle      approvals.py   exact-action approvals
  queue.py       durable jobs           scheduler.py   recurring routines
  db.py / tasks.py  storage             service.py     process wiring + recovery
config/workspace.toml   client → Notion database map (from the audit)
tests/                  end-to-end and safety tests (mocked APIs)
```

## Tests

```bash
pip install pytest && python -m pytest -q
```
