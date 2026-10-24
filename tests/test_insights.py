"""Response analytics, and the rule that keeps them honest.

Every tool in this category advertises "3x more interviews" off methodology
nobody publishes. The point of this page is to not do that, so the test that
matters most is the one asserting a percentage is *withheld* when the sample
is too small to support it.
"""

from __future__ import annotations

from jobhunt.web.app import (
    ADVANCED,
    MIN_FOR_RATE,
    REPLIED,
    SENT,
    _breakdown,
    _coverage_band,
    _rate,
    _score_band,
)


def test_rate_is_withheld_below_the_threshold():
    assert _rate(1, 3) == "1/3"
    assert _rate(2, MIN_FOR_RATE - 1) == f"2/{MIN_FOR_RATE - 1}"


def test_rate_is_quoted_once_the_sample_supports_it():
    assert _rate(5, 10) == "50%"
    assert _rate(3, 20) == "15%"


def test_no_applications_is_a_dash_not_a_zero_percent():
    """0% implies you tried and failed; there is a difference."""
    assert _rate(0, 0) == "—"


def test_awaiting_submit_is_not_counted_as_sent():
    """A prefilled form is not an application."""
    assert "awaiting_your_submit" not in SENT
    assert "awaiting_your_submit" not in REPLIED


def test_rejection_is_a_reply_but_not_an_advance():
    assert "rejected" in REPLIED
    assert "rejected" not in ADVANCED


def test_breakdown_counts_each_outcome():
    rows = [
        ("greenhouse", "submitted"),
        ("greenhouse", "interview"),
        ("greenhouse", "rejected"),
        ("greenhouse", "awaiting_your_submit"),
        ("ashby", "offer"),
    ]
    out = {r["name"]: r for r in _breakdown(rows)}

    assert out["greenhouse"]["sent"] == 3          # the prefilled one does not count
    assert out["greenhouse"]["replied"] == 2       # interview + rejected
    assert out["greenhouse"]["advanced"] == 1      # interview only
    assert out["ashby"]["advanced"] == 1


def test_breakdown_sorts_by_volume():
    rows = [("a", "submitted"), ("b", "submitted"), ("b", "offer")]
    assert [r["name"] for r in _breakdown(rows)] == ["b", "a"]


def test_score_bands_split_where_the_decisions_are():
    assert _score_band(4.65) == "4.50+"
    assert _score_band(4.25) == "4.25+"
    assert _score_band(3.8) == "3.75+"
    assert _score_band(3.1) == "below 3.75"
    assert _score_band(None) == "unscored"


def test_coverage_bands_distinguish_a_perfect_resume():
    from jobhunt.store.models import Resume

    assert _coverage_band(None) == "no resume"
    assert _coverage_band(Resume(job_id=1, pdf_path="x", ats_score=1.0)) == "100%"
    assert _coverage_band(Resume(job_id=1, pdf_path="x", ats_score=0.93)) == "90-99%"
    assert _coverage_band(Resume(job_id=1, pdf_path="x", ats_score=0.6)) == "under 85%"
