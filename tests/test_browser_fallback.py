"""Browser launch fallback.

Found in end-to-end testing: on some Windows machines Playwright's bundled
Chromium refuses to start ("side-by-side configuration is incorrect"), which
broke LinkedIn sign-in and "fill it & let me submit". The installed Chrome or
Edge is the fallback.
"""

from __future__ import annotations

import pytest

import jobhunt.sources.browser.session as session
from jobhunt.web.connections import _tidy


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(session, "_working_channel", None)


def test_the_bundled_browser_is_tried_first():
    calls = []

    def launch(**kw):
        calls.append(kw.get("channel"))
        return "ctx"

    assert session.launch_with_fallback(launch, headless=True) == "ctx"
    assert calls == [None]


def test_falls_back_to_installed_chrome_then_remembers_it():
    calls = []

    def launch(**kw):
        calls.append(kw.get("channel"))
        if kw.get("channel") is None:
            raise RuntimeError("spawn UNKNOWN")
        return "ctx"

    assert session.launch_with_fallback(launch) == "ctx"
    assert calls == [None, "chrome"]

    calls.clear()
    session.launch_with_fallback(launch)
    assert calls == ["chrome"], "a browser that failed once is not retried first"


def test_the_last_error_is_raised_when_nothing_starts():
    def launch(**kw):
        raise RuntimeError(f"no {kw.get('channel')}")

    with pytest.raises(RuntimeError, match="no msedge"):
        session.launch_with_fallback(launch)


def test_a_pinned_browser_is_the_only_one_tried(monkeypatch):
    from jobhunt.config import settings

    monkeypatch.setattr(settings(), "jobhunt_browser_channel", "msedge")
    calls = []
    session.launch_with_fallback(lambda **kw: calls.append(kw.get("channel")))
    assert calls == ["msedge"]


def test_status_messages_drop_playwrights_launch_log():
    raw = "Error: launch failed: spawn UNKNOWN\nCall log:\n  - <launching> chrome.exe --a --b"
    assert _tidy(raw) == "Error: launch failed: spawn UNKNOWN"
    assert _tidy(None) == ""


# --------------------------------------------------------------------------
# One browser on the saved profile at a time - but only while one is open
# --------------------------------------------------------------------------


def test_the_profile_lock_refuses_a_second_browser():
    """Found in use: sign-in "opened" nothing while a run was only sourcing."""
    held = session._profile_lock()
    assert held.acquire("test")
    try:
        assert session.browser_in_use()
        with pytest.raises(session.SessionError, match="browser is busy"):
            session.BrowserSession().start()
    finally:
        held.release()
    assert not session.browser_in_use()


def test_a_run_that_is_only_sourcing_does_not_block_sign_in():
    """The whole-run lock no longer stands in for the browser lock."""
    from jobhunt.graph.pipeline import exclusive

    with exclusive("morning"):
        assert not session.browser_in_use()


def test_sign_in_says_busy_instead_of_promising_a_window(monkeypatch):
    from starlette.testclient import TestClient

    from jobhunt.web.app import app
    from jobhunt.web.connections import connections

    started: list[bool] = []
    monkeypatch.setattr(connections, "sign_in_linkedin", lambda: started.append(True) or (True, ""))
    held = session._profile_lock()
    assert held.acquire("test")
    try:
        r = TestClient(app).post("/connections/linkedin/signin", follow_redirects=False)
    finally:
        held.release()
    assert "browser%20is%20busy" in r.headers["location"]
    assert started == []
