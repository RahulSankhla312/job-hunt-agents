"""Autonomy gating and form answering.

The browser automation needs a live page, so what is tested here is the policy
that decides *whether to submit at all*. Getting that wrong is the expensive
failure: a half-answered application does not just fail, it burns the company.
"""

from __future__ import annotations

import pytest

from jobhunt.agents.applicant import _ESSAY_MARKERS, answer_for, detect_ats, may_autosubmit
from jobhunt.resume.profile import Identity, Preferences, Profile

PROFILE = Profile(
    identity=Identity(
        name="Aarav Mehta",
        email="aarav.mehta@example.com",
        phone="+91 9000012345",
        location="Bengaluru, India",
        linkedin="linkedin.com/in/aarav-mehta-example",
        github="github.com/aarav-mehta-example",
    ),
    preferences=Preferences(
        current_ctc_lpa=24.0,
        expected_min_ctc_lpa=36.0,
        notice_period_days=90,
        years_experience=1.3,
        work_authorization="Indian citizen; needs sponsorship for US roles",
    ),
)


@pytest.fixture
def autonomy(monkeypatch):
    """Override the autonomy config without touching targets.yaml."""

    def _set(**overrides):
        import jobhunt.agents.applicant as mod

        base = {
            "shadow_mode": False,
            "auto_submit_clean_ats": True,
            "auto_submit_with_essays": False,
            "auto_submit_workday": False,
            "auto_submit_unknown_ats": False,
        }
        monkeypatch.setattr(mod, "targets", lambda: {"autonomy": {**base, **overrides}})

    return _set


# --------------------------------------------------------------------------
# ATS detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.greenhouse.io/stripe/jobs/123", "greenhouse"),
        ("https://jobs.ashbyhq.com/openai/abc", "ashby"),
        ("https://jobs.lever.co/cred/xyz", "lever"),
        ("https://nvidia.wd5.myworkdayjobs.com/en-US/x/job/y", "workday"),
        ("https://www.amazon.jobs/en/jobs/123", "amazon"),
        ("https://careers.example.com/apply", "unknown"),
    ],
)
def test_detect_ats(url, expected):
    assert detect_ats(url) == expected


# --------------------------------------------------------------------------
# The default is always to ask a human
# --------------------------------------------------------------------------


def test_shadow_mode_blocks_everything(autonomy):
    """The master switch has to win over every other setting."""
    autonomy(shadow_mode=True, auto_submit_clean_ats=True)
    allowed, reason = may_autosubmit("greenhouse", [], [])
    assert not allowed and "shadow" in reason.lower()


def test_clean_known_ats_may_submit(autonomy):
    autonomy()
    allowed, _ = may_autosubmit("greenhouse", [], [])
    assert allowed


def test_essay_question_always_queues(autonomy):
    """A "why this company" answer is the applicant's to write, not the agent's."""
    autonomy()
    allowed, reason = may_autosubmit("greenhouse", ["Why do you want to work here?"], [])
    assert not allowed and "free-text" in reason


def test_unknown_ats_queues(autonomy):
    autonomy()
    allowed, reason = may_autosubmit("unknown", [], [])
    assert not allowed and "unrecognised" in reason


def test_workday_queues_by_default(autonomy):
    """Workday is multi-step and account-gated; blind submission is not safe."""
    autonomy()
    allowed, _ = may_autosubmit("workday", [], [])
    assert not allowed


def test_unanswered_required_field_queues(autonomy):
    """Submitting with a required field blank wastes the application."""
    autonomy()
    allowed, reason = may_autosubmit("greenhouse", [], ["Sponsorship required?"])
    assert not allowed and "required" in reason


def test_each_gate_can_be_opened_individually(autonomy):
    autonomy(auto_submit_with_essays=True)
    assert may_autosubmit("greenhouse", ["Why us?"], [])[0]
    autonomy(auto_submit_unknown_ats=True)
    assert may_autosubmit("unknown", [], [])[0]


# --------------------------------------------------------------------------
# Answering from the profile
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Notice period (days)", "90"),
        ("Current CTC", "24.0"),
        ("Expected CTC in LPA", "36.0"),
        ("Total years of experience", "1.3"),
        ("LinkedIn profile URL", "linkedin.com/in/aarav-mehta-example"),
        ("GitHub", "github.com/aarav-mehta-example"),
        ("Current location", "Bengaluru, India"),
    ],
)
def test_factual_questions_are_answered(label, expected):
    assert answer_for(label, PROFILE) == expected


@pytest.mark.parametrize(
    "label",
    [
        "Why do you want to work at Stripe?",
        "Tell us about a hard technical problem",
        "Describe your proudest project",
        "What interests you about this role?",
    ],
)
def test_judgement_questions_are_not_answered(label):
    """These must return nothing so they surface as essay questions."""
    assert answer_for(label, PROFILE) is None
    assert _ESSAY_MARKERS.search(label)


def test_missing_profile_value_yields_nothing_rather_than_blank():
    """An empty answer would look filled and pass the required-field check."""
    bare = Profile(identity=Identity(name="X"), preferences=Preferences())
    assert answer_for("Notice period", bare) is None
    assert answer_for("LinkedIn", bare) is None


# --------------------------------------------------------------------------
# The form audit has to fail closed
# --------------------------------------------------------------------------


def test_unverifiable_form_blocks_submission(autonomy):
    """If the page could not be audited, that is not permission to submit."""
    autonomy()
    allowed, reason = may_autosubmit("greenhouse", [], ["could not verify the form"])
    assert not allowed and "required" in reason


def test_any_blank_control_blocks_submission(autonomy):
    """Real forms mark requiredness in their own validation, not the DOM.

    On a live Ashby form only 5 of 43 controls carried an HTML `required`
    attribute; Location, work-location preferences and the sponsorship
    question were enforced by React alone. Trusting the attribute reported
    "nothing unanswered" for a form that was two-thirds empty, so the audit
    reports every empty control and anything outstanding queues.
    """
    autonomy()
    blanks = ["Location", "Preferred Work Location", "Education History"]
    allowed, reason = may_autosubmit("ashby", [], blanks)
    assert not allowed
    assert "3 required field" in reason


def test_a_genuinely_complete_form_still_submits(autonomy):
    """Failing closed must not mean never submitting anything."""
    autonomy()
    assert may_autosubmit("greenhouse", [], [])[0]


@pytest.mark.parametrize(
    ("url", "ats", "expected_suffix"),
    [
        ("https://jobs.ashbyhq.com/plaid/abc", "ashby", "/application"),
        ("https://jobs.lever.co/cred/xyz", "lever", "/apply"),
    ],
)
def test_application_url_points_at_the_form_not_the_posting(url, ats, expected_suffix):
    """Landing on the posting found no inputs and filled nothing silently."""
    from jobhunt.agents.applicant import application_url
    from jobhunt.store.models import Job

    job = Job(
        source="t", source_id="1", url=url, company="C", title="T", dedupe_hash="h"
    )
    assert application_url(job, ats).endswith(expected_suffix)


def test_application_url_is_not_doubled_up():
    from jobhunt.agents.applicant import application_url
    from jobhunt.store.models import Job

    job = Job(
        source="t", source_id="1", url="https://jobs.ashbyhq.com/plaid/abc/application",
        company="C", title="T", dedupe_hash="h",
    )
    assert application_url(job, "ashby").count("/application") == 1


# --------------------------------------------------------------------------
# Stored answers to the questions forms repeat
# --------------------------------------------------------------------------


@pytest.fixture
def answered() -> Profile:
    from jobhunt.resume.profile import ApplicationAnswers

    return Profile(
        identity=PROFILE.identity,
        preferences=PROFILE.preferences,
        answers=ApplicationAnswers(
            needs_sponsorship_abroad=True,
            needs_sponsorship_india=False,
            gender="Male",
            race_ethnicity="Asian",
            hispanic_latino="No",
            veteran_status="I am not a protected veteran",
            disability_status="No, I do not have a disability",
            willing_to_relocate_domestic=True,
            can_work_hybrid=True,
            consent_to_sms=False,
        ),
    )


def test_sponsorship_depends_on_where_the_job_is(answered):
    """A blanket answer is wrong in one direction or the other."""
    from jobhunt.agents.applicant import choose_option

    yes_no = ["Yes", "No"]
    assert choose_option("Require visa sponsorship?", yes_no, answered, "San Francisco") == "Yes"
    assert choose_option("Require visa sponsorship?", yes_no, answered, "Bengaluru, India") == "No"


@pytest.mark.parametrize(
    ("question", "options", "expected"),
    [
        ("Gender", ["Male", "Female", "Decline to self-identify"], "Male"),
        # Forms word the same choice differently.
        ("Gender identity", ["Man", "Woman", "Non-binary", "Prefer not to say"], "Man"),
        ("Race/Ethnicity", ["Asian", "White", "I do not wish to answer"], "Asian"),
        (
            "Veteran status",
            ["I identify as a protected veteran", "I am not a protected veteran"],
            "I am not a protected veteran",
        ),
        (
            "Disability status",
            ["Yes, I have a disability", "No, I do not have a disability"],
            "No, I do not have a disability",
        ),
        ("Have you previously been employed by us?", ["Yes", "No"], "No"),
        ("Are you willing to relocate?", ["Yes", "No"], "Yes"),
        # The office question is location-dependent and is covered in
        # test_form_choices.py: answering it from `can_work_hybrid` alone
        # put "Yes, I can work from San Francisco" on a real application.
        (
            "Consent to receiving text messages",
            ["Yes - I consent", "No - I do not consent"],
            "No - I do not consent",
        ),
    ],
)
def test_recurring_questions_are_answered(answered, question, options, expected):
    from jobhunt.agents.applicant import choose_option

    assert choose_option(question, options, answered) == expected


def test_eeo_falls_back_to_declining_when_nothing_matches(answered):
    """A category that does not appear must not be forced onto a wrong option."""
    from jobhunt.agents.applicant import choose_option

    picked = choose_option(
        "Race/Ethnicity", ["White", "Black or African American", "Decline to self-identify"],
        answered,
    )
    assert picked == "Decline to self-identify"


def test_an_unrecognised_question_is_left_alone(answered):
    """The agent must never guess at a question it has no stored answer for."""
    from jobhunt.agents.applicant import choose_option

    assert choose_option("What is your favourite programming language?", ["Python", "Go"], answered) is None
    assert choose_option("Preferred shift", ["Day", "Night"], answered) is None


def test_missing_answer_leaves_the_question_blank():
    """An unset field must surface for a human, not resolve to something."""
    from jobhunt.agents.applicant import choose_option

    blank = Profile(identity=PROFILE.identity)
    assert choose_option("Gender", ["Male", "Female"], blank) is None
