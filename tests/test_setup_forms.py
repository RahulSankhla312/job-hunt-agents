"""The onboarding forms.

A form that silently drops a field shows up later as a wrong answer on a real
application, so parsing is tested field by field rather than trusted.
"""

from __future__ import annotations

import pytest

from jobhunt.resume.profile import ApplicationAnswers, Experience, Profile
from jobhunt.web.setup import initials, merge_parsed, parse, parse_targets, split_lines

VALID = {
    "name": "Asha Rao",
    "email": "asha@example.com",
    "phone": "+91 90000 00001",
    "location": "Pune, India",
    "citizenship": "India",
    "preferred_locations": "Pune, Remote, Pune",
    "open_to_remote": "on",
    "needs_sponsorship_abroad": "on",
    "current_ctc_lpa": "12.5",
    "notice_period_days": "30",
    "exp-0-company": "Acme",
    "exp-0-title": "Software Engineer",
    "exp-0-bullets": "• Built the billing service\n- Cut latency by 30%\n\n",
    "exp-0-tech": "Python, Go",
    "exp-7-company": "",
    "exp-7-title": "",
    "edu-0-institution": "Example University",
    "edu-0-degree": "B.E.",
    "skills_languages": "Python, Go",
    "achievements": "Won a hackathon",
}


def test_a_complete_form_parses_cleanly():
    result = parse(VALID)
    assert result.ok, result.errors
    p = result.profile
    assert p.identity.name == "Asha Rao"
    assert p.preferences.current_ctc_lpa == 12.5
    assert p.preferences.notice_period_days == 30
    # Duplicates collapse, order is kept.
    assert p.preferences.preferred_locations == ["Pune", "Remote"]
    assert p.preferences.open_to_remote is True
    assert p.preferences.willing_to_relocate is False


def test_bullet_markers_and_blank_lines_are_stripped():
    exp = parse(VALID).profile.experience
    assert len(exp) == 1, "an all-blank entry must be dropped, not saved empty"
    assert exp[0].bullets == ["Built the billing service", "Cut latency by 30%"]
    assert exp[0].tech == ["Python", "Go"]


def test_facts_used_by_two_agents_are_written_to_both_places():
    """The Applicant reads answers.*, the prompts read preferences.* - one input."""
    p = parse(VALID).profile
    assert p.answers.preferred_locations == p.preferences.preferred_locations
    assert p.answers.notice_period_days == 30
    assert p.answers.needs_sponsorship_abroad is True
    assert p.answers.needs_sponsorship_india is False


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("name", "Full name"),
        ("email", "Email"),
        ("phone", "Phone"),
        ("location", "location"),
        ("citizenship", "Citizenship"),
    ],
)
def test_required_fields_are_reported(field, message):
    form = {**VALID, field: ""}
    result = parse(form)
    assert not result.ok
    assert any(message in e for e in result.errors)


def test_a_malformed_email_is_rejected():
    assert not parse({**VALID, "email": "not-an-email"}).ok


def test_a_non_number_is_an_error_not_a_crash():
    result = parse({**VALID, "current_ctc_lpa": "lots"})
    assert any("Current CTC" in e for e in result.errors)


def test_a_job_needs_company_and_title():
    result = parse({**VALID, "exp-0-title": ""})
    assert any("Job 1" in e for e in result.errors)


def test_no_history_at_all_is_rejected():
    form = {k: v for k, v in VALID.items() if not k.startswith(("exp-", "edu-"))}
    assert any("job or one degree" in e for e in parse(form).errors)


def test_fields_the_form_does_not_show_are_preserved():
    """Saving the page must not erase what was set another way."""
    base = Profile(answers=ApplicationAnswers(flag_companies=["Acme"], earliest_start="1 month"))
    p = parse(VALID, base).profile
    assert p.answers.flag_companies == ["Acme"]
    assert p.answers.earliest_start == "1 month"


def test_split_lines_handles_numbered_lists():
    assert split_lines("1. one\n2) two\n  * three") == ["one", "two", "three"]


# --------------------------------------------------------------------------
# Job filters
# --------------------------------------------------------------------------


FILTERS = {
    "t_india_min_ctc_lpa": "20",
    "t_us_remote_min_usd": "90000",
    "t_direct_titles": "Software Engineer\nBackend Engineer",
    "t_reject_titles": "Staff\nManager",
    "t_max_years_required": "4",
    "t_apply_threshold": "3.5",
    "t_autonomy_present": "1",
    "t_shadow_mode": "on",
    "t_applications_per_day": "10",
    "t_schedule_present": "1",
    "t_schedule_enabled": "on",
    "t_morning": "8:05",
    "t_evening": "19:00",
    "t_timezone": "Europe/London",
}


def test_filters_parse_into_a_targets_override():
    errors: list[str] = []
    out = parse_targets(FILTERS, errors)
    assert errors == []
    assert out["compensation"] == {"india_min_ctc_lpa": 20.0, "us_remote_min_usd": 90000}
    assert out["level"]["direct_titles"] == ["software engineer", "backend engineer"]
    assert out["level"]["max_years_required"] == 4
    assert out["scoring"] == {"apply_threshold": 3.5}
    assert out["autonomy"]["shadow_mode"] is True
    assert out["autonomy"]["auto_submit_clean_ats"] is False
    assert out["schedule"] == {
        "enabled": True, "morning": "08:05", "evening": "19:00", "timezone": "Europe/London"
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("t_apply_threshold", "7", "between 1 and 5"),
        ("t_morning", "25:00", "Morning run time"),
        ("t_timezone", "Mars/Olympus", "not recognised"),
        ("t_direct_titles", "", "at least one target job title"),
        ("t_applications_per_day", "-1", "cannot be negative"),
    ],
)
def test_bad_filter_values_are_reported(field, value, message):
    errors: list[str] = []
    parse_targets({**FILTERS, field: value}, errors)
    assert any(message in e for e in errors), errors


def test_unchecked_boxes_mean_off_only_when_their_section_was_submitted():
    """A missing checkbox is "off" - but only if the form actually had it."""
    errors: list[str] = []
    out = parse_targets({"t_apply_threshold": "4"}, errors)
    assert "autonomy" not in out and "schedule" not in out


# --------------------------------------------------------------------------
# Resume import
# --------------------------------------------------------------------------


def test_a_resume_import_keeps_your_preferences():
    base = parse(VALID).profile
    parsed = Profile(experience=[Experience(company="NewCo", title="SDE")])
    parsed.identity.phone = "+91 90000 00002"
    merged = merge_parsed(parsed, base)
    assert [e.company for e in merged.experience] == ["NewCo"]
    assert merged.identity.phone == "+91 90000 00002"
    assert merged.identity.name == "Asha Rao", "blank parsed fields must not erase"
    assert merged.preferences.current_ctc_lpa == 12.5


@pytest.mark.parametrize(
    ("name", "expected"),
    [("Asha Rao", "AR"), ("Asha", "AS"), ("  ", "?"), ("Asha K Rao", "AR")],
)
def test_initials(name, expected):
    assert initials(name) == expected


# --------------------------------------------------------------------------
# Link text is not a link
# --------------------------------------------------------------------------


def test_a_resume_import_never_replaces_a_url_with_link_text():
    """Resumes show "GitHub | LinkedIn" as clickable words; the words are not URLs.

    Found on a real import: the draft replaced linkedin.com/in/... with the
    literal word "LinkedIn", which every application form would then have
    received.
    """
    base = parse(VALID).profile
    base.identity.linkedin = "linkedin.com/in/asha-rao"
    base.identity.github = "github.com/asha"
    parsed = Profile()
    parsed.identity.linkedin = "LinkedIn"
    parsed.identity.github = "GitHub"
    merged = merge_parsed(parsed, base)
    assert merged.identity.linkedin == "linkedin.com/in/asha-rao"
    assert merged.identity.github == "github.com/asha"


def test_link_targets_in_the_pdf_fill_the_links():
    from jobhunt.resume.extract import apply_links

    p = Profile()
    p.identity.linkedin, p.identity.github = "LinkedIn", "GitHub"
    apply_links(p, ["tel:+919000000000", "mailto:a@example.com",
                    "https://github.com/asha", "https://www.linkedin.com/in/asha/"])
    assert p.identity.github == "https://github.com/asha"
    assert p.identity.linkedin == "https://www.linkedin.com/in/asha/"
    assert p.identity.email == "a@example.com"
    assert p.identity.phone == "+919000000000"


def test_link_text_without_a_target_is_dropped_not_kept():
    from jobhunt.resume.extract import apply_links

    p = Profile()
    p.identity.github = "GitHub"
    assert apply_links(p, []).identity.github == ""


def test_saving_link_text_as_a_url_is_refused():
    result = parse({**VALID, "linkedin": "LinkedIn"})
    assert any("LinkedIn" in e and "not a link" in e for e in result.errors)


def test_jobs_scored_per_run_is_a_setting():
    errors: list[str] = []
    out = parse_targets({"t_apply_threshold": "3.7", "t_score_per_run": "120"}, errors)
    assert errors == [] and out["scoring"] == {"apply_threshold": 3.7, "per_run": 120}
    errors = []
    parse_targets({"t_score_per_run": "5000"}, errors)
    assert any("between 5 and 500" in e for e in errors)
