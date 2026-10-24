"""Form parsing for the onboarding pages.

Two forms, stored in two places:

* **Profile** (``<data>/profile/profile.yaml``) - who you are, your work
  history, and the standing answers application forms keep asking for;
* **Job filters** (``<data>/profile/targets.yaml``) - pay floors, the titles
  to target, how much autonomy the agents get, and when they run.

Parsing is kept here, away from the routes, so it can be tested without a
browser: a form that silently drops a field is exactly the kind of bug that
only shows up as a wrong answer on a real application.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from jobhunt.resume.profile import Education, Experience, Profile, Project

# Offered in the timezone picker. Anything valid is accepted if typed.
COMMON_TIMEZONES = [
    "Asia/Kolkata", "Asia/Singapore", "Asia/Dubai", "Asia/Tokyo", "Europe/London",
    "Europe/Berlin", "Europe/Amsterdam", "America/New_York", "America/Chicago",
    "America/Denver", "America/Los_Angeles", "America/Toronto", "Australia/Sydney", "UTC",
]

# Suggestions for the voluntary US EEO questions. Free text is still allowed:
# forms word these differently, and the Applicant matches on meaning.
EEO_SUGGESTIONS = {
    "gender": ["Male", "Female", "Non-binary", "Decline to self-identify"],
    "race_ethnicity": [
        "Asian", "Black or African American", "Hispanic or Latino", "White",
        "Two or more races", "Decline to self-identify",
    ],
    "hispanic_latino": ["No", "Yes", "Decline to self-identify"],
    "veteran_status": ["I am not a protected veteran", "Decline to self-identify"],
    "disability_status": ["No, I do not have a disability", "Decline to self-identify"],
}

_LIST_KEY = re.compile(r"^(edu|exp|proj)-(\d+)-([a-z_]+)$")
_BULLET_MARK = re.compile(r"^\s*(?:[-*•·▪◦]|\d+[.)])\s*")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class SetupResult:
    profile: Profile
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# --------------------------------------------------------------------------
# small parsers
# --------------------------------------------------------------------------


def _text(form: Mapping[str, Any], key: str) -> str:
    value = form.get(key)
    return str(value).strip() if value is not None else ""


def _checked(form: Mapping[str, Any], key: str) -> bool:
    return _text(form, key).lower() in ("on", "true", "1", "yes")


def split_commas(value: str) -> list[str]:
    return list(dict.fromkeys(x.strip() for x in (value or "").split(",") if x.strip()))


def split_lines(value: str) -> list[str]:
    """One entry per line, with any bullet marker a paste brought along removed."""
    out = []
    for line in (value or "").splitlines():
        line = _BULLET_MARK.sub("", line).strip()
        if line:
            out.append(line)
    return out


def _number(
    form: Mapping[str, Any], key: str, label: str, errors: list[str], *, integer: bool = False
) -> float | int | None:
    raw = _text(form, key).replace(",", "")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        errors.append(f"{label}: {raw!r} is not a number")
        return None
    if value < 0:
        errors.append(f"{label} cannot be negative")
        return None
    return int(value) if integer else value


def _rows(form: Mapping[str, Any], prefix: str) -> list[dict[str, str]]:
    """Collect ``prefix-<n>-<field>`` inputs into ordered rows."""
    rows: dict[int, dict[str, str]] = {}
    for key in form:
        m = _LIST_KEY.match(key)
        if m and m.group(1) == prefix:
            rows.setdefault(int(m.group(2)), {})[m.group(3)] = _text(form, key)
    return [rows[i] for i in sorted(rows) if any(rows[i].values())]


# --------------------------------------------------------------------------
# the form -> profile + settings
# --------------------------------------------------------------------------


def parse(form: Mapping[str, Any], base: Profile | None = None) -> SetupResult:
    """Build a profile and settings overrides from the submitted form.

    ``base`` supplies everything the form does not show, so saving the page
    never erases a field that was set some other way (the CLI, or by hand).
    """
    profile = (base or Profile()).model_copy(deep=True)
    errors: list[str] = []

    # ---- about you -------------------------------------------------------
    ident = profile.identity
    ident.name = _text(form, "name")
    ident.email = _text(form, "email")
    ident.phone = _text(form, "phone")
    ident.location = _text(form, "location")
    ident.linkedin = _text(form, "linkedin")
    ident.github = _text(form, "github")
    ident.portfolio = _text(form, "portfolio")

    if not ident.name:
        errors.append("Full name is required")
    if not ident.email:
        errors.append("Email is required")
    elif not _EMAIL.match(ident.email):
        errors.append(f"{ident.email!r} does not look like an email address")
    if not ident.phone:
        errors.append("Phone is required - almost every application form asks for it")
    if not ident.location:
        errors.append("Current location is required (e.g. 'Pune, India')")
    from jobhunt.resume.extract import looks_like_url

    for key, label, example in (
        ("linkedin", "LinkedIn", "linkedin.com/in/your-name"),
        ("github", "GitHub", "github.com/your-name"),
        ("portfolio", "Portfolio", "your-site.com"),
    ):
        value = getattr(ident, key)
        if value and not looks_like_url(value):
            errors.append(f"{label}: {value!r} is not a link - paste the full URL, e.g. {example}")

    # ---- work and pay ----------------------------------------------------
    prefs = profile.preferences
    prefs.years_experience = _number(form, "years_experience", "Years of experience", errors)
    prefs.current_ctc_lpa = _number(form, "current_ctc_lpa", "Current CTC", errors)
    prefs.expected_min_ctc_lpa = _number(form, "expected_min_ctc_lpa", "Expected CTC", errors)
    prefs.notice_period_days = _number(
        form, "notice_period_days", "Notice period", errors, integer=True
    )
    prefs.preferred_locations = split_commas(_text(form, "preferred_locations"))
    prefs.open_to_remote = _checked(form, "open_to_remote")
    prefs.willing_to_relocate = _checked(form, "willing_to_relocate")
    prefs.work_authorization = _text(form, "work_authorization")

    # ---- standing application answers ------------------------------------
    a = profile.answers
    # The same facts are read from both places by different agents; one input
    # keeps them from drifting apart.
    a.preferred_locations = list(prefs.preferred_locations)
    a.open_to_remote = prefs.open_to_remote
    a.willing_to_relocate_domestic = prefs.willing_to_relocate
    a.notice_period_days = prefs.notice_period_days
    a.expected_ctc_inr_lpa = prefs.expected_min_ctc_lpa
    a.work_auth_statement = prefs.work_authorization

    a.citizenship = _text(form, "citizenship")
    a.needs_sponsorship_india = _checked(form, "needs_sponsorship_india")
    a.needs_sponsorship_abroad = _checked(form, "needs_sponsorship_abroad")
    a.willing_to_relocate_international = _checked(form, "willing_to_relocate_international")
    a.can_work_hybrid = _checked(form, "can_work_hybrid")
    a.consent_to_sms = _checked(form, "consent_to_sms")
    a.how_did_you_hear = _text(form, "how_did_you_hear")
    a.pronouns = _text(form, "pronouns")
    for key in EEO_SUGGESTIONS:
        setattr(a, key, _text(form, key))
    if not a.citizenship:
        errors.append("Citizenship is required - work-authorisation questions depend on it")

    # ---- history ---------------------------------------------------------
    profile.experience = [
        Experience(
            company=r.get("company", ""),
            title=r.get("title", ""),
            location=r.get("location", ""),
            start=r.get("start", ""),
            end=r.get("end", ""),
            bullets=split_lines(r.get("bullets", "")),
            tech=split_commas(r.get("tech", "")),
        )
        for r in _rows(form, "exp")
    ]
    profile.education = [
        Education(
            institution=r.get("institution", ""),
            degree=r.get("degree", ""),
            field=r.get("field", ""),
            start=r.get("start", ""),
            end=r.get("end", ""),
            grade=r.get("grade", ""),
        )
        for r in _rows(form, "edu")
    ]
    profile.projects = [
        Project(
            name=r.get("name", ""),
            url=r.get("url", ""),
            tech=split_commas(r.get("tech", "")),
            bullets=split_lines(r.get("bullets", "")),
        )
        for r in _rows(form, "proj")
    ]
    for i, e in enumerate(profile.experience, 1):
        if not (e.company and e.title):
            errors.append(f"Job {i}: company and title are both required")
    for i, e in enumerate(profile.education, 1):
        if not e.institution:
            errors.append(f"Education {i}: the institution is required")
    for i, p in enumerate(profile.projects, 1):
        if not p.name:
            errors.append(f"Project {i}: the name is required")

    s = profile.skills
    s.languages = split_commas(_text(form, "skills_languages"))
    s.frameworks = split_commas(_text(form, "skills_frameworks"))
    s.tools = split_commas(_text(form, "skills_tools"))
    s.domains = split_commas(_text(form, "skills_domains"))
    profile.achievements = split_lines(_text(form, "achievements"))

    if not profile.experience and not profile.education:
        errors.append("Add at least one job or one degree")
    if not profile.all_skills():
        errors.append("Add at least one skill - scoring and tailoring match on them")

    return SetupResult(profile=profile, errors=errors)


def parse_targets(form: Mapping[str, Any], errors: list[str]) -> dict[str, Any]:
    """The Job filters form, as a targets.yaml override."""
    out: dict[str, Any] = {}

    comp: dict[str, Any] = {}
    if (v := _number(form, "t_india_min_ctc_lpa", "India pay floor", errors)) is not None:
        comp["india_min_ctc_lpa"] = v
    if (v := _number(form, "t_us_remote_min_usd", "US/remote pay floor", errors)) is not None:
        comp["us_remote_min_usd"] = int(v)
    if comp:
        out["compensation"] = comp

    level: dict[str, Any] = {}
    if (v := _number(form, "t_max_years_required", "Max years", errors, integer=True)) is not None:
        level["max_years_required"] = v
    if (v := _number(form, "t_max_level_number", "Max level", errors, integer=True)) is not None:
        level["max_level_number"] = v
    for key in ("direct_titles", "stretch_titles", "ai_track_titles", "reject_titles"):
        if f"t_{key}" in form:
            level[key] = [t.lower() for t in split_lines(_text(form, f"t_{key}"))]
    if level:
        out["level"] = level
    if not level.get("direct_titles") and "t_direct_titles" in form:
        errors.append("List at least one target job title")

    threshold = _number(form, "t_apply_threshold", "Score threshold", errors)
    if threshold is not None:
        if not 1 <= threshold <= 5:
            errors.append("Score threshold must be between 1 and 5")
        else:
            out.setdefault("scoring", {})["apply_threshold"] = threshold
    per_run = _number(form, "t_score_per_run", "Jobs scored per run", errors, integer=True)
    if per_run is not None:
        if not 5 <= per_run <= 500:
            errors.append("Jobs scored per run must be between 5 and 500")
        else:
            out.setdefault("scoring", {})["per_run"] = per_run

    if "t_autonomy_present" in form:
        out["autonomy"] = {
            "shadow_mode": _checked(form, "t_shadow_mode"),
            "auto_submit_clean_ats": _checked(form, "t_auto_submit_clean_ats"),
            "write_cover_letter": _checked(form, "t_write_cover_letter"),
        }

    limits: dict[str, Any] = {}
    for key, label in (
        ("applications_per_day", "Applications per day"),
        ("applications_per_company", "Applications per company"),
        ("cold_emails_per_day", "Cold emails per day"),
    ):
        if (v := _number(form, f"t_{key}", label, errors, integer=True)) is not None:
            limits[key] = v
    if limits:
        out["limits"] = limits

    if "t_sources_present" in form:
        out["sources"] = {
            "linkedin": {
                "enabled": _checked(form, "t_linkedin_enabled"),
                "keywords": split_lines(_text(form, "t_linkedin_keywords")),
                "watch_companies": split_lines(_text(form, "t_watch_companies")),
            },
            "wellfound": {"enabled": _checked(form, "t_wellfound_enabled")},
        }
        out["skip_companies"] = split_lines(_text(form, "t_skip_companies"))

    if "t_resume_mode" in form:
        mode = _text(form, "t_resume_mode")
        if mode not in ("tailored", "original"):
            errors.append("Choose how your resume is sent")
        else:
            out["resume"] = {"mode": mode}

    if "t_schedule_present" in form:
        from jobhunt.graph.scheduler import parse_hhmm

        schedule: dict[str, Any] = {"enabled": _checked(form, "t_schedule_enabled")}
        for key, label in (("morning", "Morning run time"), ("evening", "Evening run time")):
            raw = _text(form, f"t_{key}")
            if not raw:
                continue
            try:
                h, m = parse_hhmm(raw)
                schedule[key] = f"{h:02d}:{m:02d}"
            except ValueError:
                errors.append(f"{label}: {raw!r} is not a time like 09:00")
        tz = _text(form, "t_timezone")
        if tz:
            try:
                ZoneInfo(tz)
                schedule["timezone"] = tz
            except Exception:  # noqa: BLE001 - any failure means "not a zone"
                errors.append(f"Timezone {tz!r} is not recognised (e.g. Asia/Kolkata)")
        out["schedule"] = schedule

    return out


# --------------------------------------------------------------------------
# resume import
# --------------------------------------------------------------------------


def merge_parsed(parsed: Profile, base: Profile | None) -> Profile:
    """Take the resume's content, keep everything a resume cannot contain.

    Work history, education, projects, skills and achievements come from the
    newly parsed resume. Preferences and standing answers are yours and stay.
    Contact details from the resume win where it has them.
    """
    if base is None:
        return parsed
    from jobhunt.resume.extract import looks_like_url

    merged = base.model_copy(deep=True)
    for key, value in parsed.identity.model_dump().items():
        if not value:
            continue
        # Never replace a real link with link text ("GitHub", "LinkedIn").
        if key in ("linkedin", "github", "portfolio") and not looks_like_url(value):
            continue
        setattr(merged.identity, key, value)
    merged.education = parsed.education
    merged.experience = parsed.experience
    merged.projects = parsed.projects
    merged.skills = parsed.skills
    merged.achievements = parsed.achievements
    return merged


def initials(name: str) -> str:
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()
