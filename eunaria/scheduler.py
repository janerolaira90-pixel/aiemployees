"""Recurring routines (daily briefing, overdue check, weekly accountability, daily GHL report).

Each firing is a job with a dedupe key per day, so a restart never double-fires and a slot
missed while the service was down fires once when it comes back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time
from typing import TYPE_CHECKING, Callable
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Routine:
    name: str
    agent_id: str
    at: str                      # HH:MM local time
    title: str
    instructions: str
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)   # Monday=0
    enabled: Callable[["App"], bool] = lambda app: True


def routines(app: "App") -> list[Routine]:
    s = app.settings
    return [
        Routine("daily_briefing", "atlas", s.daily_briefing_time, "Daily executive briefing",
                "Prepare my daily briefing. Use operations_snapshot. Cover: what needs my decision today "
                "(pending approvals first), overdue or blocked work, what finished in the last 24h, today's "
                "priorities, risks, and AI cost. Only report facts from the task records and integrations; "
                "if an integration is not connected, say so in one line. Keep it scannable."),
        Routine("overdue_check", "maya", s.overdue_check_time, "Daily deadline & overdue check",
                "Run the daily accountability check: list overdue tasks (owner, days late), tasks due in the "
                "next 2 days, and blocked tasks with their blocker. Escalate anything overdue twice or blocked "
                "more than 2 days. Do not change deadlines.", weekdays=(0, 1, 2, 3, 4)),
        Routine("weekly_accountability", "maya", "09:30", "Weekly accountability report",
                "Prepare the weekly accountability report for the last 7 days: delivered vs. planned, on-time "
                "rate by employee, overdue and blocked items, repeated delays, and 2-3 concrete efficiency "
                "recommendations.", weekdays=(0,)),
        Routine("daily_ghl_report", "grace", "08:30", "Daily GHL operations report",
                "Prepare today's GHL operations report: new leads (last 24h), unassigned contacts, overdue "
                "follow-ups, stale opportunities, today's and upcoming appointments, discovery calls booked, "
                "data issues, and recommended actions. Report only what the API returns.",
                weekdays=(0, 1, 2, 3, 4), enabled=lambda app: app.ghl is not None),
    ]


def due_routines(app: "App", now: datetime | None = None) -> list[tuple[Routine, str]]:
    tz = ZoneInfo(app.settings.timezone)
    now = (now or datetime.now(tz)).astimezone(tz)
    out = []
    for r in routines(app):
        hh, mm = (int(x) for x in r.at.split(":"))
        if now.weekday() in r.weekdays and now.time() >= time(hh, mm) and r.enabled(app):
            out.append((r, now.date().isoformat()))
    return out


def tick(app: "App", now: datetime | None = None) -> int:
    fired = 0
    for r, day in due_routines(app, now):
        if app.queue.enqueue("routine", {"name": r.name, "day": day}, dedupe_key=f"routine:{r.name}:{day}"):
            fired += 1
    return fired


def run_routine(app: "App", payload: dict) -> None:
    from .orchestration import enqueue_run
    r = next((x for x in routines(app) if x.name == payload["name"]), None)
    if r is None:
        return
    task = app.tasks.create(agent_id=r.agent_id, title=f"{r.title} — {payload['day']}", instructions=r.instructions,
                            chat_id=app.owner_chat_id, created_by="scheduler", deadline=payload["day"])
    enqueue_run(app, task.id)
