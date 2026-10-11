"""Environment checks: which integrations are configured and actually reachable."""

from __future__ import annotations

from .app import App
from .llm import ClaudeLLM
from .notion_schema import IMPLIED, gaps


def _line(ok: bool | None, label: str, detail: str = "") -> None:
    mark = {True: "OK  ", False: "FAIL", None: "SKIP"}[ok]
    print(f"[{mark}] {label}{' — ' + detail if detail else ''}")


def doctor(app: App) -> int:
    s, failures = app.settings, 0
    print("Eunaria AI workforce — environment check\n")

    if isinstance(app.llm, ClaudeLLM):
        try:
            m = app.llm.client.models.retrieve(s.model)
            _line(True, "Claude API", f"model {m.id} available")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            _line(False, "Claude API", f"{type(exc).__name__}: {exc}")
    else:
        failures += 1
        _line(False, "Claude API", "ANTHROPIC_API_KEY not set")

    if s.telegram_bot_token:
        from .integrations.telegram import TelegramClient
        try:
            me = TelegramClient(s.telegram_bot_token).get_me()
            _line(True, "Telegram bot", f"@{me.get('username')}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            _line(False, "Telegram bot", str(exc))
    else:
        failures += 1
        _line(False, "Telegram bot", "TELEGRAM_BOT_TOKEN not set")
    failures += 0 if s.telegram_admin_ids else 1
    _line(bool(s.telegram_admin_ids), "Telegram admins",
          f"{len(s.telegram_admin_ids)} configured" if s.telegram_admin_ids else "TELEGRAM_ADMIN_USER_IDS not set")

    if app.notion:
        for client, cfg in app.workspace.clients.items():
            for key, ref in cfg.data_sources.items():
                try:
                    ds = app.notion.resolve_data_source(ref)
                    app.notion.schema(ds)
                    _line(True, f"Notion {client}/{key}")
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    _line(False, f"Notion {client}/{key}", str(exc)[:160])
        for key, ref in app.workspace.shared_data_sources.items():
            try:
                app.notion.schema(app.notion.resolve_data_source(ref))
                _line(True, f"Notion shared/{key}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                _line(False, f"Notion shared/{key}", str(exc)[:160])
    else:
        _line(None, "Notion", "NOTION_TOKEN not set")

    if app.ghl:
        try:
            pipes = app.ghl.list_pipelines()
            _line(True, "GoHighLevel", f"{len(pipes)} pipeline(s) readable")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            _line(False, "GoHighLevel", str(exc)[:200])
    else:
        _line(None, "GoHighLevel", "GHL_PRIVATE_TOKEN / GHL_LOCATION_ID not set")

    if app.meta:
        try:
            perms = app.meta.debug_permissions()
            _line(True, "Meta Graph API", "granted: " + ", ".join(sorted(perms)))
        except Exception as exc:  # noqa: BLE001
            failures += 1
            _line(False, "Meta Graph API", str(exc)[:200])
    else:
        _line(None, "Meta Graph API", "META_ACCESS_TOKEN not set")

    print(f"\n{failures} failure(s).")
    return 1 if failures else 0


def notion_plan(app: App, apply: bool = False) -> int:
    if not app.notion:
        print("NOTION_TOKEN not set — cannot inspect schemas.")
        return 1
    for client, cfg in app.workspace.clients.items():
        for key in ("content_ideas", "content_calendar"):
            ref = cfg.data_sources.get(key)
            if not ref:
                print(f"{client}/{key}: no database mapped")
                continue
            ds = app.notion.resolve_data_source(ref)
            missing = gaps(key, app.notion.schema(ds))
            implied = "; ".join(IMPLIED.get(key, []))
            print(f"{client}/{key}: {len(missing)} missing → {', '.join(missing) or '—'}"
                  f"{'  (implied: ' + implied + ')' if implied else ''}")
            if apply and missing:
                app.notion._req("PATCH", f"/data_sources/{ds}", json={"properties": missing})
                app.notion._schema_cache.pop(ds, None)
                print("   added (additive only; nothing removed or renamed)")
    if not apply:
        print("\nDry run. Re-run with --apply to add the missing properties.")
    return 0
