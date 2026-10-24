"""A personal detail must never answer a question about something else.

"If you selected 'Others' in the college list, please mention the name"
received "Aarav Mehta", because the full-name hint was the bare word "name"
and it is checked before the profile's specific answers. A wrong answer written
into a form is worse than a blank one: a blank blocks the submit and asks for
help, a wrong one goes out.
"""

from __future__ import annotations

import pytest

from jobhunt.agents.applicant import _NAMES_SOMEONE_ELSE, FIELD_HINTS, answer_for
from jobhunt.config import settings
from jobhunt.resume.profile import Profile


@pytest.fixture(scope="module")
def profile():
    return Profile.load(settings().profile_path)


@pytest.mark.parametrize(
    "label",
    [
        "if you selected 'others' in the college list, please mention the name",
        "college name",
        "school name",
        "university name",
        "company name",
        "name of your current employer",
        "hiring manager name",
        "name of the person who referred you",
        "emergency contact name",
    ],
)
def test_someone_elses_name_is_recognised(label):
    assert _NAMES_SOMEONE_ELSE.search(label)


@pytest.mark.parametrize(
    "label",
    ["full name", "your name", "legal name", "candidate name", "preferred first name"],
)
def test_the_applicants_own_name_is_not_excluded(label):
    assert not _NAMES_SOMEONE_ELSE.search(label)


def test_the_full_name_hint_is_not_the_bare_word():
    """The hint that caused it. A bare "name" matches a dozen labels per form."""
    assert "name" not in FIELD_HINTS["full_name"]


@pytest.mark.parametrize(
    "label",
    ["college name", "company name", "name of your current employer"],
)
def test_no_hint_fires_on_someone_elses_name(label):
    """Belt and braces: even if a hint matched, the guard stops it."""
    matched = [f for f, hints in FIELD_HINTS.items() if any(h in label for h in hints)]
    assert not matched or _NAMES_SOMEONE_ELSE.search(label)


def test_the_profile_answer_wins_over_a_generic_hint(profile):
    """"College Name" has a real answer; it must not be treated as a name field."""
    assert answer_for("College Name", profile) == (
        "Indian Institute of Information Technology, Pune"
    )
