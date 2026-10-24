"""Runs started from the dashboard, and the scheduler it hosts.

The dashboard is the one process most people leave running, so it is where
"find jobs now", "apply now" and the daily schedule live. A run takes minutes
(sourcing, LLM scoring, a browser filling forms), so it happens on a
background thread and the page polls for progress instead of holding a request
open.

One run at a time: this module serialises runs within the process, and the
pipeline's file lock serialises them across processes.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

RUNS = {
    "morning": "Find & prepare jobs",
    "evening": "Apply & follow up",
    "refilter": "Re-apply filters",
    "refresh": "Rebuild outdated resumes",
}


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RunState:
    which: str | None = None
    trigger: str = ""
    stage: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    ok: bool | None = None
    message: str = ""

    @property
    def running(self) -> bool:
        return self.started_at is not None and self.finished_at is None

    @property
    def label(self) -> str:
        return RUNS.get(self.which or "", self.which or "")

    @property
    def elapsed_s(self) -> int | None:
        if self.started_at is None:
            return None
        end = self.finished_at or _now()
        return int((end - self.started_at).total_seconds())


class Runner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = RunState()
        self._last: RunState | None = None
        self._scheduler: Any = None
        # One run to start as soon as the current one ends. A filter change
        # made mid-run must still be applied, not dropped because we were busy.
        self._queued: list[tuple[str, str]] = []

    # ---- runs -----------------------------------------------------------

    @property
    def state(self) -> RunState:
        return self._state

    @property
    def last(self) -> RunState | None:
        return self._last

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, which: str, trigger: str = "manual") -> tuple[bool, str]:
        """Start a run in the background. Returns (started, message)."""
        if which not in RUNS:
            return False, f"unknown run {which!r}"
        with self._lock:
            if self.busy():
                return False, f"'{self._state.label}' is still running"
            self._state = RunState(which=which, trigger=trigger, started_at=_now())
            self._thread = threading.Thread(
                target=self._run, args=(which,), name=f"jobhunt-{which}", daemon=True
            )
            self._thread.start()
        log.info("%s run started (%s)", which, trigger)
        return True, f"'{RUNS[which]}' started"

    def start_or_queue(self, which: str, trigger: str) -> tuple[bool, str]:
        """Start now, or run straight after the current run finishes."""
        started, message = self.start(which, trigger)
        if started or which not in RUNS:
            return started, message
        with self._lock:
            if self.busy():
                if all(q[0] != which for q in self._queued):
                    self._queued.append((which, trigger))
                return True, f"'{RUNS[which]}' will run when the current run finishes"
        return self.start(which, trigger)

    def run_blocking(self, which: str, trigger: str = "scheduled") -> None:
        """Start a run and wait for it. Used by the scheduler's worker thread."""
        started, message = self.start(which, trigger)
        if not started:
            log.warning("scheduled %s run skipped: %s", which, message)
            return
        thread = self._thread
        if thread is not None:
            thread.join()

    def _on_stage(self, name: str) -> None:
        with self._lock:
            self._state = replace(self._state, stage=name)

    def _run(self, which: str) -> None:
        from jobhunt.graph.pipeline import (
            ProfileMissing,
            RunInProgress,
            evening,
            morning,
            refilter,
            refresh_resumes,
        )

        fn = {
            "morning": morning,
            "evening": evening,
            "refilter": refilter,
            "refresh": refresh_resumes,
        }[which]
        ok, message = False, ""
        try:
            report = fn(on_stage=self._on_stage)
            ok = report.ok
            message = (
                "finished"
                if report.ok
                else "finished with errors in: " + ", ".join(report.errors)
            )
        except (RunInProgress, ProfileMissing) as exc:
            message = str(exc)
        except Exception as exc:  # noqa: BLE001 - reported on the Runs page
            log.exception("%s run crashed", which)
            message = f"crashed: {type(exc).__name__}: {exc}"
        with self._lock:
            self._state = replace(
                self._state, finished_at=_now(), ok=ok, message=message[:500], stage=None
            )
            self._last = self._state
            follow = self._queued.pop(0) if self._queued else None
            if follow is not None:
                # Same thread, so nothing else can start in between.
                self._state = RunState(which=follow[0], trigger=follow[1], started_at=_now())
        log.info("%s run: %s", which, message)
        if follow is not None:
            log.info("%s run started (%s)", *follow)
            self._run(follow[0])

    # ---- scheduler ------------------------------------------------------

    def scheduler_running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    def start_scheduler(self) -> None:
        from apscheduler.schedulers.background import BackgroundScheduler

        from jobhunt.graph.scheduler import add_daily_jobs, schedule_times

        with self._lock:
            if self.scheduler_running():
                return
            _, _, tz = schedule_times()
            scheduler = BackgroundScheduler(timezone=tz)
            add_daily_jobs(scheduler, self.run_blocking)
            scheduler.start()
            self._scheduler = scheduler
        log.info("scheduler started in the dashboard process")

    def stop_scheduler(self) -> None:
        with self._lock:
            scheduler, self._scheduler = self._scheduler, None
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)
            log.info("scheduler stopped")

    def reload_scheduler(self) -> None:
        """Pick up new times or timezone after the settings change."""
        if self.scheduler_running():
            self.stop_scheduler()
            self.start_scheduler()

    def next_runs(self) -> list[tuple[str, datetime]]:
        if not self.scheduler_running():
            return []
        out = []
        for job in self._scheduler.get_jobs():
            when = getattr(job, "next_run_time", None)
            if when is not None:
                out.append((RUNS.get(job.id, job.id), when))
        return sorted(out, key=lambda x: x[1])


runner = Runner()
