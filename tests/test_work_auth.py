"""Work authorisation, which is two opposite questions sharing every keyword.

"Will you require sponsorship?" and "Are you legally authorized to work in the
US?" want opposite answers from the same fact. A single pattern matching both
would answer "yes, I am authorised to work in the United States" on the candidate's
behalf, which is false, and false answers here are the kind that end a process
at the reference check rather than the screen.
"""

from __future__ import annotations

import pytest

from jobhunt.agents.applicant import CHOICE_QUESTIONS, choose_option, combobox_value
from jobhunt.config import settings
from jobhunt.resume.profile import Profile

YES_NO = ["Yes", "No"]


@pytest.fixture(scope="module")
def profile():
    return Profile.load(settings().profile_path)


@pytest.mark.parametrize(
    ("question", "location", "expected"),
    [
        # Abroad: he needs sponsorship, so he is not already authorised.
        ("Will you now or in the future require visa sponsorship?", "Remote: United States", "Yes"),
        ("Are you legally authorized to work in the United States of America?",
         "Remote: United States", "No"),
        ("Are you eligible to work in the UK?", "London, UK", "No"),
        ("Do you have the right to work in Germany?", "Berlin, Germany", "No"),
        # India: authorised, no sponsorship needed.
        ("Will you require sponsorship?", "Bengaluru, India", "No"),
        ("Are you legally authorized to work in India?", "Bengaluru, India", "Yes"),
    ],
)
def test_the_two_questions_get_opposite_answers(profile, question, location, expected):
    assert choose_option(question, YES_NO, profile, location) == expected


def test_the_authorisation_pattern_is_checked_first():
    """Order is the mechanism, so assert the order rather than trusting it.

    Both rows match "authorized to work". If sponsorship came first it would
    win, and the answer would be inverted.
    """
    keys = [key for _pattern, key in CHOICE_QUESTIONS]
    assert keys.index("work_authorised") < keys.index("sponsorship")


def test_an_authorisation_question_never_reaches_the_sponsorship_row():
    q = "Are you legally authorized to work in the United States of America?"
    first = next(key for pattern, key in CHOICE_QUESTIONS if pattern.search(q))
    assert first == "work_authorised"


def test_the_sponsorship_question_still_reaches_its_own_row():
    q = "Will you now or in the future require visa sponsorship for employment?"
    first = next(key for pattern, key in CHOICE_QUESTIONS if pattern.search(q))
    assert first == "sponsorship"


# --------------------------------------------------------------------------
# Typeahead fields
# --------------------------------------------------------------------------


def test_location_typeahead_is_answered_from_the_profile(profile):
    assert combobox_value("Location", profile) == profile.identity.location


def test_how_did_you_hear_is_answered_from_the_stored_answer(profile):
    value = combobox_value("How did you hear about Render?", profile)
    assert value == profile.answers.how_did_you_hear


def test_an_unrecognised_typeahead_is_left_alone(profile):
    """Typing into a widget we cannot answer is worse than leaving it blank."""
    assert combobox_value("Which product line interests you?", profile) is None


def test_the_placeholder_is_never_treated_as_a_question(profile):
    """Ashby's typeaheads all read "Start typing...", which answers nothing."""
    assert combobox_value("Start typing...", profile) is None


# --------------------------------------------------------------------------
# Citizenship, and which country the question is actually about
# --------------------------------------------------------------------------
#
# The fixture candidate is an Indian citizen: not a US citizen, holds no green card, is
# not authorised to work in the US, and requires sponsorship there. Each line
# below is one way a real form asks that, and a wrong answer to any of them is
# a misrepresentation rather than a bug.

US = "Remote: United States"
IN = "Bengaluru, Karnataka, India"


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Are you a U.S. citizen?", "No"),
        ("Are you a citizen or permanent resident of the United States?", "No"),
        ("Do you hold a Green Card?", "No"),
        ("Are you a US national?", "No"),
        ("Are you a citizen of India?", "Yes"),
    ],
)
def test_citizenship_follows_the_country_in_the_question(profile, question, expected):
    assert choose_option(question, YES_NO, profile, US) == expected


def test_an_india_posting_does_not_make_him_a_us_citizen(profile):
    """The country named in the question wins over the country of the job.

    An India-based role at a US company still asks "are you a U.S. citizen?".
    Judged by the posting alone that answered Yes.
    """
    assert choose_option("Are you a U.S. citizen?", YES_NO, profile, IN) == "No"
    assert (
        choose_option(
            "Are you legally authorized to work in the United States?", YES_NO, profile, IN
        )
        == "No"
    )


def test_a_question_naming_no_country_uses_the_posting(profile):
    q = "Are you authorized to work in the country of employment?"
    assert choose_option(q, YES_NO, profile, IN) == "Yes"
    assert choose_option(q, YES_NO, profile, US) == "No"


def test_a_work_permit_is_sponsorship(profile):
    assert choose_option("Do you require a work permit?", YES_NO, profile, US) == "Yes"


def test_being_on_a_visa_is_not_the_sponsorship_question(profile):
    """"Are you currently on a visa?" asked of someone in India is not the
    same question as "will you need one sponsored", and the bare keyword made
    it answer Yes. Unanswered is correct: it blocks the submit and asks him."""
    assert choose_option("Are you currently on a visa?", YES_NO, profile, US) is None


@pytest.mark.parametrize("country", ["United Kingdom", "Canada", "Australia", "Singapore"])
def test_he_is_not_authorised_anywhere_but_india(profile, country):
    q = f"Are you legally authorized to work in {country}?"
    assert choose_option(q, YES_NO, profile, US) == "No"
