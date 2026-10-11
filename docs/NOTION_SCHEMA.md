# Notion data model

Principle: **reuse what exists.** Each client keeps its own databases, which is also how client separation works in Notion. The system adds fields only where the brief needs them, and it never renames or deletes anything.

## Mapping of required databases

| Required (brief) | Where it lives | Status |
|---|---|---|
| Content Ideas | Each client's existing **Content Ideas** | Exists, missing fields (below) |
| Content Calendar | Each client's existing **Content Calendar** (Feed, Stories, TikTok variants) | Exists |
| Social Media Captions | Content Calendar → `Caption` + `Caption Approval` on the same record | Exists; `Hook`, `CTA`, `Hashtags`, `Revision Notes` proposed |
| Talking-Head Scripts | Content Calendar page body (current practice), stage `3. Scripting` | Exists |
| Video Sequences | Content Calendar page body: scene table appended by Luca | Exists |
| Video Performance Reports | New shared database | **To create** |
| Creative Production | New shared database (or per client) | **To create** |
| Master Task Tracker | System tracker (SQLite), mirrored to Notion | **To create** (Phase 7 mirror) |
| Team Deadlines | A view of the Master Task Tracker (deadline-sorted) | With the tracker |
| Client Workspaces | Existing per-client dashboards under CLIENTS' DASHBOARD TEMPLATE | Exists |
| Prospects (Leo) | Existing 📌 Lead Pipeline Tracker (and GHL as the CRM of record) | Exists; Leo's stages proposed |

## Content Ideas: fields to add (`notion-plan --apply`)

| Property | Type | Options |
|---|---|---|
| Platform | multi-select | Instagram, Facebook, TikTok, YouTube, LinkedIn |
| Content Pillar | select | Education, Authority, Proof, Behind the Scenes, Offer, Community |
| Funnel Stage | select | TOFU, MOFU, BOFU |
| Content Format | select | Reel, Carousel, Static, Story, Talking Head, Long-form |
| Target Audience | text | |
| Hook | text | |
| Objective | text | |
| Creative Direction | text | |
| Priority | select | High, Medium, Low |
| Idea Status | select | New, Shortlisted, Approved, In Calendar, Rejected |
| Suggested Publish Date | date | |
| Assigned AI | select | the 14 names |
| Campaign | text | (relation to a Campaigns database later, if wanted) |

Kept as-is: `Topic` (title = content title), `Description`, `Visual Inspo`, `Added to Calendar`. The client is implied by which database a record is in.

## Content Calendar: fields to add

`Hook` (text), `CTA` (text), `Hashtags` (text), `Platform` (multi-select), `Revision Notes` (text), `Assigned AI` (select). The existing `Caption Approval` serves as the copy status, and the existing stage multi-select serves as the production status.

## New databases (proposed; created only on approval)

**Video Performance Reports** (Victor): `Report` (title), `Client` (select), `Period start` / `Period end` (date), `Platform` (select), `Videos analyzed` (number), `Top performer` (url), `Key finding` (text), `Recommendations` (text), `Data gaps` (text), `Status` (Draft / Final).

**Creative Production** (Enzo): `Deliverable` (title), `Client` (select), `Content record` (relation → Content Calendar), `Script` (url), `Sequence` (url), `Aspect ratio` (select 9:16 / 1:1 / 4:5 / 16:9), `Assets needed` (text), `Edit status` (Brief / Editing / Review / Revisions / Final / Human edit required), `Final file` (url), `QA notes` (text), `Due` (date).

**Master Task Tracker** (Maya and Atlas; one-way mirror of the system tracker): `Task` (title), `Task ID` (text, e.g. T-0042), `Owner` (select: the 14 names), `Client` (select), `Project` (self-relation), `Status` (select: queued / running / waiting / awaiting approval / blocked / done / failed / cancelled), `Outcome` (select), `Priority` (select), `Deadline` (date), `Deliverables` (url / text), `Updated` (date).

## Lead Pipeline Tracker: proposed stage options

Add Leo's stages to `Status`: New Prospect, Researched, Qualified, Outreach Approved, Contacted, Follow-Up, Interested, Discovery Call Booked, Closed Won, Closed Lost, Nurture. Keep the existing options until records are migrated. Add `Source URL` (url), `Fit score` (number), `Opportunity` (text), `Do not contact` (checkbox).
