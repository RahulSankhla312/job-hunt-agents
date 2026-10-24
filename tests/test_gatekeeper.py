"""Gatekeeper - the deterministic filters.

These decide what the candidate ever sees. A wrong reject is invisible, so the
"must not reject" cases below matter most; each was a real misclassification
found by running the filter over 8,676 live postings.
"""

from __future__ import annotations

import pytest

from jobhunt.agents.gatekeeper import (
    check_compensation,
    classify_eligibility,
    classify_level,
    parse_comp_text,
    years_required,
)
from jobhunt.store.models import Eligibility, LevelBand

LAKH = 100_000


# --------------------------------------------------------------------------
# Level banding
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "description", "expected"),
    [
        ("Software Engineer", "2+ years of experience", LevelBand.DIRECT),
        ("SDE 1", "", LevelBand.DIRECT),
        ("Associate Software Engineer", "", LevelBand.DIRECT),
        # Regression: substring matching banded this as DIRECT because
        # "software engineer" matched before the more specific pattern.
        ("Software Engineer II", "3 years", LevelBand.STRETCH),
        ("Senior Software Engineer", "3 years", LevelBand.STRETCH),
        # Regression: rejected for containing "staff". This is the standard
        # AI-lab IC title and exactly the kind of role wanted here.
        ("Member of Technical Staff", "", LevelBand.AI_TRACK),
        ("Senior Member of Technical Staff", "", LevelBand.AI_TRACK),
        ("Forward Deployed Engineer", "", LevelBand.AI_TRACK),
        ("AI Engineer", "1-3 years", LevelBand.AI_TRACK),
        ("Applied AI Engineer", "", LevelBand.AI_TRACK),
    ],
)
def test_accepted_bands(title, description, expected):
    band, reason = classify_level(title, description)
    assert band is expected, f"{title!r} -> {band.value} ({reason})"


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("Staff Software Engineer", ""),
        ("Principal Engineer", ""),
        ("Engineering Manager", ""),
        ("Director of Engineering", ""),
        ("Software Engineering Intern", ""),
        ("Account Executive", ""),
        # Regression: the allowlist overrode the reject list by string length,
        # so "applied ai" (10 chars) beat "manager" (7) and these leaked in as
        # individual-contributor AI roles.
        ("Manager, Applied AI Engineering", ""),
        ("Applied AI Architect", ""),
        ("Staff Machine Learning Engineer", ""),
        ("Director of AI Engineering", ""),
        # Experience bar well past 1.3 years.
        ("Software Engineer", "We require 8+ years of experience."),
        ("Software Engineer", "Minimum of 10 years of industry experience"),
        # PhD genuinely required.
        ("Applied Scientist", "PhD in Machine Learning is required."),
    ],
)
def test_rejected_titles(title, description):
    band, _ = classify_level(title, description)
    assert band is LevelBand.REJECT, f"{title!r} should have been rejected"


def test_phd_listed_as_optional_is_not_a_reject():
    band, _ = classify_level("AI Engineer", "PhD or equivalent practical experience.")
    assert band is not LevelBand.REJECT


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3+ years of experience", 3),
        ("2-5 years of experience", 2),
        ("minimum of 4 years", 4),
        ("at least 6 years", 6),
        # The lowest credible bar is the one that actually gates an
        # application; JDs also mention higher figures for nice-to-haves.
        ("2+ years required. 8+ years with distributed systems a plus.", 2),
        ("no experience mentioned", None),
    ],
)
def test_years_required(text, expected):
    assert years_required(text) == expected


# --------------------------------------------------------------------------
# Work authorisation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "description", "company", "expected"),
    [
        ("Bengaluru, India", "", "", Eligibility.INDIA_ELIGIBLE),
        ("Remote - Worldwide", "", "", Eligibility.INDIA_ELIGIBLE),
        ("Remote - India", "", "", Eligibility.INDIA_ELIGIBLE),
        ("New York", "We sponsor H-1B visas.", "", Eligibility.SPONSORSHIP_OFFERED),
        (
            "San Francisco",
            "Must be authorized to work in the United States without sponsorship.",
            "OpenAI",
            Eligibility.US_ONLY_NO_SPONSORSHIP,
        ),
        ("Seattle", "No visa sponsorship is available.", "", Eligibility.US_ONLY_NO_SPONSORSHIP),
        # Silent US posting goes to the Analyst rather than being dropped.
        ("Austin, TX", "Join our team.", "Unknown Co", Eligibility.UNKNOWN),
        # Regression: the US H-1B sponsor list was applied to every location,
        # marking a London role as sponsoring.
        ("London, UK", "", "OpenAI", Eligibility.UNKNOWN),
    ],
)
def test_eligibility(location, description, company, expected):
    result, reason = classify_eligibility(location, description, company)
    assert result is expected, f"{location!r} -> {result.value} ({reason})"


def test_explicit_refusal_beats_a_known_sponsor():
    """A company that usually sponsors can still post a US-only role."""
    result, _ = classify_eligibility(
        "San Francisco", "We are unable to sponsor visas for this role.", "OpenAI"
    )
    assert result is Eligibility.US_ONLY_NO_SPONSORSHIP


# --------------------------------------------------------------------------
# Compensation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "low", "high", "currency"),
    [
        ("Compensation: 55 LPA", 55 * LAKH, None, "INR"),
        ("CTC 45-60 LPA", 45 * LAKH, 60 * LAKH, "INR"),
        ("$120,000 - $180,000", 120_000, 180_000, "USD"),
        ("$120k - $180k", 120_000, 180_000, "USD"),
        ("no numbers here", None, None, None),
    ],
)
def test_parse_comp_text(text, low, high, currency):
    assert parse_comp_text(text) == (low, high, currency)


def test_published_comp_below_floor_is_dropped():
    ok, estimated, _ = check_compensation("Acme", "Remote", 80_000, 95_000, "USD")
    assert not ok and not estimated


def test_published_comp_above_floor_passes():
    ok, estimated, _ = check_compensation("OpenAI", "San Francisco", 295_000, 500_000, "USD")
    assert ok and not estimated


def test_inr_floor_is_compared_against_inr_not_usd():
    """45 LPA is ~4.5M INR; comparing it to the $120k floor would pass anything."""
    ok, _, _ = check_compensation(
        "Acme", "Bangalore", 18 * LAKH, None, "INR"
    )
    assert not ok


def test_tier_one_with_no_published_comp_clears_on_estimate():
    ok, estimated, reason = check_compensation("Rubrik", "Bengaluru, India", None, None, None)
    assert ok and estimated and "tier 1" in reason


def test_uncomparable_currency_defers_rather_than_guessing():
    """Without an FX feed, EUR cannot be judged - let the Analyst decide."""
    ok, estimated, reason = check_compensation("Acme", "Berlin", 90_000, 110_000, "EUR")
    assert ok and estimated and "not comparable" in reason


def test_unknown_company_defers():
    ok, estimated, _ = check_compensation("Nobody Ltd", "Remote", None, None, None)
    assert ok and estimated


@pytest.mark.parametrize(
    "title",
    [
        # Regression: matched the generic "software development engineer" and
        # banded as a direct fit. At Amazon this title is L6.
        "Senior Software Development Engineer",
        "Sr. Software Development Engineer, Aurora",
        "Senior Backend Engineer",
        "Lead Software Engineer",
        "Software Engineer III",
    ],
)
def test_seniority_prefix_demotes_a_direct_match(title):
    band, reason = classify_level(title, "")
    assert band is not LevelBand.DIRECT, f"{title!r} banded as {band.value} ({reason})"


def test_plain_title_is_still_a_direct_match():
    """The demotion must not swallow the ordinary case."""
    assert classify_level("Software Development Engineer", "")[0] is LevelBand.DIRECT


# --------------------------------------------------------------------------
# Numeric ladder rungs
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "rung"),
    [
        ("Software Engineer 3", 3),
        ("Software Engineer III", 3),
        ("SDE 3", 3),
        ("SDE-2", 2),
        ("Engineer II", 2),
        ("L5 Engineer", 5),
        ("IC3 Software Engineer", 3),
        ("Level 4 Engineer", 4),
        ("Software Engineer 1", 1),
        # No rung named at all.
        ("Software Engineer", None),
        ("Software Engineer, Codex", None),
        ("Member of Technical Staff", None),
    ],
)
def test_title_level_number(title, rung):
    from jobhunt.agents.gatekeeper import title_level_number

    assert title_level_number(title) == rung


@pytest.mark.parametrize(
    "title",
    [
        # Regression: the config listed "software engineer ii" as a literal
        # string, so only the Roman spelling was recognised. The Arabic forms
        # fell through to the generic "software engineer" pattern and banded
        # as a DIRECT match for someone with 1.3 years.
        "Software Engineer 3",
        "Software Engineer 4",
        "SDE 3",
        "IC3 Software Engineer",
        "Level 4 Engineer",
        "Software Engineer III",
    ],
)
def test_level_three_and_above_is_rejected(title):
    band, reason = classify_level(title, "")
    assert band is LevelBand.REJECT, f"{title!r} -> {band.value} ({reason})"


@pytest.mark.parametrize("title", ["Software Engineer 2", "Software Engineer II", "SDE-2"])
def test_level_two_is_a_stretch_not_a_direct_match(title):
    assert classify_level(title, "")[0] is LevelBand.STRETCH


@pytest.mark.parametrize("title", ["Software Engineer 1", "SDE 1", "Software Engineer"])
def test_entry_level_still_reads_as_direct(title):
    assert classify_level(title, "")[0] is LevelBand.DIRECT


# --------------------------------------------------------------------------
# Auditing work already past the gate
# --------------------------------------------------------------------------
#
# reset() deliberately spares scored and tailored jobs, so a level rule
# tightened afterwards never reaches them. That is exactly how a "Software
# Engineer 3" sat in the approval queue looking approved after max_level_number
# was set to 2.


@pytest.fixture
def audit_db(monkeypatch, tmp_path):
    """Point the whole store at a throwaway database for this test."""
    from sqlmodel import Session, SQLModel, create_engine

    import jobhunt.agents.gatekeeper as gk
    import jobhunt.store.db as store

    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    SQLModel.metadata.create_all(engine)

    from contextlib import contextmanager

    @contextmanager
    def _session():
        with Session(engine, expire_on_commit=False) as s:
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise

    monkeypatch.setattr(gk, "session", _session)
    monkeypatch.setattr(store, "session", _session)
    return _session


def _seed(session_factory, title: str, status, band) -> int:
    from jobhunt.store.models import Job

    with session_factory() as s:
        job = Job(
            source="test",
            source_id=f"audit-{title}-{status.value}",
            url="https://example.com",
            company="MongoDB",
            title=title,
            dedupe_hash=title,
            score=4.2,
            status=status,
            level_band=band,
        )
        s.add(job)
        s.commit()
        return job.id


def test_audit_finds_a_too_senior_job_that_already_passed(audit_db):
    from jobhunt.agents.gatekeeper import audit_progressed
    from jobhunt.store.models import JobStatus

    _seed(audit_db, "Software Engineer 3", JobStatus.QUEUED_APPROVAL, LevelBand.DIRECT)

    report = audit_progressed(apply_changes=False)
    assert [f.new_band for f in report.rejected] == ["reject"]
    assert "level 3" in report.rejected[0].reason


def test_audit_without_fix_writes_nothing(audit_db):
    from jobhunt.agents.gatekeeper import audit_progressed
    from jobhunt.store.models import Job, JobStatus

    job_id = _seed(audit_db, "Software Engineer 3", JobStatus.SCORED, LevelBand.DIRECT)

    audit_progressed(apply_changes=False)
    with audit_db() as s:
        assert s.get(Job, job_id).status == JobStatus.SCORED


def test_audit_with_fix_demotes_it(audit_db):
    from jobhunt.agents.gatekeeper import audit_progressed
    from jobhunt.store.models import Job, JobStatus

    job_id = _seed(audit_db, "Software Engineer 3", JobStatus.SCORED, LevelBand.DIRECT)

    audit_progressed(apply_changes=True)
    with audit_db() as s:
        job = s.get(Job, job_id)
        assert job.status == JobStatus.REJECTED_GATE
        assert "re-audited" in (job.status_reason or "")


def test_audit_rebands_without_rejecting(audit_db):
    """An SDE II mislabelled `direct` is a relabel, not a rejection."""
    from jobhunt.agents.gatekeeper import audit_progressed
    from jobhunt.store.models import Job, JobStatus

    job_id = _seed(
        audit_db, "Software Development Engineer II", JobStatus.SCORED, LevelBand.DIRECT
    )

    report = audit_progressed(apply_changes=True)
    assert report.rejected == []
    assert [f.new_band for f in report.rebanded] == ["stretch"]
    with audit_db() as s:
        job = s.get(Job, job_id)
        assert job.status == JobStatus.SCORED
        assert job.level_band == LevelBand.STRETCH


def test_audit_never_touches_a_sent_application(audit_db):
    """The application is already out; rewriting its status only loses history."""
    from jobhunt.agents.gatekeeper import audit_progressed
    from jobhunt.store.models import Job, JobStatus

    job_id = _seed(audit_db, "Software Engineer 3", JobStatus.APPLIED, LevelBand.DIRECT)

    report = audit_progressed(apply_changes=True)
    assert report.rejected == []
    with audit_db() as s:
        assert s.get(Job, job_id).status == JobStatus.APPLIED
