"""Free-text answers and cover letters.

The generation itself needs the model, so what is tested here is the gate
around it: what counts as quoting the posting back versus claiming experience,
and the shape of the result the Applicant branches on.
"""

from __future__ import annotations

import pytest

from jobhunt.agents.writer import WrittenText, _strip_posting_terms
from jobhunt.store.models import Job


def make_job(**kw) -> Job:
    base = dict(
        source="test",
        source_id="w1",
        url="https://example.test/j",
        company="Figma",
        title="Software Engineer - Full Stack",
        dedupe_hash="w1",
        location="San Francisco",
        description=(
            "We build design tools. You will work with React, TypeScript and "
            "Ruby on a Postgres backend, and own UI infrastructure end to end."
        ),
    )
    return Job(**{**base, **kw})


# --------------------------------------------------------------------------
# Quoting the posting is not a claim
# --------------------------------------------------------------------------


def test_company_and_role_are_stripped_before_checking():
    """Naming the employer is not claiming to have worked there."""
    cleaned = _strip_posting_terms(
        "I am applying for the Software Engineer - Full Stack role at Figma.", make_job()
    )
    assert "Figma" not in cleaned
    assert "Software Engineer" not in cleaned


def test_technologies_the_posting_names_are_stripped():
    """Referring to what the job asks for is not claiming to have shipped it.

    The truthfulness check still applies to any claim *about the candidate*;
    this only stops the posting's own vocabulary reading as fabrication.
    """
    cleaned = _strip_posting_terms("The role involves React, Ruby and Postgres.", make_job())
    for term in ("React", "Ruby", "Postgres"):
        assert term not in cleaned


def test_short_acronyms_from_the_posting_are_stripped():
    """Regression: a 2-character term like "UI" was below the length cut-off."""
    assert "UI" not in _strip_posting_terms("Shared UI infrastructure matters here.", make_job())


def test_terms_not_in_the_posting_survive_the_strip():
    """Anything the posting never mentions must still reach the validator."""
    cleaned = _strip_posting_terms("I have deep Kubernetes and Kafka experience.", make_job())
    assert "Kubernetes" in cleaned
    assert "Kafka" in cleaned


def test_stripping_a_job_with_no_description_does_not_crash():
    cleaned = _strip_posting_terms("Some text about Figma.", make_job(description=None))
    assert "Figma" not in cleaned


# --------------------------------------------------------------------------
# Result shape the Applicant branches on
# --------------------------------------------------------------------------


def test_ok_requires_text_and_no_violations():
    assert WrittenText(text="hello").ok
    assert not WrittenText(text="").ok
    assert not WrittenText(text="hello", violations=["entity not in profile: 'Rust'"]).ok
    assert not WrittenText(text="hello", error="llm failed").ok


@pytest.mark.parametrize("field", ["violations", "error"])
def test_a_rejected_answer_is_never_treated_as_usable(field):
    """The Applicant must leave it for the human rather than submit it."""
    kwargs = {"text": "some draft", field: (["bad"] if field == "violations" else "boom")}
    assert not WrittenText(**kwargs).ok
