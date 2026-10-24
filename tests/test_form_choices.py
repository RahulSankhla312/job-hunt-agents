"""The choice-answering layer, and the JS that finds the questions.

This suite exists because the scanner was dead: a Python triple-quoted string
turned the JS `split('\n')` into a real newline, so the script never parsed and
every radio, checkbox, select and Yes/No question on every form went
unanswered. The failure was caught and logged at debug level, so nothing in the
output ever said so.
"""

from __future__ import annotations

import re

import pytest

from jobhunt.agents.applicant import (
    _CHOICES_JS,
    _LABEL_JS,
    _REQUIRED_JS,
    _office_answer,
    reachable,
)
from jobhunt.config import settings
from jobhunt.resume.profile import Profile


@pytest.fixture(scope="module")
def profile():
    return Profile.load(settings().profile_path)


@pytest.mark.parametrize(
    ("name", "script"),
    [("_CHOICES_JS", _CHOICES_JS), ("_REQUIRED_JS", _REQUIRED_JS), ("_LABEL_JS", _LABEL_JS)],
)
def test_the_embedded_js_has_no_unterminated_string(name, script):
    """A raw newline inside a JS string literal is a syntax error.

    Python's own escape handling is the trap: `\n` inside a non-raw
    triple-quoted string becomes a newline before the browser ever sees it.
    """
    for n, line in enumerate(script.splitlines(), 1):
        stripped = re.sub(r"\.", "", line)
        for quote in ("'", '"'):
            assert stripped.count(quote) % 2 == 0, f"{name} line {n}: {line[:60]}"


@pytest.mark.parametrize(
    ("name", "script"),
    [("_CHOICES_JS", _CHOICES_JS), ("_REQUIRED_JS", _REQUIRED_JS), ("_LABEL_JS", _LABEL_JS)],
)
def test_the_embedded_js_is_a_balanced_arrow_function(name, script):
    assert "=>" in script.split("{", 1)[0], name
    assert script.count("{") == script.count("}"), name
    assert script.count("(") == script.count(")"), name


# --------------------------------------------------------------------------
# Can he actually get to the office?
# --------------------------------------------------------------------------


def test_a_us_office_is_not_reachable_from_bengaluru(profile):
    assert reachable("San Francisco HQ", profile) is False
    assert reachable("New York, NY", profile) is False


def test_his_own_cities_are_reachable(profile):
    assert reachable("Bengaluru, Karnataka, India", profile) is True
    assert reachable("Hyderabad, India", profile) is True


def test_remote_is_reachable(profile):
    assert reachable("Remote - Worldwide", profile) is True


def test_an_unstated_location_is_unknown(profile):
    """Unknown must not collapse into yes or no."""
    assert reachable("", profile) is None


def test_the_office_question_is_never_answered_falsely(profile):
    """"Are you able to work from this office?" on a San Francisco role.

    Answering Yes from `can_work_hybrid` put a false statement on a real job
    application. Left unanswered it blocks the auto-submit, which is correct:
    saying No usually withdraws the application, so it is his call.
    """
    assert _office_answer("San Francisco HQ", profile) is None
    assert _office_answer("New York City Office", profile) is None


def test_the_office_question_is_answered_where_he_could_work(profile):
    assert _office_answer("Bengaluru, Karnataka, India", profile) == "Yes"
