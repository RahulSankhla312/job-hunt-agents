"""Referral discovery and drafting.

The LinkedIn navigation itself needs a live session, so what is tested here is
everything that decides *who* gets asked and *whether a job stalls waiting* -
the parts that would quietly misbehave.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from jobhunt.agents.connector import expire_overdue, profile_anchors
from jobhunt.resume.profile import Education, Experience, Profile, Skills
from jobhunt.sources.browser.linkedin import Person, shared_context
from jobhunt.store.db import init_db, session
from jobhunt.store.models import ApprovalStatus, Contact, Job, JobStatus, Referral


def person(**kw) -> Person:
    base = dict(name="A Person", headline="Software Engineer at Acme", degree=2)
    return Person(**{**base, **kw})


# --------------------------------------------------------------------------
# Who to ask
# --------------------------------------------------------------------------


def test_closer_connections_rank_higher():
    """A first-degree contact who already knows the candidate beats a stranger."""
    first = person(degree=1)
    third = person(degree=3)
    assert first.referral_score() > third.referral_score()


def test_recruiters_outrank_engineers_at_equal_degree():
    """A recruiter owns the requisition; an engineer can only submit a referral."""
    recruiter = person(headline="Technical Recruiter at Acme")
    engineer = person(headline="Software Engineer at Acme")
    assert recruiter.referral_score() > engineer.referral_score()


def test_shared_context_raises_the_score():
    plain = person()
    connected = person(shared_context=["IIIT", "Northwind"])
    assert connected.referral_score() > plain.referral_score()


def test_a_first_degree_engineer_beats_a_third_degree_recruiter():
    """Degree has to dominate title, or the ranking sends you to strangers."""
    known = person(degree=1, headline="Software Engineer at Acme")
    stranger = person(degree=3, headline="Technical Recruiter at Acme")
    assert known.referral_score() > stranger.referral_score()


# --------------------------------------------------------------------------
# Shared context must be real
# --------------------------------------------------------------------------


def test_shared_context_only_reports_genuine_overlap():
    anchors = ["IIIT", "Northwind", "Playwright"]
    found = shared_context("Software Engineer at Northwind", "Bengaluru. Uses Playwright.", anchors)
    assert set(found) == {"Northwind", "Playwright"}


def test_shared_context_is_empty_when_nothing_matches():
    """No overlap must yield nothing, so the draft cannot imply a false link."""
    assert shared_context("Designer at Figma", "San Francisco", ["IIIT", "Northwind"]) == []


def test_profile_anchors_include_the_short_form_of_the_university():
    """Nobody writes "Indian Institute of Information Technology" in a headline."""
    profile = Profile(
        education=[Education(institution="Indian Institute of Information Technology, Pune")],
        experience=[Experience(company="Northwind", title="Software Engineer")],
        skills=Skills(languages=["Python"]),
    )
    anchors = profile_anchors(profile)
    assert "IIIT" in anchors
    assert "Northwind" in anchors
    assert "Pune" in anchors


# --------------------------------------------------------------------------
# The SLA must not let a job stall
# --------------------------------------------------------------------------


@pytest.fixture
def job_awaiting_referral():
    init_db()
    with session() as s:
        for old in s.exec(select(Job).where(Job.source_id == "conn-fixture")).all():
            for r in s.exec(select(Referral).where(Referral.job_id == old.id)).all():
                s.delete(r)
            s.delete(old)
        for c in s.exec(select(Contact).where(Contact.name == "Conn Fixture")).all():
            s.delete(c)

    with session() as s:
        job = Job(
            source="test", source_id="conn-fixture", url="u", company="Acme",
            title="Software Engineer", dedupe_hash="conn-fixture",
            status=JobStatus.AWAITING_REFERRAL, score=4.2,
        )
        contact = Contact(company="Acme", name="Conn Fixture")
        s.add(job)
        s.add(contact)
        s.flush()
        job_id, contact_id = job.id, contact.id

    yield job_id, contact_id

    with session() as s:
        for r in s.exec(select(Referral).where(Referral.job_id == job_id)).all():
            s.delete(r)
        s.flush()  # children before parents; the models declare no relationships
        if (j := s.get(Job, job_id)) is not None:
            s.delete(j)
        if (c := s.get(Contact, contact_id)) is not None:
            s.delete(c)


def _add_referral(job_id: int, contact_id: int, *, hours: float) -> int:
    with session() as s:
        ref = Referral(
            job_id=job_id,
            contact_id=contact_id,
            message="hello",
            status=ApprovalStatus.QUEUED,
            sla_deadline=datetime.now(UTC) + timedelta(hours=hours),
        )
        s.add(ref)
        s.flush()
        return ref.id


def test_overdue_referral_releases_the_job(job_awaiting_referral):
    """Silence by the deadline must not hold the application hostage."""
    job_id, contact_id = job_awaiting_referral
    ref_id = _add_referral(job_id, contact_id, hours=-1)

    assert expire_overdue() >= 1
    with session() as s:
        assert s.get(Referral, ref_id).status is ApprovalStatus.EXPIRED
        assert s.get(Job, job_id).status is JobStatus.READY


def test_referral_within_sla_is_left_alone(job_awaiting_referral):
    job_id, contact_id = job_awaiting_referral
    ref_id = _add_referral(job_id, contact_id, hours=+6)

    expire_overdue()
    with session() as s:
        assert s.get(Referral, ref_id).status is ApprovalStatus.QUEUED
        assert s.get(Job, job_id).status is JobStatus.AWAITING_REFERRAL


def test_a_replied_referral_is_never_expired(job_awaiting_referral):
    """A reply after the deadline still counts - do not discard the referral."""
    job_id, contact_id = job_awaiting_referral
    ref_id = _add_referral(job_id, contact_id, hours=-5)
    with session() as s:
        s.get(Referral, ref_id).replied_at = datetime.now(UTC)

    expire_overdue()
    with session() as s:
        assert s.get(Referral, ref_id).status is not ApprovalStatus.EXPIRED
