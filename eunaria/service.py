"""Process wiring: job handlers, worker threads, scheduler, Telegram polling, restart recovery."""

from __future__ import annotations

import logging
import os
import re
import signal
import threading
import time

from .app import App
from .approvals import execute_approved
from .bot import COMMANDS, Bot
from .integrations.telegram import TelegramClient
from .orchestration import fail_task
from .queue import Worker
from .runner import run_task
from .scheduler import run_routine, tick

log = logging.getLogger("eunaria")


class RedactingFilter(logging.Filter):
    """Strip configured secrets (and anything token-shaped in Telegram URLs) from log records."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s]
        self.bot_url = re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in self.secrets:
            msg = msg.replace(s, "[REDACTED]")
        msg = self.bot_url.sub("bot[REDACTED]", msg)
        record.msg, record.args = msg, ()
        return True


def setup_logging(app: App, level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter(app.settings.secret_values()))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)   # httpx logs full URLs (Telegram token)


def job_handlers(app: App, worker_id: str) -> dict:
    return {
        "run_task": lambda p: run_task(app, p["task_id"], worker_id),
        "execute_approval": lambda p: execute_approved(app, p["approval_id"]),
        "routine": lambda p: run_routine(app, p),
    }


def on_final_failure(app: App):
    def hook(job: dict, error: str) -> None:
        tid = job["payload"].get("task_id")
        if job["kind"] == "run_task" and tid:
            fail_task(app, tid, f"Stopped after {job['attempts']} attempts: {error}")
        elif app.owner_chat_id:
            app.messenger.send(app.owner_chat_id, f"⚠️ Background job {job['kind']} failed: {error[:500]}")
    return hook


def make_worker(app: App, worker_id: str) -> Worker:
    return Worker(app.queue, job_handlers(app, worker_id), on_final_failure(app))


def recover_on_startup(app: App) -> dict:
    """Single-instance deployment: anything leased/locked before start belongs to a dead process."""
    with app.db.tx() as conn:
        jobs = conn.execute("UPDATE jobs SET status='pending', lease_until=NULL WHERE status='leased'").rowcount
        conn.execute("DELETE FROM task_locks")
    from .orchestration import enqueue_run
    resumed = 0
    for t in app.tasks.list(statuses=("running", "queued"), limit=500):
        if not t.depends_on or all(app.tasks.get(d).is_terminal for d in t.depends_on):
            enqueue_run(app, t.id)
            resumed += 1
    for a in app.db.query("SELECT id FROM approvals WHERE status='approved'"):
        app.queue.enqueue("execute_approval", {"approval_id": a["id"]}, dedupe_key=f"exec-approval:{a['id']}")
    info = {"jobs_requeued": jobs, "tasks_rescheduled": resumed}
    app.db.audit("system", "startup_recovery", info)
    return info


def startup_checks(app: App) -> list[tuple[str, bool, str]]:
    """Live checks reported to the owner on every start, so problems show up in Telegram."""
    from .llm import ClaudeLLM, describe_cause
    checks = [("Telegram", True, "connected")]
    if isinstance(app.llm, ClaudeLLM):
        try:
            app.llm.create(system="Health check.", messages=[{"role": "user", "content": "Reply with OK."}],
                           tools=[], effort="low", agent_id="atlas")
            profile = getattr(app.llm, "_profile", "full")
            checks.append(("Claude", True, app.settings.model + ("" if profile == "full" else f" (mode: {profile})")))
        except Exception as exc:  # noqa: BLE001
            checks.append(("Claude", False, f"{type(exc).__name__}: {describe_cause(exc)}"))
    else:
        checks.append(("Claude", False, "ANTHROPIC_API_KEY is not set"))
    if app.notion:
        try:
            app.notion._req("GET", "/users/me")
            checks.append(("Notion", True, "token valid"))
        except Exception as exc:  # noqa: BLE001
            checks.append(("Notion", False, str(exc)[:300]))
    else:
        checks.append(("Notion", False, "not connected yet (NOTION_TOKEN)"))
    checks.append(("GoHighLevel", app.ghl is not None, "configured" if app.ghl else "not connected yet"))
    checks.append(("Meta", app.meta is not None, "configured" if app.meta else "not connected yet"))
    return checks


def announce_startup(app: App) -> None:
    from .integrations.telegram import esc
    checks = startup_checks(app)
    for name, ok, detail in checks:
        log.info("Startup check %s: %s — %s", name, "OK" if ok else "FAIL", detail)
    if app.owner_chat_id:
        lines = ["🟢 <b>Eunaria AI team is online</b>"]
        lines += [f"{'✅' if ok else '⚠️'} {esc(name)}: {esc(detail)}" for name, ok, detail in checks]
        app.messenger.send(app.owner_chat_id, "\n".join(lines))


def run_service(app: App) -> None:
    setup_logging(app, os.environ.get("EUNARIA_LOG_LEVEL", "INFO"))
    s = app.settings
    if not isinstance(app.messenger, TelegramClient):
        raise SystemExit("TELEGRAM_BOT_TOKEN is required to run the service (use `python -m eunaria chat` locally).")
    if not s.telegram_admin_ids:
        raise SystemExit("TELEGRAM_ADMIN_USER_IDS is required — without it nobody is authorized.")
    me = app.messenger.get_me()
    app.messenger.set_commands(COMMANDS)
    log.info("Telegram bot @%s ready; admins=%d", me.get("username"), len(s.telegram_admin_ids))
    log.info("Integrations: %s", app.integration_status())
    log.info("Startup recovery: %s", recover_on_startup(app))
    announce_startup(app)

    stop = threading.Event()
    workers = [make_worker(app, f"w{i}-{os.getpid()}") for i in range(s.worker_threads)]
    threads = [threading.Thread(target=w.loop, name=f"worker-{i}", daemon=True) for i, w in enumerate(workers)]

    def scheduler_loop():
        while not stop.is_set():
            try:
                tick(app)
                app.queue.recover()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            stop.wait(30)

    threads.append(threading.Thread(target=scheduler_loop, name="scheduler", daemon=True))
    for t in threads:
        t.start()

    def shutdown(*_):
        stop.set()
        for w in workers:
            w.stop.set()
        app.queue.wakeup.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    bot = Bot(app)
    offset = int(app.db.kv_get("telegram_offset", "0")) or None
    while not stop.is_set():
        try:
            updates = app.messenger.get_updates(offset, timeout=25)
        except Exception as exc:  # noqa: BLE001
            log.warning("Telegram polling error: %s", exc)
            stop.wait(5)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            app.db.kv_set("telegram_offset", str(offset))   # persist before handling: never double-process
            try:
                bot.handle_update(upd)
            except Exception:  # noqa: BLE001
                log.exception("Failed handling update %s", upd.get("update_id"))
    log.info("Shutting down; waiting for workers to finish their current step…")
    for t in threads:
        t.join(timeout=60)
    time.sleep(0.1)
