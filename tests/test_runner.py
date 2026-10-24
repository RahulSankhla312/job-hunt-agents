"""The background runner the dashboard uses."""

from __future__ import annotations

import threading
import time

from jobhunt.graph.pipeline import RunReport
from jobhunt.graph.runner import Runner


def _fake_pipeline(monkeypatch, *, gate: threading.Event | None = None, fail: bool = False):
    import jobhunt.graph.pipeline as pipeline

    def fake(*, on_stage=None, **_kw):
        if on_stage:
            on_stage("scout")
        if gate is not None:
            gate.wait(5)
        if fail:
            raise RuntimeError("board down")
        report = RunReport(run_type="morning")
        report.stages["scout"] = {"new": 3}
        return report

    monkeypatch.setattr(pipeline, "morning", fake)


def test_a_run_finishes_and_reports(monkeypatch):
    _fake_pipeline(monkeypatch)
    r = Runner()
    started, _ = r.start("morning")
    assert started
    r._thread.join(5)
    assert not r.busy()
    assert r.last is not None and r.last.ok is True
    assert r.last.message == "finished"


def test_only_one_run_at_a_time(monkeypatch):
    gate = threading.Event()
    _fake_pipeline(monkeypatch, gate=gate)
    r = Runner()
    try:
        assert r.start("morning")[0]
        again, why = r.start("morning")
        assert not again
        assert "still running" in why
        # The stage is reported from the run's own thread; give it a moment.
        deadline = time.monotonic() + 5
        while r.state.stage != "scout" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert r.state.stage == "scout"
    finally:
        gate.set()
        r._thread.join(5)


def test_a_crashing_run_is_reported_not_raised(monkeypatch):
    _fake_pipeline(monkeypatch, fail=True)
    r = Runner()
    r.start("morning")
    r._thread.join(5)
    assert r.last.ok is False
    assert "board down" in r.last.message


def test_a_refilter_requested_mid_run_runs_afterwards(monkeypatch):
    """Changing filters during a run must still re-apply them, not drop them."""
    import jobhunt.graph.pipeline as pipeline

    gate = threading.Event()
    _fake_pipeline(monkeypatch, gate=gate)
    ran = []

    def fake_refilter(*, on_stage=None):
        ran.append("refilter")
        return RunReport(run_type="refilter")

    monkeypatch.setattr(pipeline, "refilter", fake_refilter)
    r = Runner()
    r.start("morning")
    queued, why = r.start_or_queue("refilter", "filters changed")
    assert queued and "when the current run finishes" in why
    gate.set()
    r._thread.join(5)
    assert ran == ["refilter"]
    assert r.last.which == "refilter" and r.last.ok is True


def test_run_history_shows_stage_errors():
    from jobhunt.web.runs import summarise

    lines = dict(summarise({
        "scout": {"new": 3, "raw": 10},
        "connector": {"jobs_considered": 1, "errors": ["LinkedIn is not signed in", "x"]},
        "referral_sweep": {"expired": 0},
    }))
    assert lines["scout"] == "3 new, 10 raw"
    assert "LinkedIn is not signed in (+1 more)" in lines["connector"]
    assert lines["referral_sweep"] == "nothing to do"


def test_hand_edited_schedule_times_are_understood(monkeypatch):
    """Unquoted 18:30 in YAML arrives as 1110; a typo must not stop the scheduler."""
    import yaml

    import jobhunt.graph.scheduler as scheduler

    parsed = yaml.safe_load("morning: 7:05\nevening: 18:30\ntimezone: Nowhere/Land")
    monkeypatch.setattr(scheduler, "targets", lambda: {"schedule": parsed})
    assert scheduler.schedule_times() == ("07:05", "18:30", "Asia/Kolkata")

    monkeypatch.setattr(scheduler, "targets", lambda: {"schedule": {"morning": "soon"}})
    assert scheduler.schedule_times()[0] == "09:00"


def test_unknown_runs_are_refused():
    assert Runner().start("lunch")[0] is False


def test_the_scheduler_registers_both_daily_runs():
    r = Runner()
    r.start_scheduler()
    try:
        assert r.scheduler_running()
        labels = sorted(label for label, _ in r.next_runs())
        assert labels == ["Apply & follow up", "Find & prepare jobs"]
    finally:
        r.stop_scheduler()
    assert not r.scheduler_running()


def test_two_requests_during_a_run_both_happen(monkeypatch):
    """A filter change and a resume refresh asked for mid-run must both run."""
    import jobhunt.graph.pipeline as pipeline

    gate = threading.Event()
    _fake_pipeline(monkeypatch, gate=gate)
    ran: list[str] = []

    def fake(name):
        def run(*, on_stage=None):
            ran.append(name)
            return RunReport(run_type=name)

        return run

    monkeypatch.setattr(pipeline, "refilter", fake("refilter"))
    monkeypatch.setattr(pipeline, "refresh_resumes", fake("refresh"))
    r = Runner()
    r.start("morning")
    assert r.start_or_queue("refresh", "switched")[0]
    assert r.start_or_queue("refilter", "filters changed")[0]
    assert r.start_or_queue("refilter", "again")[0], "a duplicate is accepted but not doubled"
    gate.set()
    r._thread.join(5)
    assert ran == ["refresh", "refilter"]
