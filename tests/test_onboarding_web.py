"""The first-run flow and the pages that replace the command line.

Covers the promise the dashboard makes to a new user: nothing works until the
profile exists, the profile and filters can be filled in and saved entirely
from the browser, and runs start from a button.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jobhunt.config import settings, targets
from jobhunt.resume.profile import Profile
from jobhunt.store.db import init_db
from jobhunt.web.app import app


@pytest.fixture(scope="module")
def client():
    init_db()
    return TestClient(app)


@pytest.fixture
def started(monkeypatch):
    """Record runs instead of launching real ones."""
    from jobhunt.graph.runner import runner

    calls: list[tuple[str, str]] = []

    def fake_start(which, trigger="manual"):
        calls.append((which, trigger))
        return True, "started"

    monkeypatch.setattr(runner, "start", fake_start)
    monkeypatch.setattr(runner, "start_or_queue", fake_start)
    return calls


PROFILE_FORM = {
    "name": "Asha Rao",
    "email": "asha@example.com",
    "phone": "+91 90000 00001",
    "location": "Pune, India",
    "citizenship": "India",
    "exp-0-company": "Acme",
    "exp-0-title": "Software Engineer",
    "exp-0-bullets": "Built things",
    "skills_languages": "Python",
}


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/applied", "/runs", "/filters", "/insights"])
def test_a_new_install_is_sent_to_setup(client, no_profile, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"


def test_setup_and_health_stay_open_without_a_profile(client, no_profile):
    assert client.get("/setup").status_code == 200
    assert client.get("/healthz").json() == {"ok": True, "setup_done": False}


def test_the_first_run_page_is_a_wizard(client, no_profile):
    body = client.get("/setup").text
    assert "let's set you up" in body
    assert "Setup progress" in body
    assert 'href="/applied"' not in body, "the rest of the app is hidden until setup"


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------


def test_saving_the_profile_starts_the_app(client, no_profile):
    r = client.post("/setup", data=PROFILE_FORM, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/filters?onboarding=1"
    saved = Profile.load(settings().profile_path)
    assert saved.identity.name == "Asha Rao"
    assert saved.experience[0].bullets == ["Built things"]
    assert client.get("/", follow_redirects=False).status_code == 200


def test_an_invalid_profile_is_shown_again_with_the_errors(client, no_profile):
    r = client.post("/setup", data={**PROFILE_FORM, "email": ""})
    assert r.status_code == 400
    assert "Email is required" in r.text
    assert 'value="Asha Rao"' in r.text, "what you typed must survive the error"
    assert not settings().profile_path.exists()


def test_editing_an_existing_profile_stays_on_the_page(client):
    before = Profile.load(settings().profile_path)
    try:
        form = {**PROFILE_FORM, "name": "Changed Name"}
        r = client.post("/setup", data=form, follow_redirects=False)
        assert r.headers["location"] == "/setup?saved=1"
        assert Profile.load(settings().profile_path).identity.name == "Changed Name"
    finally:
        before.save(settings().profile_path)


def test_the_profile_page_shows_the_saved_values(client):
    body = client.get("/setup").text
    assert Profile.load(settings().profile_path).identity.email in body


def test_a_non_pdf_resume_is_refused(client):
    r = client.post(
        "/setup/resume",
        files={"resume": ("resume.pdf", b"hello", "application/pdf")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "not%20a%20PDF" in r.headers["location"]


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------


FILTER_FORM = {
    "t_india_min_ctc_lpa": "21",
    "t_direct_titles": "software engineer",
    "t_apply_threshold": "3.6",
    "t_autonomy_present": "1",
    "t_shadow_mode": "on",
    "t_schedule_present": "1",
    "t_morning": "09:00",
    "t_evening": "18:30",
    "t_timezone": "Asia/Kolkata",
}


def test_saving_filters_applies_them(client, local_targets_file, started):
    r = client.post("/filters", data=FILTER_FORM, follow_redirects=False)
    assert r.status_code == 303
    cfg = targets()
    assert cfg["compensation"]["india_min_ctc_lpa"] == 21
    assert cfg["scoring"]["apply_threshold"] == 3.6
    assert cfg["level"]["direct_titles"] == ["software engineer"]
    assert cfg["autonomy"]["shadow_mode"] is True


def test_changed_filters_are_reapplied_to_existing_jobs(client, local_targets_file, started):
    from jobhunt.store.db import session
    from jobhunt.store.models import Job
    from tests.test_web import _purge  # reuse the fixture helper

    _purge("filters-fixture")
    with session() as s:
        s.add(Job(source="test", source_id="filters-fixture", url="https://example.test/f",
                  company="Testco", title="Software Engineer", dedupe_hash="filters-fixture"))
    try:
        client.post("/filters", data={**FILTER_FORM, "t_apply_threshold": "3.95"})
        assert ("refilter", "filters changed") in started
    finally:
        _purge("filters-fixture")


def test_onboarding_filters_continue_to_connections(client, local_targets_file, started):
    r = client.post("/filters", data={**FILTER_FORM, "onboarding": "1"}, follow_redirects=False)
    assert r.headers["location"] == "/connections?onboarding=1"


def test_invalid_filters_are_not_saved(client, local_targets_file, started):
    before = local_targets_file.read_bytes() if local_targets_file.exists() else None
    r = client.post("/filters", data={**FILTER_FORM, "t_apply_threshold": "9"})
    assert r.status_code == 400
    assert "between 1 and 5" in r.text
    after = local_targets_file.read_bytes() if local_targets_file.exists() else None
    assert before == after


# --------------------------------------------------------------------------
# Runs and connections
# --------------------------------------------------------------------------


def test_run_buttons_start_runs(client, started):
    r = client.post("/runs/start", data={"which": "morning"}, follow_redirects=False)
    assert r.status_code == 303
    assert started == [("morning", "manual")]


def test_run_status_is_json(client):
    data = client.get("/runs/status").json()
    assert set(data) >= {"running", "stage", "label", "elapsed"}


def test_the_scheduler_switch_persists(client, local_targets_file, monkeypatch):
    from jobhunt.graph.runner import runner

    monkeypatch.setattr(runner, "start_scheduler", lambda: None)
    monkeypatch.setattr(runner, "stop_scheduler", lambda: None)
    client.post("/runs/scheduler", data={"enabled": "1"})
    assert targets()["schedule"]["enabled"] is True
    client.post("/runs/scheduler", data={"enabled": "0"})
    assert targets()["schedule"]["enabled"] is False


def test_connections_page_and_status(client):
    assert client.get("/connections").status_code == 200
    assert set(client.get("/connections/status").json()) == {"ai", "linkedin", "gmail"}


def test_a_web_client_secret_is_refused(client):
    r = client.post(
        "/connections/gmail/secret",
        files={"secret": ("c.json", b'{"web": {"client_id": "x"}}', "application/json")},
        follow_redirects=False,
    )
    assert "Desktop%20app" in r.headers["location"]
    assert not settings().google_client_secret_path.exists()


def test_the_ai_engine_can_be_configured_from_the_page(client, tmp_path, monkeypatch):
    import jobhunt.config as config
    from jobhunt.web.connections import connections

    env = tmp_path / ".env"
    monkeypatch.setattr(config, "ENV_PATH", env)
    monkeypatch.setattr(connections, "check_ai", lambda: (True, "started"))
    r = client.post(
        "/connections/ai",
        data={"backend": "anthropic_api", "api_key": "sk-test-123"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    text = env.read_text(encoding="utf-8")
    assert "JOBHUNT_LLM_BACKEND=anthropic_api" in text
    assert "ANTHROPIC_API_KEY=sk-test-123" in text
    config.settings.cache_clear()


# --------------------------------------------------------------------------
# Cross-site protection
# --------------------------------------------------------------------------


def test_a_post_from_another_site_is_refused(client, started):
    r = client.post(
        "/runs/start",
        data={"which": "evening"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert started == []


def test_a_post_from_the_dashboard_itself_is_allowed(client, started):
    r = client.post(
        "/runs/start",
        data={"which": "evening"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert started == [("evening", "manual")]
