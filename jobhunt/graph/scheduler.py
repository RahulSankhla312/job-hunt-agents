"""Daily scheduler.

APScheduler, with the times and timezone read from the targeting config. A
missed firing - laptop asleep, lid shut - is coalesced and run once on wake
rather than replayed N times; a grace window means a run that is an hour late
still happens instead of being skipped.

Two ways to host it:

* inside the dashboard (``run.py web``), switched on from the Runs page - the
  simplest setup, and what most people want;
* standalone and headless (``run.py schedule``), for a machine where the
  dashboard is not left running. ``run.py schedule --install`` prints the
  command that registers it as a Windows logon task.

Whichever hosts it, every run takes the machine-wide run lock, so enabling both
by mistake cannot produce two overlapping runs.
"""

from __future__ import annotations

import logging
import signal
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jobhunt.config import targets

log = logging.getLogger(__name__)

# A laptop that was asleep should do today's work once, not four times.
MISFIRE_GRACE_SECONDS = 3 * 60 * 60

DEFAULT_TIMEZONE = "Asia/Kolkata"


def _time_of_day(value: object, default: str) -> str:
    """A config time as "HH:MM", whatever YAML turned it into.

    YAML 1.1 reads an unquoted 18:30 as the base-60 integer 1110, so a
    hand-edited targets.yaml hands us an int. Anything unreadable falls back to
    the default rather than stopping the scheduler.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        value = f"{value // 60:02d}:{value % 60:02d}"
    text = str(value or default)
    try:
        h, m = parse_hhmm(text)
    except ValueError:
        log.warning("unreadable time %r in config; using %s", value, default)
        return default
    return f"{h:02d}:{m:02d}"


def schedule_times() -> tuple[str, str, str]:
    """(morning, evening, timezone) from the config, validated."""
    cfg = targets().get("schedule", {})
    tz = str(cfg.get("timezone") or DEFAULT_TIMEZONE)
    try:
        ZoneInfo(tz)
    except Exception:  # noqa: BLE001 - a typo must not stop the scheduler
        log.warning("unknown timezone %r in config; using %s", tz, DEFAULT_TIMEZONE)
        tz = DEFAULT_TIMEZONE
    return (
        _time_of_day(cfg.get("morning"), "09:00"),
        _time_of_day(cfg.get("evening"), "18:30"),
        tz,
    )


def parse_hhmm(value: str) -> tuple[int, int]:
    """"18:30" -> (18, 30). Raises ValueError on anything else."""
    hour, _, minute = value.strip().partition(":")
    h, m = int(hour), int(minute or 0)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"not a time of day: {value!r}")
    return h, m


def add_daily_jobs(scheduler: Any, run: Callable[[str], None]) -> None:
    """Register the morning and evening runs on ``scheduler``.

    ``run`` receives "morning" or "evening" and is responsible for doing the
    work - and for surviving a failure, since an exception here only reaches
    APScheduler's log.
    """
    from apscheduler.triggers.cron import CronTrigger

    morning_at, evening_at, tz_name = schedule_times()
    tz = ZoneInfo(tz_name)
    for label, when in (("morning", morning_at), ("evening", evening_at)):
        hour, minute = parse_hhmm(when)
        scheduler.add_job(
            run,
            CronTrigger(hour=hour, minute=minute, timezone=tz),
            args=[label],
            id=label,
            name=f"{label} run",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
            replace_existing=True,
        )
        log.info("scheduled %s run at %s %s", label, when, tz_name)


def _run_logged(which: str) -> None:
    """One scheduled run in the standalone scheduler. Never raises."""
    from jobhunt.graph.pipeline import ProfileMissing, RunInProgress, evening, morning

    try:
        report = (morning if which == "morning" else evening)()
    except (RunInProgress, ProfileMissing) as exc:
        log.warning("%s run skipped: %s", which, exc)
        return
    except Exception:  # noqa: BLE001 - the scheduler must outlive a bad run
        log.exception("%s run crashed", which)
        return
    if report.errors:
        log.warning("%s run finished with errors: %s", which, report.errors)
    else:
        log.info("%s run finished", which)


def run_forever() -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler

    morning_at, evening_at, tz_name = schedule_times()
    tz = ZoneInfo(tz_name)
    scheduler = BlockingScheduler(timezone=tz)
    add_daily_jobs(scheduler, _run_logged)

    def _stop(*_args: object) -> None:
        log.info("shutting down")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    print(
        f"Scheduler running. Morning {morning_at}, evening {evening_at} ({tz_name}).\n"
        f"Now: {datetime.now(tz):%Y-%m-%d %H:%M}. Ctrl-C to stop."
    )
    scheduler.start()


def windows_task_command(command: str = "schedule") -> str:
    """The schtasks command that starts ``python -m jobhunt <command>`` at logon.

    Uses pythonw.exe next to the running interpreter, so it works for any
    virtualenv location and opens no console window.
    """
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    python = pythonw if pythonw.exists() else exe
    return (
        'schtasks /Create /TN "HireLoop" /SC ONLOGON /RL LIMITED /F '
        f'/TR "\'{python}\' -m jobhunt {command}"'
    )
