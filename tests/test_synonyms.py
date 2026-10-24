"""Keyword synonyms in the ATS coverage check.

A JD asking for "K8s" against a resume saying "Kubernetes" is a match to any
human reader and to a competent ATS. Comparing raw strings got it wrong twice
over - the term was counted as a skill the candidate lacks *and* as one the resume
failed to surface - and coverage gates whether a resume ships at all.
"""

from __future__ import annotations

import pytest

from jobhunt.resume.vocab import equivalents, known_to, matches_any


@pytest.mark.parametrize(
    ("term", "other"),
    [
        ("k8s", "kubernetes"),
        ("postgres", "postgresql"),
        ("js", "javascript"),
        ("golang", "go"),
        ("nodejs", "node"),
        ("cpp", "c++"),
        ("llms", "llm"),
        ("sklearn", "scikit-learn"),
        ("apis", "api"),
    ],
)
def test_spellings_are_interchangeable(term, other):
    assert other in equivalents(term)
    assert term in equivalents(other)


def test_unknown_term_is_its_own_group():
    """Callers use equivalents() unconditionally, so it must never return empty."""
    assert equivalents("bazel") == frozenset({"bazel"})


def test_matches_any_reads_the_resume_text():
    resume = "engineered ci/cd pipelines on kubernetes at northwind"
    assert matches_any("k8s", resume)
    assert matches_any("cicd", resume)
    assert not matches_any("terraform", resume)


def test_known_to_reads_the_profile():
    profile = {"kubernetes", "python", "typescript"}
    assert known_to("k8s", profile)
    assert known_to("ts", profile)
    assert not known_to("rust", profile)


def test_related_is_not_equivalent():
    """A false grouping inflates the one number the check exists to report."""
    assert "kubernetes" not in equivalents("docker")
    assert "monitoring" not in equivalents("observability")


def test_case_and_padding_are_ignored():
    assert "kubernetes" in equivalents("  K8S ")


def test_coverage_counts_a_synonym_as_surfaced():
    """The end-to-end effect: the JD's spelling, the resume's spelling."""
    wanted = ["k8s", "postgres", "rust"]
    have = {"kubernetes", "postgresql"}
    resume = "shipped services on kubernetes backed by postgresql"

    relevant = [w for w in wanted if known_to(w, have)]
    unmet = [w for w in wanted if not known_to(w, have)]
    present = {w for w in relevant if matches_any(w, resume)}

    assert relevant == ["k8s", "postgres"]
    assert unmet == ["rust"]          # genuinely absent, correctly reported
    assert present == {"k8s", "postgres"}
    assert len(present) / len(relevant) == 1.0


# --------------------------------------------------------------------------
# One skill, one slot in the denominator
# --------------------------------------------------------------------------


def test_spellings_collapse_to_one_denominator_entry():
    """A JD writing both "full-stack" and "fullstack" is asking for one thing.

    Counting both put it in the denominator twice, so a resume using neither
    spelling lost two points instead of one. That is exactly how the Plaid
    full-stack resume scored 83% against an 85% gate and never shipped.
    """
    from jobhunt.resume.render import _one_per_meaning

    wanted = ["backend", "full-stack", "fullstack", "api", "apis", "python"]
    assert _one_per_meaning(wanted) == ["backend", "full-stack", "api", "python"]


def test_the_posting_s_preferred_spelling_wins():
    """Terms arrive frequency-ranked, so the first occurrence is the common one."""
    from jobhunt.resume.render import _one_per_meaning

    assert _one_per_meaning(["k8s", "kubernetes"]) == ["k8s"]
    assert _one_per_meaning(["kubernetes", "k8s"]) == ["kubernetes"]


def test_unrelated_terms_are_untouched():
    from jobhunt.resume.render import _one_per_meaning

    terms = ["python", "java", "docker", "kubernetes"]
    assert _one_per_meaning(terms) == terms


def test_the_plaid_case_end_to_end():
    """The real numbers: 10 of 12 was 83% and failed, 9 of 10 is 90% and passes.

    Two pairs collapse - api/apis and full-stack/fullstack - so the twelve
    terms the JD used name ten distinct skills. The resume surfaced every one
    of them except full-stack.
    """
    from jobhunt.resume.render import MIN_COVERAGE, _one_per_meaning

    wanted = [
        "backend", "frontend", "apis", "javascript", "api", "full-stack",
        "fullstack", "java", "node.js", "performance", "python", "security",
    ]
    collapsed = _one_per_meaning(wanted)
    assert len(wanted) == 12
    assert len(collapsed) == 10

    surfaced = [w for w in collapsed if w != "full-stack"]
    assert len(surfaced) == 9
    assert MIN_COVERAGE > 10 / 12          # what it scored before
    assert MIN_COVERAGE <= 9 / 10          # what it scores now
