# Credentials and permissions

Put every value in `.env` on the server (see `.env.example`). Never paste them into Telegram, Notion, or code.
After filling them in, run `python -m eunaria doctor`. It checks each integration against the live API and shows exactly what works.

## 1. Required for the first milestone

| Variable | Where to get it | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → Settings → API keys | Billing must be active. Model: `claude-opus-5-5`. |
| `TELEGRAM_BOT_TOKEN` | Telegram → @BotFather → `/newbot` | Then `/setprivacy` → Enable (the bot only needs direct messages). |
| `TELEGRAM_ADMIN_USER_IDS` | Message @userinfobot to see your numeric ID | Only these IDs can command the team or approve actions. |

## 2. Notion (Phase 3)

1. Go to notion.so/profile/integrations → **New integration** → type *Internal*, workspace "Justine Janerol's Notion".
2. Capabilities: **Read content**, **Update content**, **Insert content**. Comments are optional. User info is **not** needed.
3. Copy the secret into `NOTION_TOKEN`.
4. Share the integration with each parent page. Access is inherited by everything below it:
   - `CLIENTS' DASHBOARD TEMPLATE` (all client dashboards)
   - `📌 Lead Pipeline Tracker`
   - Do **not** share Login Vault pages. If they sit under a shared parent, move them out or keep the integration off them. The agents never need credentials.
5. Run `python -m eunaria doctor`. Every mapped database should show `OK`.
6. Optional: `python -m eunaria notion-plan` shows the missing Content Ideas fields. `--apply` adds them.

## 3. GoHighLevel (Phase 4)

1. In the Eunaria **sub-account**: Settings → **Private Integrations** → Create.
2. Scopes (least privilege for Phases 4–6):
   - `contacts.readonly`, `contacts.write`
   - `opportunities.readonly`, `opportunities.write`
   - `calendars.readonly`, `calendars/events.readonly`
   - `workflows.readonly`
   - `locations.readonly`
   - `conversations.readonly` (Phase 6: reply classification)
   - `conversations/message.write` (Phase 6: approved outreach only)
3. `GHL_PRIVATE_TOKEN` = the token. `GHL_LOCATION_ID` = Settings → Business Profile → Location ID.
4. Create **test contacts** (e.g. tagged `ai-test`) for Gabriel's workflow tests.
5. Client sub-accounts: one token per client location will be added to `config/workspace.toml`. Until then, GHL tools only work on Eunaria's own location.

Known API limitation: the public API can list workflows and add or remove contacts from them, but it **cannot create or edit workflow logic**. Gabriel produces exact build specs for the GHL builder, then verifies the result with test contacts and API reads.

## 4. Meta (Phase 3)

1. Business Manager → Business settings → **Users → System users** → add an Admin system user.
2. **Assign assets** to it: the Facebook Pages, Instagram professional accounts, and ad accounts the team may read.
3. Create or choose a Meta app (type *Business*) owned by the same Business Manager, and add the system user to it.
4. **Generate token** for the system user with:
   - Read (Phase 3): `pages_show_list`, `pages_read_engagement`, `read_insights`, `instagram_basic`, `instagram_manage_insights`, `ads_read`, `business_management`
   - Later, only with approval gates in place: `instagram_content_publish`, `pages_manage_posts` (publishing), `leads_retrieval`, `pages_manage_metadata` (lead forms), `ads_management` (budgets)
5. Fill `META_ACCESS_TOKEN`, `META_PAGE_IDS`, `META_IG_USER_IDS`, and `META_AD_ACCOUNT_IDS` (only listed assets are readable).

Permission note: some permissions need Advanced Access through App Review when used on assets the business does not own. `doctor` prints the permissions the token actually has, and Victor reports unavailable metrics as unavailable.

## 5. Later phases

| Need | For | Status |
|---|---|---|
| Video editing tool or API (e.g. a render service, or Higgsfield, Descript, CapCut where an API exists) | Enzo: real renders | Not connected. Enzo produces editor-ready packages and says human editing is required. |
| Email sending identity (GHL conversations, or a domain with SPF/DKIM/DMARC) | Leo: approved outreach | Not connected |
| Owner's booking link or GHL calendar ID | Leo: discovery calls | Needed in Phase 6 |
| Hosting (any VPS or container host with a persistent disk) | 24/7 operation | Owner's choice |
