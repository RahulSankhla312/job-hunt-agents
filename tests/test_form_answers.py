"""Facts a form asks for that the profile already knows.

Every field here was one an application stalled on. A form that cannot be
completed is not submitted, so each unanswered fact is the difference between
an application going out and sitting in the queue.
"""

from __future__ import annotations

import pytest

from jobhunt.agents.applicant import answer_for
from jobhunt.config import settings
from jobhunt.resume.profile import Experience, Profile


@pytest.fixture(scope="module")
def profile():
    return Profile.load(settings().profile_path)


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("College Name", "Indian Institute of Information Technology, Pune"),
        ("University", "Indian Institute of Information Technology, Pune"),
        ("School", "Indian Institute of Information Technology, Pune"),
        ("Graduation Branch", "Computer Science and Engineering"),
        ("Field of Study", "Computer Science and Engineering"),
        ("Degree", "Bachelor of Technology"),
        ("Graduation Completion Year", "2025"),
    ],
)
def test_the_education_block_is_answered(profile, label, expected):
    assert answer_for(label, profile) == expected


def test_the_grade_is_given_as_a_number(profile):
    """Stored as "CPI: 8.1" for the resume; a form field wants just the number."""
    assert answer_for("CGPA in Under graduation Degree", profile) == "8.1"


def test_money_and_notice_are_answered(profile):
    assert answer_for("Current Annual CTC", profile) == "24.0"
    assert answer_for("Expected Annual CTC", profile) == "36.0"
    assert answer_for("Notice Period", profile) == "60"


def test_country_is_taken_from_the_location(profile):
    assert answer_for("Country", profile) == "India"


# --------------------------------------------------------------------------
# Years of experience
# --------------------------------------------------------------------------


def test_experience_is_counted_from_the_dates(profile):
    """Derived, not stored: a number in a config file is wrong within months."""
    years = float(profile.years_of_experience())
    assert 1.0 <= years <= 2.0


def test_internships_are_not_counted():
    """The forms asking this say "not counting internships"."""
    p = Profile(
        experience=[
            Experience(title="Software Engineer", start="July 2025", end="July 2026"),
            Experience(title="Technical Intern", start="October 2023", end="January 2024"),
        ]
    )
    assert p.years_of_experience() == "1.0"


def test_a_present_role_counts_up_to_today():
    p = Profile(experience=[Experience(title="Engineer", start="January 2020", end="Present")])
    assert float(p.years_of_experience()) > 5


def test_no_dates_means_no_answer():
    """Better to report it unanswered than to invent a number."""
    assert Profile().years_of_experience() == ""


def test_an_unparseable_date_is_skipped_not_guessed():
    p = Profile(experience=[Experience(title="Engineer", start="sometime", end="later")])
    assert p.years_of_experience() == ""


def test_education_with_no_entry_answers_nothing():
    assert answer_for("College Name", Profile()) is None


# --------------------------------------------------------------------------
# Matching order
# --------------------------------------------------------------------------
#
# _KNOWN_QUESTIONS is matched by substring, first hit wins. Several labels
# contain more than one key, so the order is load-bearing rather than
# cosmetic.


@pytest.mark.parametrize(
    ("label", "expected_key"),
    [
        # "...Degree" also contains "degree"; the grade must win.
        ("CGPA in Under graduation Degree (10 point scale)", "grade"),
        ("Graduation Completion Year", "graduation_year"),
        # "Current Annual CTC" contains neither "current ctc" nor "annual ctc"
        # as written, so it needs its own entry.
        ("Current Annual CTC (With Break-up)", "current_ctc"),
        ("Expected Annual CTC", "expected_ctc"),
        # "Who is your current or previous employer" contains "employer".
        ("Who is your current or previous employer", "current_company"),
        ("Total years of work experience", "years_experience"),
    ],
)
def test_the_most_specific_key_wins(label, expected_key):
    from jobhunt.agents.applicant import _KNOWN_QUESTIONS

    low = label.lower()
    hit = next((v for k, v in _KNOWN_QUESTIONS.items() if k in low), None)
    assert hit == expected_key


def test_a_grade_field_never_receives_a_degree_name(profile):
    """The specific regression: order put "degree" ahead of "cgpa"."""
    answer = answer_for("CGPA in Under graduation Degree", profile)
    assert answer == "8.1"
    assert "Bachelor" not in (answer or "")
