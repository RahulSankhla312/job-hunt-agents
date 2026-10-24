"""A spent daily cap must cost nothing.

Found in end-to-end testing: with the cap at zero, the evening run still wrote
a cover letter per ready job (an AI call each) and launched a browser before
discovering it could not apply to anything.
"""

from __future__ import annotations

import pytest
from sqlmodel import select

import jobhunt.agents.applicant as applicant
from jobhunt.store.db import init_db, session
from jobhunt.store.models import Job, JobStatus


@pytest.fixture
def ready_job():
    init_db()
    with session() as s:
        for old in s.exec(select(Job).where(Job.source_id == "cap-fixture")).all():
            s.delete(old)
    with session() as s:
        s.add(Job(source="test", source_id="cap-fixture", url="https://example.test/cap",
                  company="Capco", title="Software Engineer", dedupe_hash="cap-fixture",
                  status=JobStatus.READY, score=4.9))
    yield
    with session() as s:
        for old in s.exec(select(Job).where(Job.source_id == "cap-fixture")).all():
            s.delete(old)


def test_a_spent_cap_writes_nothing_and_opens_no_browser(ready_job, monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("must not be called when the cap is spent")

    monkeypatch.setattr(applicant, "remaining_quota", lambda _key: 0)
    monkeypatch.setattr(applicant, "prepare_cover_letter", forbidden)
    monkeypatch.setattr(applicant, "browser", forbidden)

    stats, results = applicant.run(limit=5)
    assert stats.quota_blocked == 1
    assert results == []
