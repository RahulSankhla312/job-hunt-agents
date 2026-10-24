"""The public demo: everything can be looked at, nothing can be changed."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jobhunt.config import settings
from jobhunt.store.db import init_db
from jobhunt.web.app import app


@pytest.fixture
def read_only(monkeypatch):
    monkeypatch.setenv("JOBHUNT_READ_ONLY", "1")
    settings.cache_clear()
    yield
    monkeypatch.delenv("JOBHUNT_READ_ONLY")
    settings.cache_clear()


@pytest.fixture
def client():
    init_db()
    return TestClient(app)


def test_actions_are_refused_and_nothing_runs(read_only, client, monkeypatch):
    from jobhunt.graph.runner import runner

    started: list[str] = []
    monkeypatch.setattr(runner, "start_or_queue", lambda *a, **k: started.append("run") or (True, ""))
    monkeypatch.setattr(runner, "start", lambda *a, **k: started.append("run") or (True, ""))

    r = client.post(
        "/runs/start", data={"which": "morning"},
        headers={"referer": "http://testserver/runs"}, follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/runs?read_only=1"
    assert started == []


def test_even_a_cross_site_post_just_bounces(read_only, client):
    r = client.post(
        "/filters", data={}, headers={"origin": "https://evil.example"}, follow_redirects=False
    )
    assert r.status_code == 303


def test_pages_say_it_is_a_demo(read_only, client):
    page = client.get("/runs").text
    assert "Public demo." in page
    assert "Shadow mode is on." not in page
    assert "That button is switched off" in client.get("/runs?read_only=1").text


def test_normal_mode_is_unchanged(client):
    assert "Public demo." not in client.get("/runs").text
