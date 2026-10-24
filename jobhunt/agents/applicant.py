"""Applicant - fill an ATS application form and, when it is safe to, submit it.

The autonomy rules from ``config/targets.yaml`` are enforced here, and the
default answer is "queue it for a human". A form is only submitted
automatically when three things hold:

* ``shadow_mode`` is off,
* the ATS is one whose form shape is understood (Greenhouse, Ashby, Lever),
* and nothing on the form was left unanswered.

Free-text questions are answered by the Writer, grounded in the profile and
the posting and fact-checked the same way the resume is. An answer that fails
that check is *not* submitted - it is left for you, because an unverifiable
answer has to be defended in an interview.

Anything else is filled in, screenshotted, and left for you. A badly
auto-answered application is not a neutral outcome: it burns the company.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlmodel import select

from jobhunt.agents import writer
from jobhunt.agents.selection import over_company_cap
from jobhunt.config import settings, targets
from jobhunt.resume.profile import Profile
from jobhunt.resume.render import resume_staleness
from jobhunt.sources.browser.session import BrowserSession, browser
from jobhunt.store.db import consume_quota, remaining_quota, session
from jobhunt.store.models import Application, Job, JobStatus, Resume

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page

log = logging.getLogger(__name__)



def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40]

# Free-text prompts that need a written answer rather than a profile lookup.
# These go to the Writer; only the ones it cannot verifiably answer reach the
# queue.
_ESSAY_MARKERS = re.compile(
    r"why (do you|are you|this|our)|tell us|describe|in your own words|"
    r"cover letter|what interests|motivat|how would you|explain|"
    r"what excites|elaborat",
    re.IGNORECASE,
)

# Questions with a factual answer the profile already contains.
#
# ORDER MATTERS. This is matched by substring and the first hit wins, so the
# most specific phrasing has to come first. "CGPA in Under graduation Degree"
# contains "degree", and answering it with "Bachelor of Technology" is how a
# grade field ends up holding a degree name.
_KNOWN_QUESTIONS = {
    # --- grade, before anything containing "degree" ---
    "cgpa": "grade",
    "gpa": "grade",
    "percentage in": "grade",
    # --- year, before the looser education keys ---
    "graduation completion year": "graduation_year",
    "graduation year": "graduation_year",
    "year of graduation": "graduation_year",
    "passing year": "graduation_year",
    # --- money, before the bare "ctc"/"salary" forms ---
    "current annual ctc": "current_ctc",
    "current ctc": "current_ctc",
    "current salary": "current_ctc",
    "expected annual ctc": "expected_ctc",
    "expected ctc": "expected_ctc",
    "expected salary": "expected_ctc",
    "salary expectation": "expected_ctc",
    "annual ctc": "expected_ctc",
    "notice": "notice_period",
    # --- experience ---
    "years of experience": "years_experience",
    "total years of work experience": "years_experience",
    "total experience": "years_experience",
    "work experience": "years_experience",
    # --- employer, before the bare "employer" ---
    "current/last company": "current_company",
    "current company": "current_company",
    "last company": "current_company",
    "present employer": "current_company",
    "current employer": "current_company",
    "previous employer": "current_company",
    "employer": "current_company",
    "current designation": "current_title",
    "current title": "current_title",
    "job title": "current_title",
    # --- links ---
    "linkedin": "linkedin",
    "github": "github",
    "portfolio": "portfolio",
    "website": "portfolio",
    # --- where ---
    "current location": "location",
    "location": "location",
    "country": "country",
    "relocate": "relocate",
    "authorized": "work_auth",
    "sponsorship": "work_auth",
    "visa": "work_auth",
    # --- education. Greenhouse ships a School/Degree block; Indian employers
    # ask the same thing as "College Name", "Graduation Branch", "CGPA".
    "college name": "school",
    "college": "school",
    "university": "school",
    "school": "school",
    "institute": "school",
    "graduation branch": "field_of_study",
    "field of study": "field_of_study",
    "specialization": "field_of_study",
    "branch": "field_of_study",
    "major": "field_of_study",
    "degree": "degree",
    "qualification": "degree",
}

FIELD_HINTS: dict[str, tuple[str, ...]] = {
    # Both spellings: the attribute name (first_name) and the visible label
    # ("First Name*"). Only the underscore forms were here, so a form whose
    # label reader returned the visible text matched nothing.
    "first_name": ("first_name", "firstname", "first name", "given name",
                   "given", "first-name"),
    "last_name": ("last_name", "lastname", "last name", "family name",
                  "surname", "last-name", "family"),
    # Deliberately not a bare "name". Every form has a dozen labels containing
    # it - "College Name", "Company Name", "please mention the name" - and the
    # bare hint put the candidate's full name into NK Securities' college free-text box.
    "full_name": ("full_name", "fullname", "full name", "your name",
                  "candidate name", "legal name"),
    "email": ("email", "e-mail"),
    "phone": ("phone", "mobile", "telephone"),
    "linkedin": ("linkedin",),
    "github": ("github",),
    "portfolio": ("portfolio", "website", "personal site"),
    "location": ("location", "city", "current city"),
}


@dataclass
class ApplyResult:
    job_id: int
    ats: str = "unknown"
    filled: dict[str, str] = field(default_factory=dict)
    unanswered: list[str] = field(default_factory=list)
    essay_questions: list[str] = field(default_factory=list)
    written_answers: dict[str, str] = field(default_factory=dict)
    # Questions answered by *not* ticking a box. The DOM cannot tell a
    # deliberate "No" from an untouched control, so the intent is recorded here
    # and subtracted from the unanswered audit.
    answered_questions: list[str] = field(default_factory=list)
    cover_letter: str | None = None
    submitted: bool = False
    queued_reason: str = ""
    screenshot: str | None = None
    error: str = ""


@dataclass
class ApplyStats:
    considered: int = 0
    submitted: int = 0
    queued: int = 0
    failed: int = 0
    quota_blocked: int = 0
    company_capped: int = 0

    def as_dict(self) -> dict:
        return {
            "considered": self.considered,
            "submitted": self.submitted,
            "queued": self.queued,
            "failed": self.failed,
            "quota_blocked": self.quota_blocked,
            "company_capped": self.company_capped,
        }


# --------------------------------------------------------------------------
# Autonomy policy
# --------------------------------------------------------------------------


# Job boards whose postings are applied to on the board itself, by you. The
# real "apply" link there needs a login, and LinkedIn prohibits automating
# its Easy Apply - so these are never form-filled, only handed to you.
MANUAL_APPLY = {"linkedin": "LinkedIn", "wellfound": "Wellfound"}


def manual_apply_site(job: Job) -> str | None:
    """The site you apply on yourself for this job, or None if we can fill it."""
    return MANUAL_APPLY.get((job.ats or "").lower())


def detect_ats(url: str, declared: str | None = None) -> str:
    if declared in ("greenhouse", "ashby", "lever", "workday", "amazon"):
        return declared
    u = (url or "").lower()
    for name, marker in (
        ("greenhouse", "greenhouse.io"),
        ("ashby", "ashbyhq.com"),
        ("lever", "lever.co"),
        ("workday", "myworkdayjobs.com"),
        ("amazon", "amazon.jobs"),
    ):
        if marker in u:
            return name
    return "unknown"


def may_autosubmit(
    ats: str, essays: list[str], unanswered: list[str], *, requested: bool = False
) -> tuple[bool, str]:
    """Decide whether this form may be submitted without a human.

    Returns ``(allowed, reason)``. The reason is recorded either way, so a
    queued application always explains itself.

    ``requested=True`` means you clicked Submit on this specific job. That
    stands in for shadow mode, which exists to stop *unattended* runs sending
    things he has not seen - a deliberate click on one posting is a stronger
    signal of intent than a global switch, and it is scoped to that posting.
    Every other gate still applies: an incomplete form is not submitted just
    because it was asked for.
    """
    cfg = targets().get("autonomy", {})

    if cfg.get("shadow_mode", True) and not requested:
        return False, "shadow mode - nothing is submitted"
    if essays:
        # Only questions the Writer could not answer verifiably land here.
        if not cfg.get("auto_submit_with_essays", False):
            return False, f"{len(essays)} free-text question(s) need your answer"
    if ats in ("greenhouse", "ashby", "lever"):
        if not cfg.get("auto_submit_clean_ats", True):
            return False, "auto-submit disabled for standard ATS"
    elif ats == "workday":
        if not cfg.get("auto_submit_workday", False):
            return False, "Workday forms are multi-step and account-gated"
    else:
        if not cfg.get("auto_submit_unknown_ats", False):
            return False, f"unrecognised ATS ({ats})"
    if unanswered:
        return False, f"{len(unanswered)} required field(s) could not be filled"
    return True, "clean form on a known ATS"


# --------------------------------------------------------------------------
# Form filling
# --------------------------------------------------------------------------


def answer_for(label: str, profile: Profile) -> str | None:
    """A factual answer from the profile, or None if it needs judgement."""
    low = label.lower()
    prefs = profile.preferences
    ident = profile.identity

    key = next((v for k, v in _KNOWN_QUESTIONS.items() if k in low), None)
    if key is None:
        return None

    current = profile.experience[0] if profile.experience else None

    # The most recent degree. Forms ask for one school, and that is the one.
    edu = profile.education[0] if profile.education else None

    return {
        "notice_period": str(prefs.notice_period_days or profile.answers.notice_period_days or ""),
        "current_company": current.company if current else "",
        "current_title": current.title if current else "",
        "relocate": "Yes" if prefs.willing_to_relocate else "Open to discuss",
        "current_ctc": str(prefs.current_ctc_lpa or ""),
        "expected_ctc": str(
            prefs.expected_min_ctc_lpa or profile.answers.expected_ctc_inr_lpa or ""
        ),
        # Derived from the employment dates rather than stored, so it cannot go
        # stale by sitting in a config file for a year. Falls back to a stored
        # value when there are no dates to count.
        "years_experience": (
            profile.years_of_experience() or str(prefs.years_experience or "")
        ),
        "linkedin": ident.linkedin,
        "github": ident.github,
        "portfolio": ident.portfolio,
        "location": ident.location,
        "country": (ident.location or "").split(",")[-1].strip(),
        "school": edu.institution if edu else "",
        "degree": edu.degree if edu else "",
        "field_of_study": edu.field if edu else "",
        # "CGPA: 8.3" is how it is stored for the resume; a form field wants
        # the number on its own.
        "grade": _numeric(edu.grade) if edu else "",
        "graduation_year": (edu.end or "") if edu else "",
        "work_auth": prefs.work_authorization,
    }.get(key) or None


# Each ATS puts the form somewhere different from the posting URL. Landing on
# the posting and looking for inputs finds none, which silently produced
# "filled: nothing" applications that looked like they had worked.
# Greenhouse embeds its board in the company's own careers site and passes the
# posting through as ?gh_jid=. The visible URL is then a JavaScript search page
# with no form on it - stripe.com/jobs/search?gh_jid=8062305 is a search page,
# not an application - so applying there fills nothing and reports nothing,
# which is exactly what it did. The form lives on Greenhouse's own host.
_GH_JID = re.compile(r"[?&]gh_jid=(\d+)", re.I)


def application_url(job: Job, ats: str) -> str:
    raw = job.url or ""
    url = raw.split("?")[0].rstrip("/")

    if ats == "greenhouse":
        match = _GH_JID.search(raw) or re.search(r"/jobs/(\d+)", raw)
        if match and job.ats_slug:
            # The bare form. Not `/{slug}/jobs/{id}` - that 302s back to the
            # company's own careers page, where the form is inside an iframe
            # and the top-level document has no inputs at all, which is how
            # "filled: nothing" happened twice on the same posting.
            return (
                "https://boards.greenhouse.io/embed/job_app"
                f"?for={job.ats_slug}&token={match.group(1)}"
            )

    if ats == "ashby" and not url.endswith("/application"):
        return f"{url}/application"
    if ats == "lever" and not url.endswith("/apply"):
        return f"{url}/apply"
    return raw or url


# Consent dialogs overlay the form and swallow clicks. Always take the
# privacy-preserving option, never "Accept All".
_CONSENT_BUTTONS = (
    "button:has-text('Necessary Only')",
    "button:has-text('Only necessary')",
    "button:has-text('Reject All')",
    "button:has-text('Decline')",
    "button:has-text('Essential only')",
    "#onetrust-reject-all-handler",
    "[aria-label='Reject all']",
)

# What a posting page shows instead of the form itself.
_APPLY_BUTTONS = (
    "a:has-text('Apply for this Job')",
    "button:has-text('Apply for this Job')",
    "a:has-text('Apply Now')",
    "button:has-text('Apply Now')",
    "[role=tab]:has-text('Application')",
    "a:has-text('Apply')",
    "button:has-text('Apply')",
)


def dismiss_consent(page: Page) -> str | None:
    """Close a cookie banner, declining anything non-essential."""
    for selector in _CONSENT_BUTTONS:
        try:
            button = page.query_selector(selector)
            if button and button.is_visible():
                button.click(timeout=4000)
                page.wait_for_timeout(700)
                return selector
        except Exception:  # noqa: BLE001 - a missing banner is the normal case
            continue
    return None


def open_application_form(page: Page) -> bool:
    """Ensure the form is on screen. Returns whether inputs are present."""
    if page.query_selector("input[type=file], input[type=email]"):
        return True
    for selector in _APPLY_BUTTONS:
        try:
            button = page.query_selector(selector)
            if button and button.is_visible():
                button.click(timeout=5000)
                page.wait_for_timeout(2500)
                if page.query_selector("input[type=file], input[type=email]"):
                    return True
        except Exception:  # noqa: BLE001
            continue
    return bool(page.query_selector("input[type=text], textarea"))


# Read the control's real <label>, not just its attributes. Ashby labels its
# inputs properly and uses placeholders for examples, so attribute-only lookup
# saw "hello@example.com" and "1-415-555-1234" and matched nothing.
_LABEL_JS = """el => {
  const byFor = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
  if (byFor && byFor.innerText.trim()) return byFor.innerText.trim();
  const wrapping = el.closest('label');
  if (wrapping && wrapping.innerText.trim()) return wrapping.innerText.trim();
  const described = el.getAttribute('aria-labelledby');
  if (described) {
    const node = document.getElementById(described);
    if (node && node.innerText.trim()) return node.innerText.trim();
  }
  // Ashby and Greenhouse both wrap each field in a container whose first
  // heading-ish child is the question.
  const field = el.closest('[class*="field"], [class*="Field"], fieldset, div');
  if (field) {
    const heading = field.querySelector('label, legend, [class*="label"], [class*="Label"]');
    if (heading && heading.innerText.trim()) return heading.innerText.trim();
  }
  return '';
}"""

# Controls that are never the applicant's to fill.
_IGNORED_FIELDS = re.compile(
    r"recaptcha|captcha|csrf|honeypot|^utm_|token|hidden", re.IGNORECASE
)

# Labels that ask for the name of something other than the applicant. A
# personal-detail hint must never answer one of these.
# The whole label is just "name" (with optional required marker or colon).
_BARE_NAME = re.compile(r"(full\s+)?name\s*[:*]?\s*", re.IGNORECASE)

# Fields that only apply when another answer was given a particular value.
_CONDITIONAL_FIELD = re.compile(r"\s*if\s+(you\s+selected|other|yes|no|any)\b", re.IGNORECASE)

# \b on "referr" matters: without it, "preferred first name" matches inside
# "p-referr-ed" and the applicant's own preferred name is refused.
_NAMES_SOMEONE_ELSE = re.compile(
    r"\bcollege|\bschool|\buniversity|\binstitut|\bcompany|\bemployer|"
    r"\borgani[sz]ation|\bmanager|\breferr|\brecruiter|\bsupervisor|"
    r"\breference|\bcontact person|\bemergency|\bspouse|\bguardian|\bparent",
    re.IGNORECASE,
)


# Recurring multiple-choice questions, mapped to the stored answers. The value
# is a callable so the answer can depend on the posting - sponsorship is needed
# abroad but not in India, and a blanket answer is wrong in one direction.
CHOICE_QUESTIONS: list[tuple[re.Pattern[str], str]] = [
    # MUST come before the sponsorship row. The two questions share every
    # keyword - "authorized", "work", the country - but want opposite answers:
    # A candidate who requires sponsorship for a US role is *not* already
    # authorised to work there. Matching the sponsorship row first would answer
    # "yes, I am authorised to work in the United States", which is false.
    (
        re.compile(
            r"(?:legally\s+)?authoriz(?:ed|ation)\s+to\s+work"
            r"|eligible\s+to\s+work"
            r"|permitted\s+to\s+work"
            r"|have\s+the\s+right\s+to\s+work",
            re.I,
        ),
        "work_authorised",
    ),
    # Citizenship is a separate fact from sponsorship: a green-card holder
    # needs no sponsorship and is still not a citizen. Asked directly, it gets
    # a direct answer rather than one inferred from the sponsorship value.
    (
        re.compile(
            r"\bcitizen(?:ship)?\b|permanent resident|green\s?card|\bnationals?\b",
            re.I,
        ),
        "citizenship",
    ),
    # Deliberately no bare "visa": "Are you currently on a visa?" is a
    # different question from "will you need one sponsored", and answering it
    # from the sponsorship value returned Yes for someone sitting in India on
    # no visa at all. Sponsorship needs sponsorship-shaped wording.
    (
        re.compile(
            r"sponsor|immigration|work permit|employment pass"
            r"|require.{0,25}visa|visa.{0,15}(?:support|status|required)",
            re.I,
        ),
        "sponsorship",
    ),
    (re.compile(r"previously (?:been )?employed|worked (?:here|at|for) (?:us|this)", re.I), "no"),
    (re.compile(r"family|relative|friend.{0,20}employed|referred by an employee", re.I), "no"),
    (re.compile(r"\bgender\b", re.I), "gender"),
    (re.compile(r"hispanic|latino", re.I), "hispanic"),
    (re.compile(r"race|ethnic", re.I), "ethnicity"),
    (re.compile(r"veteran", re.I), "veteran"),
    (re.compile(r"disabilit", re.I), "disability"),
    (re.compile(r"relocat", re.I), "relocate"),
    (re.compile(r"office|onsite|on-site|hybrid|days per week|in person", re.I), "hybrid"),
    (re.compile(r"background check|consent to.{0,20}(check|screening)", re.I), "yes"),
    (re.compile(r"text message|sms|communication consent", re.I), "sms"),
    (re.compile(r"how did you hear|how.{0,15}find (?:out about )?(?:us|this)", re.I), "heard"),
    (re.compile(r"pronoun", re.I), "pronouns"),
]

# Option text that means "prefer not to say", used as a fallback.
_DECLINE = re.compile(
    r"decline|prefer not|don'?t wish|do not wish|not disclose|choose not", re.I
)


def _office_answer(location: str, profile: Profile) -> str | None:
    """Yes/No for "can you work from this office", or None to leave it alone."""
    can = reachable(location, profile)
    if can is None:
        return None
    if not can:
        # "No" here usually withdraws the application, so it is his call, not
        # the agent's. Left unanswered, it surfaces in the audit and blocks an
        # auto-submit - which is the right outcome.
        return None
    return "Yes" if profile.answers.can_work_hybrid else "No"


def _numeric(text: str) -> str:
    """The number out of a stored label: "CPI: 8.3" -> "8.3"."""
    match = re.search(r"\d+(?:\.\d+)?", text or "")
    return match.group(0) if match else ""


def reachable(location: str, profile: Profile) -> bool | None:
    """Could the candidate actually show up at this office?

    ``None`` means "cannot tell from the posting" - the caller must not answer.

    This exists because "Are you able to work from the office associated with
    this role?" was being answered Yes from `can_work_hybrid`, which is about
    hybrid working in general, not about a specific office. On a San Francisco
    role that is a false statement on a job application, which is exactly the
    class of thing the rest of this codebase refuses to do.
    """
    if not location:
        return None
    low = location.lower()
    a = profile.answers

    if any(c.lower() in low for c in a.preferred_locations):
        return True
    if "remote" in low or "anywhere" in low:
        return True
    if "india" in low:
        return bool(a.willing_to_relocate_domestic)
    # A named place that is not in India and not one he listed.
    if a.willing_to_relocate_international:
        return True
    return False


# A work-authorisation question usually means the country the job is in, but
# not always: an India-based posting from a US company still asks "are you a
# U.S. citizen?". Judged by the posting alone that answers Yes, which is false.
# When the question names a country, that country wins.
_COUNTRY_IN_QUESTION: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"united states|u\.s\.|\busa\b|\bamerica\b|green\s?card|h-?1b", re.I),
     "United States"),
    (re.compile(r"\bindia\b", re.I), "India"),
    (re.compile(r"united kingdom|\bu\.k\.|\bbritain\b", re.I), "United Kingdom"),
    (re.compile(r"\bcanada\b|\bcanadian\b", re.I), "Canada"),
    (re.compile(r"european union|\bEEA\b|\bSchengen\b", re.I), "European Union"),
    (re.compile(r"\bsingapore\b", re.I), "Singapore"),
    (re.compile(r"\baustralia\b", re.I), "Australia"),
    (re.compile(r"\bgermany\b|\bireland\b|\bnetherlands\b|\bfrance\b", re.I), "Europe"),
]


def country_context(question: str, location: str) -> str:
    """Which country a work-authorisation question is really about."""
    for pattern, name in _COUNTRY_IN_QUESTION:
        if pattern.search(question or ""):
            return name
    return location


def choose_option(label: str, options: list[str], profile: Profile, location: str = "") -> str | None:
    """Pick the option text answering this question, or None to leave it alone.

    Matching is done on the option's own wording rather than a fixed value,
    because every form words the same choice differently - "I am not a
    protected veteran", "Not a veteran", "No".
    """
    a = profile.answers
    key = next((k for pattern, k in CHOICE_QUESTIONS if pattern.search(label)), None)
    if key is None:
        return None

    wanted: str | None = {
        # These three are judged against the country the question names, not
        # the one the job is in - they are different often enough to matter.
        "sponsorship": a.sponsorship_answer(country_context(label, location)),
        "work_authorised": a.work_authorised_answer(country_context(label, location)),
        "citizenship": a.citizenship_answer(country_context(label, location)),
        "no": "No",
        "yes": "Yes",
        "gender": a.gender,
        "hispanic": a.hispanic_latino,
        "ethnicity": a.race_ethnicity,
        "veteran": a.veteran_status,
        "disability": a.disability_status,
        "relocate": "Yes" if a.willing_to_relocate_domestic else "No",
        # Only answerable when the office is somewhere he could actually be.
        "hybrid": _office_answer(location, profile),
        "sms": "Yes" if a.consent_to_sms else "No",
        "heard": a.how_did_you_hear,
        "pronouns": a.pronouns,
    }.get(key)

    if not wanted:
        return None

    # Forms word the same choice differently: Male/Man, Female/Woman.
    synonyms = {
        "male": ("male", "man"),
        "female": ("female", "woman"),
        "yes": ("yes", "true"),
        "no": ("no", "false"),
    }

    lowered = [o.lower().strip() for o in options]
    target = wanted.lower().strip()
    targets = synonyms.get(target, (target,))

    for want in targets:
        for i, opt in enumerate(lowered):
            if opt == want:
                return options[i]

    for want in targets:
        for i, opt in enumerate(lowered):
            if want in opt or opt in want:
                return options[i]
    # Yes/No questions where the option is phrased as a sentence.
    if target in ("yes", "no"):
        for i, opt in enumerate(lowered):
            if re.match(rf"^{target}\b", opt):
                return options[i]
    # EEO questions: declining is a legitimate answer when nothing matched.
    if key in ("gender", "ethnicity", "veteran", "disability", "hispanic"):
        for opt in options:
            if _DECLINE.search(opt):
                return opt
    return None


def _describe(element: Any) -> str:
    """A human-readable label for a form control.

    Prefers the associated <label>; falls back to attributes. The placeholder
    comes last because it usually holds an *example* value rather than the
    question.
    """
    try:
        label = element.evaluate(_LABEL_JS)
        if label and label.strip():
            return " ".join(label.split())[:160]
    except Exception:  # noqa: BLE001 - detached node or no JS context
        pass
    for attr in ("aria-label", "name", "id", "placeholder"):
        try:
            if value := element.get_attribute(attr):
                return value
        except Exception:  # noqa: BLE001
            continue
    return ""


def _is_fillable(element: Any) -> bool:
    """Skip hidden, disabled and machinery fields."""
    try:
        if not element.is_visible() or not element.is_enabled():
            return False
        for attr in ("name", "id"):
            if (value := element.get_attribute(attr)) and _IGNORED_FIELDS.search(value):
                return False
    except Exception:  # noqa: BLE001
        return False
    return True


def fill_form(
    page: Page,
    profile: Profile,
    resume_path: Path | None,
    job: Job | None = None,
    cover_letter_path: Path | None = None,
    location: str = "",
) -> ApplyResult:
    """Fill everything answerable, including free text. Never clicks submit."""
    result = ApplyResult(job_id=0)
    ident = profile.identity
    name_parts = (ident.name or "").split()

    values = {
        "first_name": name_parts[0] if name_parts else "",
        "last_name": " ".join(name_parts[1:]) if len(name_parts) > 1 else "",
        "full_name": ident.name,
        "email": ident.email,
        "phone": ident.phone,
        "linkedin": ident.linkedin,
        "github": ident.github,
        "portfolio": ident.portfolio,
        "location": ident.location,
    }

    for element in page.query_selector_all(
        "input[type=text], input[type=email], input[type=tel], input[type=url], input:not([type])"
    ):
        if not _is_fillable(element):
            continue
        label = _describe(element).lower()
        if not label:
            continue
        # A field that only applies if another answer was given. Filling
        # "If you selected 'Others' in the college list, please mention the
        # name" when the college list was answered normally puts a stray value
        # on the form.
        if _CONDITIONAL_FIELD.match(label):
            continue
        # A label that is *only* the word "name" is the applicant's own - Ashby
        # labels its field exactly that. It is excluded from the hint list
        # because as a substring it also matches "College Name", so it is
        # matched here on the whole label instead.
        if _BARE_NAME.fullmatch(label.strip()):
            value = values.get("full_name") or ""
            if value:
                try:
                    element.fill(value)
                    result.filled[label[:40]] = value[:60]
                except Exception as exc:  # noqa: BLE001
                    log.debug("could not fill %s: %s", label[:40], exc)
            continue
        # The profile's specific answers win over the generic hints: "Current
        # Annual CTC" and "College Name" both have exact mappings, and a hint
        # match on a stray word would answer them with something else.
        value = answer_for(label, profile)
        if not value and not _NAMES_SOMEONE_ELSE.search(label):
            target = next(
                (field for field, hints in FIELD_HINTS.items() if any(h in label for h in hints)),
                None,
            )
            value = values.get(target or "", "") if target else None
        if not value:
            continue
        try:
            element.fill(value)
            result.filled[label[:40]] = value[:60]
        except Exception as exc:  # noqa: BLE001
            log.debug("could not fill %s: %s", label[:40], exc)

    # File uploads. Forms label their inputs, so the cover letter goes to the
    # cover-letter field rather than overwriting the resume.
    uploaders = page.query_selector_all("input[type=file]")
    used: set[int] = set()

    def _upload(path: Path, hints: tuple[str, ...], label: str) -> None:
        for i, uploader in enumerate(uploaders):
            if i in used:
                continue
            described = _describe(uploader).lower()
            if hints and not any(h in described for h in hints):
                continue
            try:
                uploader.set_input_files(str(path))
                result.filled[label] = path.name
                used.add(i)
                return
            except Exception as exc:  # noqa: BLE001
                log.debug("%s upload failed: %s", label, exc)

    if resume_path and resume_path.exists():
        _upload(resume_path, ("resume", "cv"), "resume")
        if "resume" not in result.filled:
            _upload(resume_path, (), "resume")   # unlabelled single input
    if cover_letter_path and cover_letter_path.exists():
        _upload(cover_letter_path, ("cover", "letter"), "cover_letter")

    # Free-text questions. Factual ones come straight from the profile;
    # judgement ones go to the Writer, which grounds its answer in the profile
    # and the posting and is fact-checked the same way the resume is.
    for area in page.query_selector_all("textarea"):
        if not _is_fillable(area):
            continue
        label = _describe(area)
        if not label or _IGNORED_FIELDS.search(label):
            continue

        answer = answer_for(label, profile)
        if answer is None and job is not None and _ESSAY_MARKERS.search(label):
            written = writer.answer_question(profile, job, label)
            if written.ok:
                answer = written.text
                result.written_answers[label[:80]] = written.text
            else:
                # An unverifiable answer is worse than none: leave it for the
                # human rather than submit something that cannot be defended.
                result.essay_questions.append(label[:140])
                if written.violations:
                    log.warning(
                        "unverifiable answer for %r: %s", label[:50], written.violations[:3]
                    )
                continue

        if answer:
            try:
                area.fill(answer)
                result.filled[label[:40]] = answer[:60]
            except Exception:  # noqa: BLE001
                pass
        else:
            result.essay_questions.append(label[:140])

    fill_choices(page, profile, result, location=location)
    fill_comboboxes(page, profile, result)
    result.unanswered = unanswered_required(page)
    return result


# Ashby renders Location and "How did you hear about us?" as typeaheads: an
# input with role=combobox and a popup listbox, no <select> and no name. Typing
# a value is not enough - the widget only registers an answer when an option
# from its own list is chosen, so a blind fill leaves the field looking filled
# and submitting empty.
_COMBOBOX_VALUES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"how did you hear|how.{0,15}find (?:out )?about", re.I), "heard"),
    (re.compile(r"\blocation\b|\bcity\b|where are you (?:based|located)", re.I), "location"),
    (re.compile(r"country", re.I), "country"),
]


def combobox_value(label: str, profile: Profile) -> str | None:
    """What to type into a typeahead, or None to leave it alone."""
    key = next((k for pattern, k in _COMBOBOX_VALUES if pattern.search(label)), None)
    if key is not None:
        ident, answers = profile.identity, profile.answers
        value = {
            "heard": answers.how_did_you_hear,
            "location": ident.location,
            "country": (ident.location or "").split(",")[-1].strip(),
        }.get(key)
        if value:
            return value

    # Anything else the profile can answer. NK Securities renders College Name,
    # Graduation Branch, CGPA, Notice Period and years of experience all as
    # typeaheads rather than text inputs, so the text-filling path never saw
    # them and five answerable facts came back as blockers.
    return answer_for(label, profile)


# A typeahead has no <label for=...> and its placeholder is "Start typing...",
# so the generic label reader returns the placeholder rather than the question.
# The question is the heading of the field container that wraps it.
_COMBOBOX_LABEL_JS = """el => {
  const box = el.closest('fieldset, [class*="fieldEntry"], [class*="FieldEntry"], [class*="field"], [class*="Field"]');
  if (!box) return '';
  for (const h of box.querySelectorAll('legend, label, [class*="heading"], [class*="label"]')) {
    const t = (h.innerText || '').trim();
    if (t && !/^start typing/i.test(t)) return t;
  }
  return '';
}"""


def fill_comboboxes(page: Page, profile: Profile, result: ApplyResult) -> None:
    """Answer typeahead fields by picking from the widget's own list."""
    try:
        boxes = page.query_selector_all("input[role=combobox]")
    except Exception as exc:  # noqa: BLE001
        log.debug("could not find comboboxes: %s", exc)
        return

    for box in boxes:
        try:
            if (box.input_value() or "").strip():
                continue
            label = " ".join((page.evaluate(_COMBOBOX_LABEL_JS, box) or "").split())
            if not label:
                label = " ".join((_describe(box) or "").split())
            value = combobox_value(label, profile)
            if not value:
                continue

            box.click(timeout=4000)
            box.fill("")
            box.type(value[:40], delay=45)
            page.wait_for_timeout(900)

            # Only pick an option that actually relates to what was typed.
            # Taking the first one unconditionally would answer "College Name"
            # with whatever the widget happened to list first.
            options = page.query_selector_all("[role=option]") or []
            wanted = value.lower()[:24]
            chosen = None
            for option in options[:12]:
                text = " ".join((option.inner_text() or "").split()).lower()
                if not text:
                    continue
                if text.startswith(wanted) or wanted in text or text in wanted:
                    chosen = option
                    break

            if chosen is not None:
                chosen.click(timeout=4000)
            elif options:
                # It offered a list and nothing on it matched. Leaving it blank
                # reports an honest blocker; guessing writes a wrong answer.
                log.info("combobox %r: no option matched %r", label[:40], value[:30])
                box.fill("")
                continue
            else:
                # Some builds commit the typed text on Enter with no list.
                box.press("Enter")
            page.wait_for_timeout(350)

            if (box.input_value() or "").strip():
                result.filled[label[:60]] = value[:60]
        except Exception as exc:  # noqa: BLE001 - one stubborn widget, not the run
            log.debug("combobox %r not set: %s", label[:40] if label else "?", exc)
    return


# Option text for each radio group and select on the page, so choose_option can
# match against the form's own wording.
_CHOICES_JS = """() => {
  const text = (el) => (el && el.innerText ? el.innerText.trim() : '');
  // An option's own label, never the group's. Using questionFor for both made
  // every radio in a group report the same text, so nothing could be matched.
  const optionLabelOf = (el) => {
    const byFor = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (byFor && text(byFor)) return text(byFor);
    const wrap = el.closest('label');
    if (wrap && text(wrap)) return text(wrap);
    return el.value || el.getAttribute('aria-label') || el.name || '';
  };
  const questionFor = (el) => {
    const group = el.closest('fieldset, [role=group], [class*="field"], [class*="Field"]');
    if (group) {
      const h = group.querySelector('legend, label, [class*="label"], [class*="Label"]');
      if (h && text(h)) return text(h);
    }
    const byFor = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (byFor && text(byFor)) return text(byFor);
    const wrap = el.closest('label');
    if (wrap && text(wrap)) return text(wrap);
    return el.getAttribute('aria-label') || el.name || '';
  };
  const out = [];
  const seen = new Set();

  // Native radio groups.
  for (const el of document.querySelectorAll('input[type=radio]')) {
    if (el.disabled) continue;
    const key = el.name || questionFor(el);
    if (!key || seen.has('r:' + key)) continue;
    seen.add('r:' + key);
    const peers = el.name ? [...document.getElementsByName(el.name)] : [el];
    if (peers.some(p => p.checked)) continue;
    out.push({kind:'radio', key, question: questionFor(peers[0]),
              options: peers.map(p => optionLabelOf(p))});
  }

  // Checkbox groups: each box carries the OPTION in its name, and the
  // container legend carries the question. Grouping by name would make a
  // one-option group per checkbox.
  const boxes = new Map();
  for (const el of document.querySelectorAll('input[type=checkbox]')) {
    if (el.disabled) continue;
    const group = el.closest('fieldset, [role=group], [class*="field"], [class*="Field"]');
    const h = group && group.querySelector('legend, label, [class*="label"], [class*="Label"]');
    const question = (h && text(h)) || questionFor(el);
    if (!question) continue;
    if (!boxes.has(question)) boxes.set(question, []);
    boxes.get(question).push(el);
  }
  for (const [question, els] of boxes) {
    if (els.some(e => e.checked)) continue;
    if (els.length === 1) {
      // A lone checkbox is a yes/no, not a one-option multi-select. Its name
      // is usually an opaque field id, so offering that as the option text
      // gives the matcher nothing to match on - ticked means yes, left alone
      // means no.
      out.push({kind:'bool', key: question, question, options: ['Yes', 'No']});
      continue;
    }
    out.push({kind:'checkbox', key: question, question,
              options: els.map(e => e.name || e.value || optionLabelOf(e))});
  }

  // Selects.
  for (const el of document.querySelectorAll('select')) {
    if (el.disabled || el.value) continue;
    out.push({kind:'select', key: el.name || el.id || questionFor(el),
              question: questionFor(el), options: [...el.options].map(o => o.text)});
  }

  // Button-based Yes/No, which Ashby uses instead of radios. The question is
  // whatever text precedes the pair inside the surrounding field container.
  const buttons = [...document.querySelectorAll('button')]
    .filter(b => /^(yes|no)$/i.test(text(b)));
  const groups = new Map();
  for (const b of buttons) {
    const container = b.closest('[class*="field"], [class*="Field"], fieldset, div');
    if (!container) continue;
    const q = text(container).split('\\n').map(t => t.trim())
      .filter(t => t && !/^(yes|no)$/i.test(t))[0];
    if (!q) continue;
    if (!groups.has(q)) groups.set(q, []);
    groups.get(q).push(text(b));
  }
  for (const [question, options] of groups) {
    if (options.length >= 2) {
      out.push({kind:'button', key: question, question, options});
    }
  }
  return out;
}"""


def fill_choices(page: Page, profile: Profile, result: ApplyResult, *, location: str = "") -> None:
    """Answer the recurring radio, checkbox and dropdown questions.

    Only questions whose answer is stored in the profile are touched. Anything
    unrecognised is left alone and surfaces in the unanswered audit, so the
    agent never guesses at a question it has no answer for.
    """
    try:
        groups = page.evaluate(_CHOICES_JS)
    except Exception as exc:  # noqa: BLE001
        # Promoted from debug: a syntax error in this script silently disabled
        # every radio, checkbox, select and Yes/No answer on every form for as
        # long as it was broken, and nothing in the output said so.
        log.warning("choice scanner failed, no choices answered: %s", exc)
        result.unanswered.append("could not read the form's choice questions")
        return

    for group in groups:
        question = " ".join(str(group.get("question") or "").split())
        options = [" ".join(str(o).split()) for o in group.get("options") or [] if str(o).strip()]
        if not question or not options:
            continue

        wanted = choose_option(question, options, profile, location)
        if wanted is None:
            continue

        kind = group["kind"]
        try:
            if kind == "select":
                page.select_option(
                    f"select[name='{group['key']}']", label=wanted, timeout=4000
                )
            elif kind == "bool":
                # Ticking means yes. "No" is expressed by leaving it alone -
                # but it is still an answer, so it is recorded as one and must
                # not come back as an unanswered required field.
                if wanted.strip().lower().startswith("y"):
                    box = page.evaluate(
                        """(q) => {
                          const norm = s => (s||'').replace(/\s+/g,' ').trim();
                          for (const el of document.querySelectorAll('input[type=checkbox]')) {
                            const c = el.closest('fieldset, [role=group], [class*="field"], [class*="Field"]');
                            if (c && norm(c.innerText).includes(q)) { el.click(); return true; }
                          }
                          return false;
                        }""",
                        question[:80],
                    )
                    if not box:
                        continue
                    page.wait_for_timeout(200)
                result.answered_questions.append(question[:110])
            elif kind == "checkbox":
                box = page.query_selector(f"input[type=checkbox][name='{wanted}']")
                if box is None:
                    continue
                box.check(timeout=4000)
            elif kind == "button":
                # Click the Yes/No button inside this question's own container,
                # never the first one on the page.
                clicked = page.evaluate(
                    """([q, want]) => {
                      const norm = s => (s||'').replace(/\s+/g,' ').trim();
                      for (const b of document.querySelectorAll('button')) {
                        if (norm(b.innerText).toLowerCase() !== want.toLowerCase()) continue;
                        const c = b.closest('[class*="field"], [class*="Field"], fieldset, div');
                        if (c && norm(c.innerText).includes(q)) { b.click(); return true; }
                      }
                      return false;
                    }""",
                    [question[:80], wanted],
                )
                if not clicked:
                    continue
                page.wait_for_timeout(300)
            else:
                controls = page.query_selector_all(f"input[name='{group['key']}']") or []
                index = options.index(wanted)
                if index >= len(controls):
                    continue
                controls[index].check(timeout=4000)
            result.filled[question[:60]] = wanted[:60]
        except Exception as exc:  # noqa: BLE001 - a control that will not take
            log.debug("could not set %r: %s", question[:40], exc)


# Required controls that input_value() cannot speak for. Reporting only text
# inputs made a form with seven blank required radio groups and checkbox sets
# come back as "unanswered: []" - which, with shadow mode off, would have
# submitted a half-empty application.
_REQUIRED_JS = """() => {
  const missing = [];
  const optional = [];
  const seen = new Set();

  const labelOf = (el) => {
    const byFor = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (byFor && byFor.innerText.trim()) return byFor.innerText.trim();
    const group = el.closest('fieldset, [role=group], [class*="field"], [class*="Field"]');
    if (group) {
      const h = group.querySelector('legend, label, [class*="label"], [class*="Label"]');
      if (h && h.innerText.trim()) return h.innerText.trim();
    }
    return el.getAttribute('aria-label') || el.name || el.id || '';
  };

  // Form builders that render their own validation still have to *show* the
  // reader which fields are required. Ashby puts a `_required_<hash>` class on
  // the question heading; Greenhouse and Lever draw an asterisk. Both are in
  // the DOM, so both are readable - an earlier version of this function
  // assumed neither was and fell back to reporting every empty control, which
  // meant a form with twenty optional questions could never auto-submit.
  const isRequired = (el) => {
    if (el.required || el.getAttribute('aria-required') === 'true') return true;
    const group = el.closest('fieldset, [role=group], [class*="field"], [class*="Field"], [class*="entry"]');
    if (!group) return false;
    for (const h of group.querySelectorAll('legend, label, [class*="label"], [class*="heading"]')) {
      if (/_required_|(^|\s)required(\s|$)/i.test(h.className || '')) return true;
      if (/\*\s*$/m.test(h.innerText || '')) return true;
    }
    return false;
  };

  // The container holding one question and all of its options. Ashby gives
  // every checkbox in a "select all that apply" its own name, so grouping by
  // name reports five separate blockers for one question.
  const questionOf = (el) =>
    el.closest('fieldset, [role=group], [class*="fieldEntry"], [class*="FieldEntry"]')
    || el.closest('[class*="field"], [class*="Field"]');

  const questionLabel = (el) => {
    const q = questionOf(el);
    if (q) {
      const h = q.querySelector('legend, [class*="heading"], [class*="label"], label');
      if (h && h.innerText.trim()) return h.innerText.trim();
    }
    return labelOf(el);
  };

  const isEmpty = (el) => {
    if (el.type === 'radio' || el.type === 'checkbox') {
      // Answered if any option inside the same question is ticked.
      const q = questionOf(el);
      const peers = q
        ? q.querySelectorAll('input[type=radio], input[type=checkbox]')
        : (el.name ? document.getElementsByName(el.name) : [el]);
      return ![...peers].some(p => p.checked);
    }
    if (el.tagName === 'SELECT') return !el.value;
    if (el.type === 'file') return !el.files || el.files.length === 0;
    return !String(el.value || '').trim();
  };

  for (const el of document.querySelectorAll('input, textarea, select')) {
    if (el.type === 'hidden' || el.disabled) continue;
    if (el.offsetParent === null && el.type !== 'radio' && el.type !== 'checkbox') continue;

    // A typeahead is two inputs: the visible combobox the user types into, and
    // a transparent aria-hidden field behind it holding the committed value.
    // The backing field is never filled directly, so counting it reported
    // College Name, Graduation Branch and Notice Period as blockers on a form
    // where all three had been answered.
    if (el.getAttribute('aria-hidden') === 'true') continue;
    if (getComputedStyle(el).opacity === '0') continue;

    const name = el.name || el.id || labelOf(el);
    if (/recaptcha|captcha|csrf|token/i.test(name)) continue;

    const grouped = el.type === 'radio' || el.type === 'checkbox';
    const label = grouped ? questionLabel(el) : labelOf(el);
    const key = grouped ? 'q:' + label : 'el:' + (el.name || el.id || label);
    if (seen.has(key)) continue;
    seen.add(key);

    if (!isEmpty(el)) continue;
    (isRequired(el) ? missing : optional).push(label);
  }
  return {
    required: [...new Set(missing.filter(Boolean))],
    optional: [...new Set(optional.filter(Boolean))],
  };
}"""


def unanswered_required(page: Page) -> list[str]:
    """Required controls still blank - the ones that block a submit.

    Requiredness is read from the markup the form shows its reader: the HTML
    attribute, an ``aria-required``, a class naming the field required (Ashby's
    ``_required_<hash>``), or a trailing asterisk (Greenhouse, Lever). An
    earlier version assumed none of that was visible to the DOM and reported
    every empty control instead, which is why a Plaid form with twenty optional
    questions reported twenty blockers and could never auto-submit.

    Still fails closed on error: an unreadable form must never say "nothing to
    answer".
    """
    try:
        found = page.evaluate(_REQUIRED_JS)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not audit form fields: %s", exc)
        # An unknown form state must never read as "all answered".
        return ["could not verify the form"]
    if isinstance(found, dict):
        found = found.get("required", [])
    return [" ".join(str(f).split())[:110] for f in found][:20]


def blank_optional(page: Page) -> list[str]:
    """Empty controls the form does not insist on. Reported, never blocking."""
    try:
        found = page.evaluate(_REQUIRED_JS)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(found, dict):
        return []
    return [" ".join(str(f).split())[:110] for f in found.get("optional", [])][:20]


# What a refused submission says. Ashby's anti-spam wording is first because
# it is the one that actually happened, twice.
_SUBMIT_REJECTED = re.compile(
    r"couldn'?t submit your application|flagged as possible spam|"
    r"could not be submitted|submission failed|something went wrong|"
    r"please (?:correct|fix|complete) the (?:errors?|required|highlighted)|"
    r"this field is required|unable to submit|try (?:again|submitting)",
    re.IGNORECASE,
)

# What a successful one says.
_SUBMIT_CONFIRMED = re.compile(
    r"thank you for applying|application (?:has been )?(?:received|submitted|sent)|"
    r"we(?:'| ha)ve received your application|thanks for (?:applying|your interest)|"
    r"successfully submitted|your application is in",
    re.IGNORECASE,
)


def submit_rejected(page: Page) -> str:
    """The site's own words for refusing the submission, or "" if it did not."""
    try:
        body = page.inner_text("body") or ""
    except Exception:  # noqa: BLE001 - an unreadable page is not a confirmation
        return "the page could not be read after submitting"
    match = _SUBMIT_REJECTED.search(body)
    if not match:
        return ""
    # Return the sentence around the match, so the card says what went wrong.
    start = max(0, match.start() - 60)
    return " ".join(body[start : match.end() + 120].split())[:180]


def submit_confirmed(page: Page) -> bool:
    """Did the page actually say the application went through?"""
    try:
        body = page.inner_text("body") or ""
    except Exception:  # noqa: BLE001
        return False
    return bool(_SUBMIT_CONFIRMED.search(body))


def _screenshot(page: Page, job_id: int) -> str | None:
    shots = settings().screenshots_dir
    shots.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = shots / f"job{job_id}-{stamp}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception as exc:  # noqa: BLE001
        log.debug("screenshot failed: %s", exc)
        return None


def prepare_cover_letter(profile: Profile, job: Job) -> tuple[Path | None, str]:
    """Write and render the cover letter.

    Must be called *outside* any open browser context: rendering drives sync
    Playwright, and nesting it inside the applicant's own session raises
    "Sync API inside the asyncio loop".
    """
    written = writer.cover_letter(profile, job)
    if not written.ok:
        if written.violations:
            log.warning("cover letter rejected as unverifiable: %s", written.violations[:3])
        return None, ""

    path = settings().cover_letters_dir / f"{_slug(job.company)}_{_slug(job.title)}.pdf"
    try:
        writer.render_cover_letter(profile, job, written.text, path)
    except Exception as exc:  # noqa: BLE001 - the text is still usable as a textarea answer
        log.warning("cover letter render failed: %s", exc)
        return None, written.text
    return path, written.text


def apply_to(
    sess: Any,
    job: Job,
    profile: Profile,
    resume_path: Path | None,
    *,
    cover: tuple[Path | None, str] | None = None,
    requested: bool = False,
) -> ApplyResult:
    page = sess.page()
    ats = detect_ats(job.url, job.ats)
    result = ApplyResult(job_id=job.id or 0, ats=ats)

    cover_path, cover_text = cover or (None, "")

    try:
        page.goto(application_url(job, ats), wait_until="domcontentloaded", timeout=60_000)
    except Exception as exc:  # noqa: BLE001
        result.error = f"could not open the form: {str(exc).splitlines()[0][:120]}"
        return result

    sess.pause()
    dismiss_consent(page)
    if not open_application_form(page):
        result.error = "no application form found on the page"
        result.screenshot = _screenshot(page, job.id or 0)
        return result
    sess.pause()
    filled = fill_form(
        page, profile, resume_path, job=job, cover_letter_path=cover_path,
        location=job.location or "",
    )
    result.filled = filled.filled
    result.answered_questions = list(getattr(filled, "answered_questions", []))
    # A boolean answered "No" is answered, even though the box is still empty.
    _answered = {q.lower()[:60] for q in result.answered_questions}
    result.unanswered = [
        u for u in filled.unanswered if u.lower()[:60] not in _answered
    ]
    result.essay_questions = filled.essay_questions
    result.written_answers = filled.written_answers
    result.cover_letter = cover_text or None

    allowed, reason = may_autosubmit(
        ats, result.essay_questions, result.unanswered, requested=requested
    )
    result.queued_reason = reason
    result.screenshot = _screenshot(page, job.id or 0)

    if not allowed:
        return result

    for selector in (
        "button[type=submit]",
        "button:has-text('Submit application')",
        "button:has-text('Submit')",
        "input[type=submit]",
    ):
        button = page.query_selector(selector)
        if button:
            try:
                button.click()
                sess.pause()
                result.screenshot = _screenshot(page, job.id or 0)
                # A click that raises nothing is not a submission. Ashby
                # rejected two applications as "possible spam" and both were
                # recorded as sent, because nothing ever read the page back.
                # Reporting an application that does not exist is the worst
                # failure this program has: it stops him applying himself.
                rejected = submit_rejected(page)
                if rejected:
                    result.error = f"the site refused the submission: {rejected}"
                elif submit_confirmed(page):
                    result.submitted = True
                else:
                    result.queued_reason = (
                        "submitted, but the page showed no confirmation - "
                        "check the screenshot before assuming it went"
                    )
            except Exception as exc:  # noqa: BLE001
                result.error = f"submit click failed: {str(exc).splitlines()[0][:120]}"
            break
    else:
        result.queued_reason = "no submit button found"
    return result


def _record(s, job_id: int, **fields) -> Application:
    """One application row per job, updated in place.

    Re-filling a form is routine - the autonomy gate queues a job, you look
    at the screenshot, tweaks something and runs it again. Inserting a row per
    attempt made the Applied board show the same job three times and lose which
    state was current. A submitted row is never reopened: once an application
    is out, that record is history.
    """
    existing = s.exec(
        select(Application)
        .where(Application.job_id == job_id, Application.status == "awaiting_your_submit")
        .order_by(Application.id.desc())
    ).first()
    if existing is None:
        row = Application(job_id=job_id, **fields)
        s.add(row)
        return row
    for key, value in fields.items():
        setattr(existing, key, value)
    existing.updated_at = datetime.now(UTC)
    s.add(existing)
    return existing


def run(*, limit: int = 3, headless: bool = True) -> tuple[ApplyStats, list[ApplyResult]]:
    stats = ApplyStats()
    results: list[ApplyResult] = []
    profile = Profile.load(settings().profile_path)

    with session() as s:
        job_ids = list(
            s.exec(
                select(Job.id)
                .where(Job.status == JobStatus.READY)
                .order_by(Job.score.desc())
                .limit(limit)
            ).all()
        )
    if not job_ids:
        return stats, results

    # Jobs you apply to on LinkedIn/Wellfound yourself: flag, never fill.
    with session() as s:
        for job_id in list(job_ids):
            job = s.get(Job, job_id)
            if job is not None and (site := manual_apply_site(job)):
                job_ids.remove(job_id)
                job.status = JobStatus.QUEUED_APPROVAL
                job.status_reason = f"apply on {site} yourself - open the posting"
    if not job_ids:
        return stats, results

    # The daily cap is checked before any work, not inside the browser loop:
    # otherwise a spent cap still paid for a cover letter per job and opened a
    # browser, every evening, to do nothing.
    allowed = remaining_quota("application")
    if allowed <= 0:
        stats.quota_blocked += 1
        log.warning("daily application cap reached - nothing applied")
        return stats, results
    job_ids = job_ids[:allowed]

    # Cover letters first, before any browser is open - see
    # prepare_cover_letter for why nesting the two breaks.
    covers: dict[int, tuple[Path | None, str]] = {}
    if targets().get("autonomy", {}).get("write_cover_letter", True):
        for job_id in job_ids:
            with session() as s:
                job = s.get(Job, job_id)
            if job is not None:
                covers[job_id] = prepare_cover_letter(profile, job)

    with browser(headless=headless) as sess:
        for job_id in job_ids:
            if remaining_quota("application") <= 0:
                stats.quota_blocked += 1
                log.warning("daily application cap reached")
                break

            with session() as s:
                job = s.get(Job, job_id)
                resume = s.exec(
                    select(Resume).where(Resume.job_id == job_id).order_by(Resume.id.desc())
                ).first()
                # A job can reach READY without passing the queue's per-company
                # budget - tailored by hand, or queued before the cap was
                # lowered - so the budget is checked again at submit time.
                capped = job is not None and over_company_cap(s, job)
            if job is None:
                continue
            if capped:
                stats.company_capped += 1
                log.info("skipping %s %s: over the per-company cap", job.company, job.id)
                continue

            # Never attach a resume rendered before the current layout. The
            # unattended run cannot re-tailor safely here (it would spend an
            # LLM call inside the browser loop), so it declines to submit and
            # says why - "Write resume" on the job page rewrites it.
            if resume is not None:
                stale = resume_staleness(resume.pdf_path, resume.ats_detail, profile)
                if stale:
                    stats.failed += 1
                    log.warning("job %s: resume is out of date - %s", job_id, stale)
                    with session() as s:
                        tracked = s.get(Job, job_id)
                        if tracked is not None:
                            tracked.status_reason = f"resume out of date: {stale}"
                    continue

            stats.considered += 1
            resume_path = Path(resume.pdf_path) if resume and resume.pdf_path else None
            result = apply_to(sess, job, profile, resume_path, cover=covers.get(job_id))
            results.append(result)

            with session() as s:
                tracked = s.get(Job, job_id)
                if tracked is None:
                    continue
                if result.submitted:
                    consume_quota("application")
                    stats.submitted += 1
                    tracked.status = JobStatus.APPLIED
                    tracked.status_reason = f"auto-submitted via {result.ats}"
                    _record(
                        s,
                        job_id,
                        resume_id=resume.id if resume else None,
                        method=f"ats_auto:{result.ats}",
                        status="submitted",
                        screenshot_path=result.screenshot,
                        notes=(
                            f"filled: {', '.join(sorted(result.filled))}"
                            + (
                                f" | wrote {len(result.written_answers)} free-text answer(s)"
                                if result.written_answers
                                else ""
                            )
                        ),
                    )
                elif result.error:
                    stats.failed += 1
                    tracked.status_reason = f"apply failed: {result.error}"
                else:
                    stats.queued += 1
                    tracked.status = JobStatus.QUEUED_APPROVAL
                    tracked.status_reason = f"ready to submit - {result.queued_reason}"
                    _record(
                        s,
                        job_id,
                        resume_id=resume.id if resume else None,
                        method=f"ats_prefilled:{result.ats}",
                        status="awaiting_your_submit",
                        screenshot_path=result.screenshot,
                        notes=(
                            f"{result.queued_reason}. "
                            f"filled: {', '.join(sorted(result.filled)) or 'nothing'}. "
                            + (
                                f"needs answers: {'; '.join(result.essay_questions[:3])}"
                                if result.essay_questions
                                else ""
                            )
                        ),
                    )

    return stats, results


# --------------------------------------------------------------------------
# Hand-over: fill it, you send it
# --------------------------------------------------------------------------


@dataclass
class HandoverResult:
    job_id: int
    ats: str = "unknown"
    filled: dict[str, str] = field(default_factory=dict)
    unanswered: list[str] = field(default_factory=list)
    outcome: str = "abandoned"   # submitted | refused | abandoned | error
    detail: str = ""
    screenshot: str | None = None


# How long to leave the window open waiting for a decision, and how often to
# look. Ten minutes is long enough to read a form properly without leaving a
# browser running all night if he walks away.
HANDOVER_TIMEOUT_S = 600
HANDOVER_POLL_S = 2.0


def handover(job_id: int, *, timeout_s: int = HANDOVER_TIMEOUT_S) -> HandoverResult:
    """Fill the form in a visible browser and let you press Submit.

    Ashby refused two automated submissions as "possible spam" - which is the
    ATS declining automation, and its call to make. Nothing here tries to look
    less like a program: the typing is automated, the submission is a person.
    That is also the honest division, since the submission is the part that
    carries his name.

    Waits for the page to say what happened rather than assuming, and records
    the same way an automated run would.
    """
    import time

    profile = Profile.load(settings().profile_path)
    with session() as s:
        job = s.get(Job, job_id)
        resume = s.exec(
            select(Resume).where(Resume.job_id == job_id).order_by(Resume.id.desc())
        ).first()
    if job is None:
        return HandoverResult(job_id=job_id, outcome="error", detail="no such job")

    result = HandoverResult(job_id=job_id, ats=detect_ats(job.url, job.ats))
    if resume is not None:
        stale = resume_staleness(resume.pdf_path, resume.ats_detail, profile)
        if stale:
            result.outcome = "error"
            result.detail = (
                f"the saved resume is out of date ({stale}) - press Write resume "
                "on the job page first"
            )
            return result
    resume_path = Path(resume.pdf_path) if resume and resume.pdf_path else None
    cover = prepare_cover_letter(profile, job)

    # Headed on purpose: you have to see it, and the submission has to come from
    # a session a person is actually driving.
    try:
        sess = BrowserSession(headless=False).start()
    except Exception as exc:  # noqa: BLE001
        # "spawn UNKNOWN" says nothing useful on its own. Seen causes: no
        # desktop session (a service or scheduled task), or a bundled Chromium
        # Windows refuses to start - the launcher already falls back to the
        # installed Chrome and Edge, so reaching here means none of them could.
        result.outcome = "error"
        result.detail = (
            "could not open a visible browser window "
            f"({str(exc).splitlines()[0][:80]}). Install Google Chrome or "
            "Microsoft Edge, and make sure the dashboard runs in your own "
            f"desktop session (or run: python run.py handover --job-id {job_id})"
        )
        log.error(result.detail)
        return result
    try:
        page = sess.page()
        try:
            page.goto(
                application_url(job, result.ats), wait_until="domcontentloaded", timeout=60_000
            )
        except Exception as exc:  # noqa: BLE001
            result.outcome = "error"
            result.detail = f"could not open the form: {str(exc).splitlines()[0][:110]}"
            return result

        sess.pause()
        dismiss_consent(page)
        if not open_application_form(page):
            result.outcome = "error"
            result.detail = "no application form found on the page"
            result.screenshot = _screenshot(page, job_id)
            return result

        sess.pause()
        filled = fill_form(
            page, profile, resume_path, job=job,
            cover_letter_path=cover[0], location=job.location or "",
        )
        answered = {q.lower()[:60] for q in filled.answered_questions}
        result.filled = filled.filled
        result.unanswered = [u for u in filled.unanswered if u.lower()[:60] not in answered]
        result.screenshot = _screenshot(page, job_id)

        log.info(
            "job %s ready for you: %d filled, %d left. Press Submit in the window.",
            job_id, len(result.filled), len(result.unanswered),
        )

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(HANDOVER_POLL_S)
            try:
                refused = submit_rejected(page)
                if refused:
                    result.outcome, result.detail = "refused", refused
                    result.screenshot = _screenshot(page, job_id)
                    break
                if submit_confirmed(page):
                    result.outcome = "submitted"
                    result.detail = "the site confirmed it"
                    result.screenshot = _screenshot(page, job_id)
                    break
            except Exception:  # noqa: BLE001 - he closed the window, or it navigated away
                result.outcome = "abandoned"
                result.detail = "the window was closed before the page confirmed anything"
                break
        else:
            result.detail = f"no decision within {timeout_s // 60} minutes"
    finally:
        try:
            sess.close()
        except Exception:  # noqa: BLE001
            pass

    _record_handover(job_id, result, resume)
    return result


def _record_handover(job_id: int, result: HandoverResult, resume: Resume | None) -> None:
    """Write the outcome exactly as the page reported it."""
    status = {
        "submitted": "submitted",
        "refused": "awaiting_your_submit",
        "abandoned": "awaiting_your_submit",
        "error": "awaiting_your_submit",
    }[result.outcome]

    with session() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        if result.outcome == "submitted":
            consume_quota("application")
            job.status = JobStatus.APPLIED
            job.status_reason = f"submitted by you via {result.ats}"
        else:
            job.status = JobStatus.QUEUED_APPROVAL
            job.status_reason = f"{result.outcome}: {result.detail}"[:300]
        s.add(job)

        _record(
            s,
            job_id,
            resume_id=resume.id if resume else None,
            method=f"handover:{result.ats}",
            status=status,
            screenshot_path=result.screenshot,
            notes=(
                f"{result.detail}. filled: {', '.join(sorted(result.filled)) or 'nothing'}"
                + (f". left blank: {'; '.join(result.unanswered[:6])}" if result.unanswered else "")
            )[:900],
        )
