"""Approval dashboard.

Smoke-level, but it covers the two things that would silently break the daily
routine: a page that 500s, and a decision button that does not persist.
"""

from __future__ import annotations

import pytest
from sqlmodel import select
from starlette.testclient import TestClient

from jobhunt.store.db import init_db, session
from jobhunt.store.models import Application, Job, JobStatus, Resume
from jobhunt.web.app import app


@pytest.fixture(scope="module")
def client():
    init_db()
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch):
    """Approving runs the tailor and the browser for real.

    These tests are about routing and persistence, so the chain is stubbed;
    the agents themselves are covered by their own suites.
    """
    import jobhunt.web.app as web

    calls: list[int] = []
    monkeypatch.setattr(web, "_advance", calls.append)
    monkeypatch.setattr(web, "_apply_one", calls.append)
    return calls


def _purge(source_id: str) -> None:
    """Remove a fixture job and anything referencing it, dependents first.

    The models declare bare foreign-key columns rather than relationships, so
    SQLAlchemy does not know the ordering and will happily delete the parent
    first. Flushing between the two passes makes the order explicit.
    """
    with session() as s:
        job_ids = [
            j.id for j in s.exec(select(Job).where(Job.source_id == source_id)).all()
        ]
        if not job_ids:
            return
        for row in s.exec(select(Application).where(Application.job_id.in_(job_ids))).all():
            s.delete(row)
        for row in s.exec(select(Resume).where(Resume.job_id.in_(job_ids))).all():
            s.delete(row)
        s.flush()
        for job_id in job_ids:
            if (job := s.get(Job, job_id)) is not None:
                s.delete(job)


@pytest.fixture
def a_job():
    """Create a throwaway job, and remove it afterwards.

    Purges first as well as last: a test that errors mid-run would otherwise
    leave a row behind that collides with the (source, source_id) unique
    index on every subsequent run.
    """
    _purge("web-fixture")
    with session() as s:
        job = Job(
            source="test",
            source_id="web-fixture",
            url="https://example.test/j",
            company="Testco",
            title="Software Engineer",
            dedupe_hash="web-fixture",
            status=JobStatus.SCORED,
            score=4.2,
        )
        s.add(job)
    with session() as s:
        job = s.exec(select(Job).where(Job.source_id == "web-fixture")).one()
        job_id = job.id
    yield job_id
    _purge("web-fixture")


@pytest.mark.parametrize(
    "path",
    ["/", "/pipeline", "/pipeline?status=ready", "/pipeline?status=archived", "/archive"],
)
def test_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_unknown_status_falls_back_instead_of_erroring(client):
    assert client.get("/pipeline?status=not-a-status").status_code == 200


def test_job_detail_renders(client, a_job):
    r = client.get(f"/job/{a_job}")
    assert r.status_code == 200
    assert "Testco" in r.text


def test_missing_job_is_404(client):
    assert client.get("/job/99999999").status_code == 404


def test_missing_resume_is_404(client):
    assert client.get("/resume/99999999").status_code == 404


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("approve", JobStatus.READY),
        ("reject", JobStatus.ARCHIVED),
        ("retailor", JobStatus.SCORED),
        ("applied", JobStatus.APPLIED),
    ],
)
def test_decisions_persist(client, a_job, action, expected):
    r = client.post(
        f"/job/{a_job}/decision", data={"action": action, "note": ""}, follow_redirects=False
    )
    assert r.status_code == 303
    with session() as s:
        assert s.get(Job, a_job).status is expected


def test_approving_kicks_off_the_work(client, a_job, no_side_effects):
    """Approve must not merely change a status - that read as doing nothing."""
    client.post(f"/job/{a_job}/decision", data={"action": "approve"}, follow_redirects=False)
    assert a_job in no_side_effects


@pytest.mark.parametrize("action", ["reject", "retailor", "applied"])
def test_other_decisions_do_not_start_the_chain(client, a_job, no_side_effects, action):
    client.post(f"/job/{a_job}/decision", data={"action": action}, follow_redirects=False)
    assert no_side_effects == []


def test_decision_records_a_reason(client, a_job):
    """Every status change has to say who changed it and why."""
    client.post(
        f"/job/{a_job}/decision",
        data={"action": "reject", "note": "wrong domain"},
        follow_redirects=False,
    )
    with session() as s:
        reason = s.get(Job, a_job).status_reason
    assert reason and "wrong domain" in reason


def test_decision_on_missing_job_redirects_rather_than_500(client):
    r = client.post(
        "/job/99999999/decision", data={"action": "approve"}, follow_redirects=False
    )
    assert r.status_code == 303


def test_shadow_mode_banner_is_shown_while_enabled(client):
    """The banner is the only signal that nothing is actually being sent."""
    from jobhunt.config import targets

    if targets().get("autonomy", {}).get("shadow_mode"):
        assert "Shadow mode is on" in client.get("/").text


# --------------------------------------------------------------------------
# Links that silently do nothing
# --------------------------------------------------------------------------


PAGES = [
    "/", "/applied", "/insights", "/pipeline", "/archive",
    "/setup", "/filters", "/connections", "/runs", "/?q=engineer&min_score=3",
]


@pytest.mark.parametrize("path", PAGES)
def test_no_page_renders_an_empty_link(client, path):
    """An empty href reloads the current page instead of navigating.

    This is how "Open the form to submit" became a button that just reopened
    the Applied board: a context key the running server did not have rendered
    as an empty string, and Jinja does not complain. The failure is invisible
    in the markup and only shows up as a click that does nothing.
    """
    body = client.get(path).text
    assert 'href=""' not in body
    assert "href=''" not in body


@pytest.mark.parametrize("path", PAGES)
def test_no_page_renders_an_undefined(client, path):
    """The other shape of the same bug, when the key is printed rather than linked."""
    body = client.get(path).text
    assert "Undefined" not in body
    assert "{{" not in body


@pytest.mark.parametrize("path", PAGES)
def test_every_page_still_renders(client, path):
    assert client.get(path).status_code == 200
