"""Analyst scoring arithmetic and the eligibility-override rules.

The LLM call itself is not tested here - what is tested is everything that
decides what happens to its answer, which is where a silent mistake would
send the candidate a job they cannot legally take, or bury one he could.
"""

from __future__ import annotations

import pytest

from jobhunt.agents.analyst import DIMENSIONS, ScoreStats, _apply, weighted_score
from jobhunt.store.models import Eligibility, Job, JobStatus, LevelBand

GOOD = {
    "skill_overlap": 5, "level_realism": 5, "comp_confidence": 5,
    "eligibility": 5, "ai_relevance": 5, "company_quality": 5,
}


def scores(**overrides) -> dict[str, float]:
    return {**GOOD, **overrides}


def make_job(**kw) -> Job:
    defaults = dict(
        source="ashby", source_id="1", url="u", company="Acme", title="Software Engineer",
        dedupe_hash="h", status=JobStatus.GATED, level_band=LevelBand.DIRECT,
        eligibility=Eligibility.UNKNOWN,
    )
    return Job(**{**defaults, **kw})


# --------------------------------------------------------------------------
# Weighting
# --------------------------------------------------------------------------


def test_weights_sum_to_one():
    assert sum(DIMENSIONS.values()) == pytest.approx(1.0)


def test_all_fives_is_five():
    assert weighted_score(GOOD) == pytest.approx(5.0)


def test_all_threes_is_three():
    assert weighted_score({k: 3 for k in DIMENSIONS}) == pytest.approx(3.0)


def test_missing_dimension_defaults_to_neutral():
    """A malformed LLM response must not silently score 0."""
    assert weighted_score({"skill_overlap": 3}) == pytest.approx(3.0)


# --------------------------------------------------------------------------
# Hard overrides - a single disqualifying dimension caps the total
# --------------------------------------------------------------------------


def test_ineligible_role_cannot_score_well():
    """A job the candidate cannot legally take is not a 4.5 opportunity."""
    assert weighted_score(scores(eligibility=1)) <= 1.5


def test_implausible_level_is_capped():
    """A role that would never interview 1.3 years of experience is capped."""
    assert weighted_score(scores(level_realism=1)) <= 2.5


def test_eligibility_cap_beats_level_cap():
    assert weighted_score(scores(eligibility=1, level_realism=1)) <= 1.5


def test_caps_do_not_fire_at_two():
    """Only a 1 is disqualifying; a 2 is merely bad and stays comparable."""
    assert weighted_score(scores(eligibility=2, level_realism=2)) > 2.5


# --------------------------------------------------------------------------
# Eligibility overrides
# --------------------------------------------------------------------------


def test_analyst_resolves_unknown_eligibility():
    job = make_job(eligibility=Eligibility.UNKNOWN)
    stats = ScoreStats()
    _apply(job, {"scores": GOOD, "eligibility_verdict": "sponsorship_offered"}, stats, 4.0)
    assert job.eligibility is Eligibility.SPONSORSHIP_OFFERED
    assert stats.reclassified == 1


def test_analyst_may_downgrade_a_false_positive():
    """Real case: a posting headed "Anywhere in the World" whose body
    restricted hiring to five European countries."""
    job = make_job(eligibility=Eligibility.INDIA_ELIGIBLE)
    stats = ScoreStats()
    _apply(
        job,
        {"scores": scores(eligibility=1), "eligibility_verdict": "us_only_no_sponsorship"},
        stats,
        4.0,
    )
    assert job.eligibility is Eligibility.US_ONLY_NO_SPONSORSHIP
    assert job.status is JobStatus.REJECTED_GATE


def test_analyst_may_not_overturn_an_explicit_refusal():
    """The posting stated outright it will not sponsor; inference cannot undo that."""
    job = make_job(eligibility=Eligibility.US_ONLY_NO_SPONSORSHIP)
    stats = ScoreStats()
    _apply(job, {"scores": GOOD, "eligibility_verdict": "india_eligible"}, stats, 4.0)
    assert job.eligibility is Eligibility.US_ONLY_NO_SPONSORSHIP
    assert stats.reclassified == 0


def test_analyst_may_not_upgrade_india_to_sponsorship():
    """Only downgrades and UNKNOWN resolutions are accepted."""
    job = make_job(eligibility=Eligibility.INDIA_ELIGIBLE)
    stats = ScoreStats()
    _apply(job, {"scores": GOOD, "eligibility_verdict": "sponsorship_offered"}, stats, 4.0)
    assert job.eligibility is Eligibility.INDIA_ELIGIBLE


def test_garbage_verdict_is_ignored_not_crashed():
    job = make_job(eligibility=Eligibility.UNKNOWN)
    stats = ScoreStats()
    _apply(job, {"scores": GOOD, "eligibility_verdict": "maybe?"}, stats, 4.0)
    assert job.eligibility is Eligibility.UNKNOWN
    assert stats.reclassified == 0


# --------------------------------------------------------------------------
# Threshold routing
# --------------------------------------------------------------------------


def test_above_threshold_is_kept_for_tailoring():
    job = make_job(eligibility=Eligibility.INDIA_ELIGIBLE)
    stats = ScoreStats()
    _apply(job, {"scores": GOOD, "summary": "great"}, stats, 4.0)
    assert job.status is JobStatus.SCORED
    assert stats.above_threshold == 1


def test_below_threshold_is_archived_with_a_reason():
    """A filtered job must always say why, or the funnel cannot be tuned."""
    job = make_job(eligibility=Eligibility.INDIA_ELIGIBLE)
    stats = ScoreStats()
    _apply(job, {"scores": {k: 2 for k in DIMENSIONS}, "summary": "weak"}, stats, 4.0)
    assert job.status is JobStatus.ARCHIVED
    assert job.status_reason and "below" in job.status_reason
    assert job.score_summary == "weak"
