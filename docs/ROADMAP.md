# Implementation plan and status

Legend: ✅ built and tested (automated tests with mocked APIs) · 🟡 built, waiting on credentials for a live test · ⬜ not started

## Phase 1: Audit ✅
Environment, Notion workspace (read-only), and integration inventory. See `AUDIT.md`.

## Phase 2: Core platform ✅ (first milestone)
- ✅ SQLite persistence: tasks, events, transcripts, jobs, approvals, deliverables, costs, audit log
- ✅ Durable job queue with leases, retries, backoff, dedupe; startup recovery
- ✅ Telegram bot: admin allowlist, named-agent routing, task IDs, progress updates, reply-to-continue, `/status /task /approvals /approve /reject /overdue /cancel /costs /health /agents /brief`
- ✅ Atlas orchestration: delegation, dependencies, parent resume, verified completion
- ✅ Approval system: exact-action, hash-locked, idempotent, Telegram buttons
- ✅ All 14 employees defined with tools, boundaries, escalation, KPIs
- ✅ Scheduler: daily briefing, overdue check, weekly accountability, daily GHL report
- ✅ Daily AI cost cap, per-employee cost tracking, log redaction
- 🟡 Live test: needs `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_USER_IDS`

## Phase 3: Content team + Notion + Meta analytics
- ✅ Notion adapter (2025-09-03 data sources), schema-aware writes, Markdown → blocks (tables for Luca), duplicate check, read-back verification, client separation
- ✅ Sofia, Chloe, Clara, Theo, Luca, Victor prompts and toolsets
- 🟡 Live Notion test: needs `NOTION_TOKEN` shared with the dashboards
- 🟡 Meta read adapter (IG media insights, page posts, ad insights with video percentiles): needs a system-user token
- ⬜ Content Ideas schema extension (`notion-plan --apply`, owner decision)
- ⬜ Video Performance Reports database plus Victor's report template
- ⬜ Per-client Meta asset mapping (`workspace.toml`)

## Phase 4: Gabriel and Grace (GHL)
- ✅ GHL read adapter (contacts, pipelines, opportunities, calendars, appointments, workflows) and approval-gated writes
- 🟡 Live test: needs a Private Integration token and Location ID
- ⬜ Inbound GHL webhook endpoint (new leads and bookings in real time) — the first reason to expose an HTTPS endpoint
- ⬜ Gabriel's workflow-spec template and test-contact verification routine
- ⬜ Bulk CRM update batching (one approval for a reviewed batch)

## Phase 5: Felix and Enzo
- ⬜ Felix: page build path (GHL funnels have no public build API, so either the GHL builder plus a spec, or a separately hosted page with a form posting to GHL), form-submission tests
- ⬜ Enzo: connect an editing or rendering tool, then file-existence and QA checks before claiming a render. Until then, editor-ready packages plus a "human editing required" status.

## Phase 6: Leo, full pipeline
- ⬜ Prospect research with source URLs, DNC/existing-client exclusion, scoring
- ⬜ Outreach drafts → approval of exact message and recipients → send via GHL conversations → track replies, bounces, opt-outs
- ⬜ Booking verification via GHL calendar events, then a discovery-call brief to Telegram

## Phase 7: Ava, Maya, executive reporting
- ✅ Ava/Maya toolsets (snapshot, overdue, approvals, costs) and scheduled reports
- ⬜ One-way mirror of the task tracker to a Notion Master Task Tracker
- ⬜ Calendar/meeting source for Ava (Google Calendar or GHL calendar)

## Phase 8: Hardening
- ✅ Tested: approval gates, tampering, duplicate prevention, permissions, client separation, restart recovery, scheduler idempotency
- ⬜ Live soak test across all integrations; optional dashboard; backups of `data/eunaria.db`

## Acceptance criteria (brief §8): current state

| # | Criterion | State |
|---|---|---|
| 1 | Message Atlas in Telegram, reliable responses | 🟡 built and tested with a mocked API; needs bot token and API key |
| 2 | Address any of the 14 by name | ✅ routing tested |
| 3 | Atlas assigns and coordinates across agents | ✅ tested end to end (mocked model) |
| 4 | Chloe creates real Notion records | 🟡 adapter tested against a mocked Notion API; needs `NOTION_TOKEN` |
| 5 | Clara and Theo save to the correct databases | 🟡 same |
| 6 | Victor analyzes real video data | 🟡 adapter built; needs a Meta token |
| 7 | Luca produces scene-by-scene plans | 🟡 prompt + table output to Notion; live test pending |
| 8 | Gabriel builds and tests GHL automations | ⬜ needs GHL plus the builder workflow (API limitation documented) |
| 9 | Grace reports real GHL data | 🟡 adapter built; needs a GHL token |
| 10 | Felix builds a working funnel | ⬜ Phase 5 |
| 11 | Enzo delivers edits or says human editing is required | 🟡 "human editing required" path in place; no editing tool yet |
| 12 | Leo end-to-end with verified bookings | ⬜ Phase 6 |
| 13 | Ava priorities and reminders | 🟡 tools + scheduled briefing; live test pending |
| 14 | Maya tracks real deadlines | 🟡 against the system tracker; Notion mirror in Phase 7 |
| 15 | Atlas consolidated reports | ✅ verified-link consolidation tested |
| 16 | Meta and GHL within verified permissions | 🟡 `doctor` reports granted permissions; live verification pending |
| 17 | Approval controls tested | ✅ |
| 18 | State retained after restart; errors recorded | ✅ |
