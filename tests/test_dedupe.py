"""Deduplication.

Every "must not merge" case below was found by auditing real OpenAI and
Databricks boards against an earlier, looser implementation that used a bare
fuzzy ratio. It merged all of them. A false merge silently destroys a job
opportunity, so these are the important tests in this file.
"""

from __future__ import annotations

import pytest

from jobhunt.agents.scout import collapse_exact, collapse_near
from jobhunt.sources.base import RawJob, distinguishing_tokens, title_level


def job(title: str, company: str = "OpenAI", *, source: str = "ashby", location: str = "SF",
        sid: str | None = None, comp: float | None = None, desc: str = "x" * 300) -> RawJob:
    return RawJob(
        source=source,
        source_id=sid or f"{company}-{title}",
        title=title,
        company=company,
        url="https://example.test/j",
        location=location,
        description=desc,
        comp_min=comp,
    )


# --------------------------------------------------------------------------
# Must NOT merge - all of these were wrongly collapsed by the fuzzy-only rule
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # Seniority: merging these defeats the entire level filter.
        ("Software Engineer II", "Software Engineer III"),
        ("Senior Software Engineer", "Staff Software Engineer"),
        # One differing content word, but a completely different team.
        ("Software Engineer, Data Infrastructure", "Software Engineer, Agent Infrastructure"),
        ("Software Engineer, AI Safety", "Software Engineer, API Safety"),
        ("Software Engineer, GPU Infrastructure", "Software Engineer, GPT Infrastructure"),
        ("Full Stack Software Engineer, Gov", "Full Stack Software Engineer, Growth"),
        # The parenthetical is the distinguishing detail.
        ("Software Engineer, Backend (Cooperative AI)", "Backend Software Engineer (Evals)"),
        ("Software Engineer, Privacy Engineering", "Software Engineer, Privacy (Lawful Access)"),
        # Different disciplines that share generic words.
        ("AI Engineer", "ML Engineer"),
        ("Software Engineer, Platform", "Software Engineer, Security"),
    ],
)
def test_distinct_roles_are_not_merged(a, b):
    survivors, dropped = collapse_near([job(a), job(b)])
    assert dropped == 0, f"WRONGLY MERGED: {a!r} into {b!r}"
    assert len(survivors) == 2


# --------------------------------------------------------------------------
# Must merge - genuine re-posts of one role
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Software Engineer, Backend", "Backend Software Engineer"),
        ("Senior Engineer, Payments", "Payments Senior Engineer"),
        ("Software Engineer - Platform", "Software Engineer, Platform"),
    ],
)
def test_word_order_variants_are_merged(a, b):
    survivors, dropped = collapse_near([job(a, sid="a"), job(b, sid="b")])
    assert dropped == 1, f"failed to merge {a!r} with {b!r}"
    assert len(survivors) == 1


def test_same_title_at_different_companies_is_never_merged():
    survivors, dropped = collapse_near(
        [job("Software Engineer", "Stripe"), job("Software Engineer", "Rubrik")]
    )
    assert dropped == 0
    assert len(survivors) == 2


# --------------------------------------------------------------------------
# Exact dedupe + survivor selection
# --------------------------------------------------------------------------


def test_exact_duplicate_across_sources_collapses():
    a = job("Software Engineer", source="greenhouse", sid="1")
    b = job("Software Engineer", source="remoteok", sid="2")
    survivors, dropped = collapse_exact([a, b])
    assert dropped == 1
    assert len(survivors) == 1


def test_richest_record_survives_a_collapse():
    """An Ashby row with a salary band must beat an aggregator row without."""
    poor = job("Software Engineer", source="remoteok", sid="1", desc="short")
    rich = job("Software Engineer", source="ashby", sid="2", comp=250_000, desc="x" * 4000)
    for order in ([poor, rich], [rich, poor]):
        survivors, _ = collapse_exact(order)
        assert survivors[0].source == "ashby"
        assert survivors[0].comp_min == 250_000


def test_location_separates_otherwise_identical_roles():
    a = job("Software Engineer", location="Bengaluru, India")
    b = job("Software Engineer", location="San Francisco")
    survivors, dropped = collapse_exact([a, b])
    assert dropped == 0
    assert len(survivors) == 2


def test_bengaluru_and_bangalore_are_the_same_place():
    a = job("Software Engineer", location="Bengaluru, Karnataka, India", sid="1")
    b = job("Software Engineer", location="Bangalore", sid="2")
    assert a.dedupe_hash() == b.dedupe_hash()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Software Engineer II", "2"),
        ("Software Engineer III", "3"),
        ("Senior Software Engineer", "senior"),
        ("Software Engineer", ""),
    ],
)
def test_title_level(title, expected):
    assert title_level(title) == expected


def test_generic_words_are_not_distinguishing():
    assert distinguishing_tokens("Software Engineer") == frozenset()
    assert "backend" in distinguishing_tokens("Backend Software Engineer")
