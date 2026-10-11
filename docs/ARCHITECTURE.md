# Architecture

## Design goals

1. **One command center.** The owner talks to Atlas (or any employee by name) in Telegram. Nothing else is required for daily use.
2. **Work is records, not chat.** Every request becomes a task (`T-0042`). Agents coordinate through task records, dependencies, and verified deliverables.
3. **Honest completion.** "Done" means verified: Notion pages are read back after writing, required deliverables are counted in code, and a parent project cannot complete while subtasks are open.
4. **Owner-controlled side effects.** External or irreversible actions run only after the owner approves the *exact* action in Telegram.
5. **Survives restarts.** Tasks, conversations, the job queue, approvals, and Telegram offsets live in SQLite. A crashed process resumes where it stopped.
6. **Smallest reliable stack.** One Python process, one SQLite file, official APIs. No extra automation platform is needed at this stage.

## Components

```
┌──────────────┐  long polling   ┌───────────────────────────────────────────────────────────┐
│ Telegram     │◀───────────────▶│ eunaria (single Python process)                           │
│ (owner)      │                 │                                                           │
└──────────────┘                 │  bot.py ── auth (admin allowlist) · routing · commands ·  │
                                 │             approval buttons · reply-to-continue           │
                                 │      │ creates task + job                                  │
                                 │      ▼                                                     │
                                 │  queue.py ── durable SQLite job queue (leases, retries,   │
                                 │      │        backoff, dedupe)                             │
                                 │      ▼  N worker threads                                   │
                                 │  runner.py ── per-task Claude loop (append-only history)  │
                                 │      │          ├─ agents/ (14 identities + prompts)       │
                                 │      │          └─ tools/  (34 typed tools, permissions,   │
                                 │      │                      approval gate, idempotency)    │
                                 │      ▼                                                     │
                                 │  orchestration.py ── dependencies · parent resume ·       │
                                 │                      verification · notifications         │
                                 │  approvals.py ── exact-action approvals (hash-locked)     │
                                 │  scheduler.py ── daily briefing · overdue · weekly · GHL  │
                                 │  db.py ── SQLite (WAL)                                     │
                                 └──────┬──────────────┬───────────────┬──────────────┬──────┘
                                        ▼              ▼               ▼              ▼
                                   Claude API      Notion API       GHL API v2    Meta Graph API
                                 (claude-opus-5-5)  (2025-09-03)   (LeadConnector)   (v23.0)
```

### Orchestration layer (Claude)

- **Models by cost tier:** Atlas uses `claude-opus-5-5` (effort `high`). The ten creative and specialist employees use `claude-sonnet-5-5` (effort `medium`, half the per-token price). Grace, Ava and Maya, who do reporting, use `claude-haiku-5-5` (effort `low`). All use adaptive thinking. Override with `EUNARIA_MODEL`, `EUNARIA_SPECIALIST_MODEL`, `EUNARIA_LIGHT_MODEL`. Web search is capped at `EUNARIA_WEB_SEARCH_MAX_USES` (default 3) per request.
- **Refusal fallback:** requests opt into server-side fallback (`fallbacks: "default"`) so that a safety-classifier false positive on legitimate aesthetic-clinic marketing retries on the recommended model instead of failing the task.
- **Prompt caching:** system prompts and tool lists are byte-stable per employee. Volatile context (date, task brief) goes in the first user message.
- **Append-only transcripts:** every assistant response, including thinking blocks, is stored verbatim and replayed unchanged.
- **Manual tool loop, not the SDK tool runner:** approvals and restarts need the loop's state on disk. A paused task can wait days for an approval and resume in a different process.

### Task model

| Field | Purpose |
|---|---|
| `agent_id`, `parent_id` | Who owns it; which project it belongs to |
| `client` | Client separation (Notion and GHL tools refuse other clients' data) |
| `depends_on` | Sibling tasks that must finish first; their results are delivered as context |
| `requires` | Deliverables that must be **verified** before `completed` is accepted |
| `status` | `queued → running → waiting / awaiting_approval / blocked → done / failed / cancelled` |
| `outcome` | `completed / partial / failed`, chosen by the agent and checked by code |

### Typical flow: "Atlas, plan next week's content for NULA"

1. The bot authorizes the sender, routes the message to Atlas, detects client `nula`, and creates `T-0001`. It replies with the task ID.
2. A worker runs Atlas. Atlas delegates in dependency order, for example Sofia (strategy) → Chloe (ideas, `requires: ≥N notion_page in nula/content_ideas`) → Theo (scripts) → Luca (sequences), and Clara (captions) after Chloe. Then Atlas ends its turn and `T-0001` is `waiting`.
3. Each specialist runs when its dependencies finish and receives their results. Chloe searches Notion for duplicates, creates pages, and each page is read back and recorded as a verified deliverable.
4. As each subtask finishes, the owner gets a one-line progress update. When all are finished, Atlas is resumed with every result and every unmet requirement.
5. Atlas submits. Code rejects `completed` if anything is open or unverified. The owner receives the consolidated report with every verified Notion link.

### Approvals

- Tools flagged `approval=True` (all GHL writes in this phase, and later outbound messages, publishing, and live automation changes) do **not** execute when called. They create an approval record holding the action, its exact parameters, and a SHA-256 hash of both. The owner gets Approve and Reject buttons.
- Approving runs exactly the stored action after re-checking the hash. Different parameters mean a different approval. Decisions are idempotent, so a second click does nothing.
- The decision is delivered back to the agent's task, and the task resumes.

### Reliability

- **Job queue:** leases (15 min), up to 3 attempts with exponential backoff, and dedupe keys. Startup recovery re-queues leased jobs and re-schedules `queued` and `running` tasks.
- **Tool idempotency:** each tool call is keyed by `tool_use_id`. Results are reused after a crash. A side-effecting call that was interrupted mid-flight is **not** silently re-run; the agent is told to check state first.
- **Task locks:** one worker per task at a time.
- **Telegram offset:** persisted before an update is handled, so no update is double-processed after a restart.
- **Cost guard:** a daily USD cap (default $25) blocks new agent runs and tells the owner.

### Security

- Telegram admin allowlist (`TELEGRAM_ADMIN_USER_IDS`). Messages and button clicks from anyone else are ignored and audited.
- Secrets come only from environment variables or `.env` (git-ignored). Logs pass through a redaction filter. httpx URL logging is silenced because Telegram URLs contain the bot token.
- Client separation is enforced in the Notion, GHL, and Meta tools, not just described in prompts.
- `audit_log` records task creation, approvals, decisions, executions, unauthorized access, and startup recovery.

## Why no extra platform

Long polling means no public webhook endpoint is needed, and the SQLite queue replaces a separate broker. A second service (Postgres, Redis, n8n/Make) would be worth adding only if one of these happens:

- several replicas must run at once (Postgres plus row-level job locking),
- inbound webhooks from GHL or Meta lead forms are needed (a small HTTPS endpoint, planned for Phase 4/6),
- or the owner wants a hosted dashboard (Phase 8, optional).
