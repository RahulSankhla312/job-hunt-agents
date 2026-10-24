"""A submit click is not a submission until the page says so.

Ashby refused two applications as "possible spam" and both were recorded as
sent, because nothing read the page back after clicking. Reporting an
application that does not exist is the worst failure this program has: a
rejection is recoverable, but believing you already applied stops you applying
at all.
"""

from __future__ import annotations

import pytest

from jobhunt.agents.applicant import _SUBMIT_CONFIRMED, _SUBMIT_REJECTED


class FakePage:
    def __init__(self, body: str = "", raises: bool = False):
        self._body, self._raises = body, raises

    def inner_text(self, _selector: str) -> str:
        if self._raises:
            raise RuntimeError("navigation destroyed the context")
        return self._body


ASHBY_SPAM = (
    "We couldn't submit your application. Your application submission was "
    "flagged as possible spam. If you believe this was a mistake, please "
    "submit your application again."
)


def test_the_real_ashby_rejection_is_detected():
    from jobhunt.agents.applicant import submit_rejected

    reason = submit_rejected(FakePage(ASHBY_SPAM))
    assert reason
    assert "spam" in reason.lower()


@pytest.mark.parametrize(
    "body",
    [
        "Something went wrong. Please try again.",
        "Please correct the errors below",
        "This field is required",
        "Unable to submit your application at this time",
    ],
)
def test_other_refusals_are_detected(body):
    from jobhunt.agents.applicant import submit_rejected

    assert submit_rejected(FakePage(body))


@pytest.mark.parametrize(
    "body",
    [
        "Thank you for applying to Sarvam AI!",
        "Your application has been received.",
        "We have received your application and will be in touch.",
        "Application submitted successfully",
    ],
)
def test_confirmations_are_recognised(body):
    from jobhunt.agents.applicant import submit_confirmed

    assert submit_confirmed(FakePage(body))


def test_a_confirmation_is_not_read_as_a_rejection():
    from jobhunt.agents.applicant import submit_rejected

    assert submit_rejected(FakePage("Thank you for applying!")) == ""


def test_an_unreadable_page_is_never_a_success():
    """A page that cannot be read must fail closed, not pass."""
    from jobhunt.agents.applicant import submit_confirmed, submit_rejected

    assert submit_rejected(FakePage(raises=True))
    assert submit_confirmed(FakePage(raises=True)) is False


def test_silence_is_not_confirmation():
    """No banner either way is not proof it worked."""
    from jobhunt.agents.applicant import submit_confirmed, submit_rejected

    page = FakePage("Backend Engineer, Chanakya  Location Bengaluru")
    assert submit_rejected(page) == ""
    assert submit_confirmed(page) is False


def test_the_patterns_do_not_overlap():
    for body in ("Thank you for applying", "Your application has been received"):
        assert _SUBMIT_CONFIRMED.search(body)
        assert not _SUBMIT_REJECTED.search(body)
