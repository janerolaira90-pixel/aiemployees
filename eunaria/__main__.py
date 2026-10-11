"""Command-line entry point.

  python -m eunaria doctor              check configuration and integration access
  python -m eunaria run                 start the Telegram service (workers + scheduler + bot)
  python -m eunaria chat "Atlas, ..."   run a request locally, printing what Telegram would receive
  python -m eunaria tasks               list recent tasks
  python -m eunaria approve A-3 | reject A-3
  python -m eunaria notion-plan         compare mapped Notion databases with the required fields
"""

from __future__ import annotations

import argparse
import sys

from .config import Settings

CONSOLE_CHAT = 1  # pseudo chat id for local runs (Telegram ids are never 1)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="eunaria")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    sub.add_parser("run")
    c = sub.add_parser("chat")
    c.add_argument("text", nargs="+")
    sub.add_parser("tasks")
    for name in ("approve", "reject"):
        a = sub.add_parser(name)
        a.add_argument("approval_id")
    np_ = sub.add_parser("notion-plan")
    np_.add_argument("--apply", action="store_true", help="add missing properties (additive only)")
    args = p.parse_args(argv)

    settings = Settings.from_env()
    from .app import build_app

    if args.cmd == "run":
        from .service import run_service
        run_service(build_app(settings))
        return 0

    from .integrations.telegram import ConsoleMessenger
    app = build_app(settings, messenger=ConsoleMessenger())

    if args.cmd == "doctor":
        from .doctor import doctor
        return doctor(app)
    if args.cmd == "notion-plan":
        from .doctor import notion_plan
        return notion_plan(app, apply=args.apply)
    if args.cmd == "tasks":
        for t in app.tasks.list(limit=30):
            print(f"{t.ref}  {t.agent_id:8} {t.status:18} {t.outcome or '':9} {t.title}")
        return 0
    if args.cmd in ("approve", "reject"):
        from .approvals import decide
        from .service import make_worker
        print(decide(app, int(args.approval_id.upper().removeprefix("A-")), args.cmd == "approve", "cli"))
        make_worker(app, "cli").drain()
        return 0
    if args.cmd == "chat":
        from .bot import Bot
        from .service import make_worker, recover_on_startup
        recover_on_startup(app)
        task = Bot(app).new_request(CONSOLE_CHAT, " ".join(args.text))
        n = make_worker(app, "cli").drain()
        t = app.tasks.get(task.id)
        print(f"\n[{n} job(s) processed; {t.ref} is {t.status}]")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
