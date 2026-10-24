"""Per-company application cap.

The cap is the difference between a targeted search and forty applications
landing in one recruiter's queue, so the two behaviours that matter are tested
directly: the queue must not stop at the dominant company (it skips past it),
and a job that reaches READY by another route must still be held back.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from jobhunt.agents import selection
from jobhunt.store.models import Job, JobStatus


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture
def cap(monkeypatch):
    def _set(n):
        monkeypatch.setattr(selection, "targets", lambda: {"limits": {"applications_per_company": n}})

    return _set


def _job(s, company: str, score: float, status=JobStatus.SCORED) -> Job:
    job = Job(
        source="test",
        source_id=f"{company}-{score}-{status.value}",
        url="https://example.com",
        company=company,
        title="Software Engineer",
        dedupe_hash=f"{company}{score}{status.value}",
        score=score,
        status=status,
    )
    s.add(job)
    s.commit()
    return job


def test_cap_limits_one_company(db, cap):
    cap(2)
    for i in range(5):
        _job(db, "Amazon", 4.5 - i * 0.01)

    picked = selection.pick_for_tailoring(db, threshold=3.7, limit=10)
    assert len(picked) == 2


def test_cap_does_not_starve_the_rest(db, cap):
    """The whole point: a wall of one company must not block what is beneath."""
    cap(2)
    for i in range(6):
        _job(db, "Amazon", 4.6 - i * 0.01)
    gitlab = _job(db, "GitLab", 4.0)
    stripe = _job(db, "Stripe", 3.9)

    picked = selection.pick_for_tailoring(db, threshold=3.7, limit=4)
    assert gitlab.id in picked
    assert stripe.id in picked


def test_already_committed_jobs_spend_the_budget(db, cap):
    cap(2)
    _job(db, "Amazon", 4.5, status=JobStatus.APPLIED)
    _job(db, "Amazon", 4.4, status=JobStatus.READY)
    fresh = _job(db, "Amazon", 4.3)

    assert selection.pick_for_tailoring(db, threshold=3.7, limit=5) == []
    assert fresh.id is not None


def test_rejected_jobs_free_their_slot(db, cap):
    """A cap on *live* applications, not on how many you may ever send."""
    cap(1)
    _job(db, "Amazon", 4.5, status=JobStatus.ARCHIVED)
    fresh = _job(db, "Amazon", 4.3)

    assert selection.pick_for_tailoring(db, threshold=3.7, limit=5) == [fresh.id]


def test_zero_means_unlimited(db, cap):
    cap(0)
    for i in range(5):
        _job(db, "Amazon", 4.5 - i * 0.01)

    assert len(selection.pick_for_tailoring(db, threshold=3.7, limit=10)) == 5


def test_threshold_still_applies(db, cap):
    cap(4)
    keep = _job(db, "GitLab", 3.8)
    _job(db, "GitLab", 3.2)

    assert selection.pick_for_tailoring(db, threshold=3.7, limit=5) == [keep.id]


def test_over_cap_holds_the_weakest_ready_job(db, cap):
    cap(2)
    best = _job(db, "Amazon", 4.5, status=JobStatus.READY)
    second = _job(db, "Amazon", 4.4, status=JobStatus.READY)
    third = _job(db, "Amazon", 4.3, status=JobStatus.READY)

    assert selection.over_company_cap(db, best) is False
    assert selection.over_company_cap(db, second) is False
    assert selection.over_company_cap(db, third) is True


def test_over_cap_ignores_other_companies(db, cap):
    cap(1)
    _job(db, "Amazon", 4.5, status=JobStatus.READY)
    gitlab = _job(db, "GitLab", 3.9, status=JobStatus.READY)

    assert selection.over_company_cap(db, gitlab) is False


def test_non_numeric_cap_is_treated_as_unlimited(db, cap):
    cap("four")
    for i in range(3):
        _job(db, "Amazon", 4.5 - i * 0.01)

    assert len(selection.pick_for_tailoring(db, threshold=3.7, limit=10)) == 3
