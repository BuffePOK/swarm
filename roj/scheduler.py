"""Daily schedules: deliver at a local time, start early enough, catch up after sleep.

project.toml:
  [[schedules]]
  skill = "news_digest"
  at = "08:00"          # delivery time, project timezone
  lead_minutes = 20     # pipeline starts at 07:40, delivery waits for 08:00
  grace_hours = 4       # laptop asleep at 08:00? still delivers until 12:00, marked late

Two ways to drive it:
  roj scheduler tick    one pass (Windows Task Scheduler / cron, every 10-15 min or at 07:40 + at logon)
  roj scheduler run     a loop for servers
Each (skill, day) runs at most once; interrupted runs resume from their checkpoint.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .config import ScheduleSpec
from .skills.catalog import ref
from .util import utcnow, utcnow_iso

if TYPE_CHECKING:
    from .project import Installation, Project

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def window(s: ScheduleSpec, tz: str, now: datetime) -> tuple[str, datetime, datetime, datetime]:
    local = now.astimezone(ZoneInfo(tz))
    h, m = map(int, s.at.split(":"))
    at = local.replace(hour=h, minute=m, second=0, microsecond=0)
    return local.date().isoformat(), at - timedelta(minutes=s.lead_minutes), at, at + timedelta(hours=s.grace_hours)


def due(project: "Project", now: datetime | None = None) -> list[tuple[ScheduleSpec, str, bool]]:
    """Schedules to start now: (schedule, local day, late?)."""
    now = now or utcnow()
    out = []
    for s in project.settings.schedules:
        day, start, at, end = window(s, project.settings.timezone, now)
        local = now.astimezone(ZoneInfo(project.settings.timezone))
        if WEEKDAYS[local.weekday()] not in s.days or not (start <= local <= end):
            continue
        if project.store.one("SELECT 1 FROM schedule_runs WHERE skill = ? AND day = ?", (s.skill, day)):
            continue
        out.append((s, day, local > at))
    return out


async def tick(inst: "Installation", now: datetime | None = None) -> list[dict[str, Any]]:
    from .notify.outbox import flush_all, poll_once

    report: list[dict[str, Any]] = []
    try:
        for r in await poll_once(inst):  # owner clicks since the last tick (approvals, ratings)
            report.append({"telegram": r})
    except Exception as e:  # noqa: BLE001 - Telegram problems must not stop the schedule
        report.append({"telegram_error": str(e)[:200]})
    for name in inst.project_names():
        p = inst.project(name)
        try:
            for r in await p.kernel.resume():
                report.append({"project": name, "resumed": r.task_id, "status": r.status.value})
            for s, day, late in due(p, now):
                tid = p.kernel.submit(ref(s.skill, "run"), {**s.inputs, "deliver_at": None if late else s.at, "scheduled": True})
                p.store.run("INSERT OR IGNORE INTO schedule_runs(skill, day, task_id, status, started) VALUES(?,?,?,?,?)",
                            (s.skill, day, tid, "running", utcnow_iso()))
                p.events.append("schedule.started", {"skill": s.skill, "day": day, "late": late, "task_id": tid}, actor="scheduler")
                res = await p.kernel.run(tid)
                p.store.run("UPDATE schedule_runs SET status = ? WHERE skill = ? AND day = ?", (res.status.value, s.skill, day))
                report.append({"project": name, "skill": s.skill, "day": day, "late": late, "task_id": tid,
                               "status": res.status.value, "error": res.error})
        finally:
            await inst.client.aclose()
            p.close()
    try:
        report.append({"outbox": await flush_all(inst)})
    except Exception as e:  # noqa: BLE001
        report.append({"outbox_error": str(e)[:200]})
    return report


async def run_forever(inst: "Installation", interval_s: float = 60.0) -> None:
    while True:
        await tick(inst)
        await asyncio.sleep(interval_s)


def windows_task_commands(roj_exe: str, home: str, start_hhmm: str) -> list[str]:
    tr = f'\\"{roj_exe}\\" --home \\"{home}\\" scheduler tick'
    return [
        f'schtasks /Create /F /SC DAILY /ST {start_hhmm} /TN "roj scheduler" /TR "{tr}"',
        f'schtasks /Create /F /SC ONLOGON /TN "roj scheduler catch-up" /TR "{tr}"',
    ]
