"""Interview-stage transitions on the Applied board.

The app used to stop paying attention at "submitted", which is exactly when a
search starts going well.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from jobhunt.store.db import init_db, session
from jobhunt.store.models import Application, Job, JobStatus
from jobhunt.web.app import APPLIED_STATES, NEXT_STAGE, STAGE_ORDER, app


@pytest.fixture(scope="module")
def client():
    init_db()
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def seeded():
    """A job with one submitted application, removed again afterwards."""
    with session() as s:
        job = Job(
            source="test", source_id="stage-fixture", url="https://example.com",
            company="Fixture Co", title="Software Engineer",
            dedupe_hash="stage-fixture", score=4.2, status=JobStatus.APPLIED,
        )
        s.add(job)
        s.commit()
        row = Application(job_id=job.id, method="test", status="submitted")
        s.add(row)
        s.commit()
        ids = (job.id, row.id)
    yield ids
    with session() as s:
        s.delete(s.get(Application, ids[1]))
        s.commit()
        s.delete(s.get(Job, ids[0]))


def test_every_stage_has_a_label():
    for stage in STAGE_ORDER:
        assert stage in APPLIED_STATES


def test_every_stage_can_be_closed_out():
    """A search that cannot record a rejection stops reflecting reality."""
    for stage, moves in NEXT_STAGE.items():
        if stage != "rejected":
            assert "rejected" in moves, stage


def test_the_funnel_only_moves_forward():
    """Offering a backwards move invites mis-clicks that corrupt the history."""
    for stage, moves in NEXT_STAGE.items():
        if stage not in STAGE_ORDER:
            continue
        here = STAGE_ORDER.index(stage)
        for m in moves:
            if m in STAGE_ORDER:
                assert STAGE_ORDER.index(m) > here, f"{stage} -> {m}"


def test_advancing_persists(client, seeded):
    _job_id, app_id = seeded
    r = client.post(f"/application/{app_id}/stage", data={"stage": "interview"},
                    follow_redirects=False)
    assert r.status_code == 303
    with session() as s:
        assert s.get(Application, app_id).status == "interview"


def test_an_unknown_stage_changes_nothing(client, seeded):
    _job_id, app_id = seeded
    client.post(f"/application/{app_id}/stage", data={"stage": "promoted"},
                follow_redirects=False)
    with session() as s:
        assert s.get(Application, app_id).status == "submitted"


def test_a_missing_application_does_not_500(client):
    r = client.post("/application/999999/stage", data={"stage": "offer"},
                    follow_redirects=False)
    assert r.status_code == 303
