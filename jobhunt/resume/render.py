"""Render a tailored resume to PDF, then verify a machine can read it back.

Rendering through headless Chromium rather than a LaTeX toolchain keeps the
dependency footprint to something already installed for the browser agents,
and produces a real text layer - which is the only thing an ATS actually
parses.

The verification step is not optional. A resume that looks perfect and whose
text extracts in the wrong order is worse than a plain one, and the only way
to know is to parse the generated file back.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from jobhunt.resume.profile import Profile

TEMPLATE_DIR = Path(__file__).parent
TEMPLATE_NAME = "template.html"
TEMPLATE_PATH = TEMPLATE_DIR / TEMPLATE_NAME


def profile_fingerprint(profile: Profile) -> str:
    """Hash of the parts of the profile a resume actually renders.

    Deliberately not the file's timestamp. Most edits to profile.yaml are to
    application answers and preferences - sponsorship, notice period,
    citizenship - none of which appear on the page. Keying re-renders to the
    file's mtime marked every resume stale the moment one of those was saved,
    and each false positive costs an LLM call.
    """
    payload = json.dumps(
        {
            "identity": profile.identity.model_dump(),
            "education": [e.model_dump() for e in profile.education],
            "experience": [x.model_dump() for x in profile.experience],
            "projects": [p.model_dump() for p in profile.projects],
            "skills": profile.skills.model_dump(),
            "achievements": list(profile.achievements),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


_TEMPLATE_COMMENTS = re.compile(r"<!--.*?-->|\{#.*?#\}|/\*.*?\*/", re.S)


def layout_fingerprint() -> str:
    """Hash of what the template actually renders.

    Comments are stripped and whitespace collapsed first: editing a comment
    does not change a single pixel, and the previous timestamp comparison
    marked every resume stale - an LLM call each - whenever one was edited.
    """
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = " ".join(_TEMPLATE_COMMENTS.sub(" ", html).split())
    return hashlib.sha256(html.encode("utf-8")).hexdigest()[:16]


# (path, mtime, size) -> whether an older PDF already follows today's layout.
_LEGACY_LAYOUT: dict[tuple[str, int, int], bool | None] = {}


def pdf_follows_layout(pdf: Path) -> bool | None:
    """Whether a PDF's section headings match the current template.

    For resumes written before the layout fingerprint was recorded. Judged on
    the text itself: the superseded layout opens with SUMMARY and says
    EXPERIENCE; the current one runs EDUCATION, SKILLS, WORK EXPERIENCE.
    None when the file has no readable text layer.
    """
    try:
        st = pdf.stat()
    except OSError:
        return None
    key = (str(pdf), st.st_mtime_ns, st.st_size)
    if key in _LEGACY_LAYOUT:
        return _LEGACY_LAYOUT[key]

    try:
        from pdfminer.high_level import extract_text

        text = extract_text(str(pdf), maxpages=2)
    except Exception:  # noqa: BLE001 - unreadable means "cannot tell"
        text = ""
    _LEGACY_LAYOUT[key] = result = layout_matches(text) if text.strip() else None
    return result


def layout_matches(text: str) -> bool:
    """Do these extracted resume headings follow the current template?"""
    lines = [ln.strip().lower() for ln in text.splitlines() if ln.strip()]
    if "summary" in lines:
        return False
    positions = []
    for heading in SECTION_ORDER:
        if heading in lines:
            positions.append(lines.index(heading))
        elif heading in ("education", "skills", "work experience"):
            return False  # always rendered; missing means another layout
    return positions == sorted(positions)


def is_stale(
    pdf_path: str | Path,
    *,
    profile: Profile | None = None,
    fingerprint: str | None = None,
    layout: str | None = None,
) -> str:
    """Why this rendered PDF should be written again, or "" if it is current.

    A Resume row is kept and reused for the life of a job, so a PDF rendered
    under the old layout stayed attached to it. That is how a Render
    application went out in the superseded template - SUMMARY section, four
    skills lines, wrong section order - hours after the layout was corrected.
    Nothing noticed, because a resume existed.

    ``fingerprint`` and ``layout`` are what the resume recorded of the profile
    and the template when it was written. Rows from before either was
    recorded are judged by content instead: the PDF's own section headings,
    and nothing for the profile.
    """
    pdf = Path(pdf_path)
    if not pdf.exists():
        return "the PDF is missing"

    if layout:
        if layout != layout_fingerprint():
            return "the resume layout changed since this was written"
    else:
        follows = pdf_follows_layout(pdf)
        if follows is False:
            return OLD_LAYOUT
        if follows is None and TEMPLATE_PATH.stat().st_mtime > pdf.stat().st_mtime:
            # No text layer to judge by - fall back to file times.
            return "the resume layout changed since this was written"

    if profile is not None and fingerprint:
        if profile_fingerprint(profile) != fingerprint:
            return "your profile changed since this was written"
    return ""


OLD_LAYOUT = "it uses an old resume layout, written before the current one"


def resume_mode() -> str:
    """"tailored" (rewrite per job) or "original" (send your own PDF as-is)."""
    from jobhunt.config import targets

    mode = str(targets().get("resume", {}).get("mode", "tailored")).strip().lower()
    return "original" if mode == "original" else "tailored"


_ORIGINAL_SHA: dict[tuple[int, int], str] = {}


def original_fingerprint() -> str | None:
    """Hash of your uploaded resume PDF, or None if there is none."""
    from jobhunt.config import settings

    path = settings().uploaded_resume_path
    try:
        st = path.stat()
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    if key not in _ORIGINAL_SHA:
        _ORIGINAL_SHA.clear()
        _ORIGINAL_SHA[key] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return _ORIGINAL_SHA[key]


def resume_staleness(pdf_path: str | Path, detail: dict | None, profile: Profile | None) -> str:
    """:func:`is_stale` for a Resume row, using everything it recorded.

    Also enforces the resume mode: in "original" mode only an unchanged copy
    of your current PDF is current, and in "tailored" mode a copy of your own
    PDF is superseded by a tailored one.
    """
    d = detail or {}
    mode = resume_mode()
    if d.get("kind") == "original":
        if mode != "original":
            return "you switched to tailored resumes"
        current = original_fingerprint()
        if current is None:
            return "your resume PDF is missing - upload it on the Filters page"
        if d.get("original_sha") != current:
            return "you uploaded a newer resume"
        return "" if Path(pdf_path).exists() else "the PDF is missing"
    if mode == "original":
        return "you chose to send your own resume unchanged"
    if d.get("layout_outdated"):
        return OLD_LAYOUT
    return is_stale(
        pdf_path,
        profile=profile,
        fingerprint=d.get("profile_fingerprint"),
        layout=d.get("layout_fingerprint"),
    )


def _section_order() -> tuple[str, ...]:
    """Section headings in the order the template emits them.

    Derived from the template so the extraction-order check cannot drift out
    of sync with the layout.
    """
    html = (TEMPLATE_DIR / TEMPLATE_NAME).read_text(encoding="utf-8")
    return tuple(h.strip().lower() for h in re.findall(r"<h2>([^<{]+)</h2>", html))


SECTION_ORDER = _section_order()

MIN_COVERAGE = 0.85

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#./-]{1,}")

# Grammatical filler. Never a skill.
_STOP = frozenset(
    ["a", "an", "the", "and", "or", "but", "if", "then", "than", "that", "this", "these", "those", "for", "to", "of", "in", "on", "at", "by", "with", "from", "as", "is", "are", "was", "were", "be", "been", "being", "have", "has", "had", "do", "does", "did", "will", "would", "shall", "should", "can", "could", "may", "might", "must", "you", "your", "we", "our", "us", "they", "their", "it", "its", "he", "she", "his", "her", "who", "whom", "which", "what", "when", "where", "why", "how", "all", "any", "both", "each", "few", "more", "most", "other", "some", "such", "no", "nor", "not", "only", "own", "same", "so", "too", "very", "just", "also", "about", "into", "over", "under", "again", "further", "once", "here", "there", "while", "during", "before", "after", "above", "below", "up", "down", "out", "off", "role", "team", "work", "working", "experience", "years", "year", "including", "etc", "ability", "able", "help", "across", "within", "using", "use", "used"]
)

# Where a posting stops describing the job and starts describing the company's
# legal and benefits position. Nothing after this is answerable by a resume.
_BOILERPLATE_START = re.compile(
    r"\b(?:equal\s+(?:employment\s+)?opportunity|we\s+are\s+an\s+equal|"
    r"eeo\b|affirmative\s+action|reasonable\s+accommodation|"
    r"applicants?\s+with\s+disabilities|fair\s+chance|ban\s+the\s+box|"
    r"benefits\s*(?:&|and)?\s*perks|our\s+benefits|what\s+we\s+offer\b|"
    r"compensation\s+(?:range|and\s+benefits)|pay\s+transparency|"
    r"e-verify|privacy\s+policy|to\s+all\s+recruitment\s+agencies)\b",
    re.IGNORECASE,
)

# HR and legal vocabulary that survives into the requirements section but which
# a resume has no business containing.
_HR_BOILERPLATE = frozenset(
    ["gender", "sexual", "orientation", "disability", "disabilities", "veteran", "race", "religion", "ethnicity", "accommodation", "accommodations", "medical", "dental", "vision", "insurance", "parental", "leave", "pto", "equity", "compensation", "salary", "benefit", "benefits", "perks", "bonus", "stipend", "reimbursement", "applicant", "applicants", "candidate", "candidates", "employer", "employment", "employee", "employees", "hiring", "recruiter", "recruiters", "recruitment", "interview", "interviews", "offer", "offers", "please", "apply", "application", "resume", "cv", "submit", "consideration", "considered", "eligible", "status", "identity", "expression", "age", "national", "origin", "protected", "law", "laws", "legal", "comply", "diversity", "inclusion", "inclusive", "belonging", "equal", "opportunity", "workplace", "culture", "company", "mission", "values", "people", "team", "teams", "everyone", "anyone", "someone", "position", "role", "job", "posting", "description", "qualifications", "responsibilities", "requirements", "preferred", "plus", "nice", "must", "should", "would", "like", "love", "passionate", "excited", "life", "healthy", "wellness", "family", "flexible", "remote", "hybrid", "office", "location", "locations"]
)

from jobhunt.resume.vocab import (  # noqa: E402, F401  (TECH_VOCAB is re-exported)
    TECH_VOCAB,
    equivalents,
    known_to,
    matches_any,
)


@dataclass
class ATSReport:
    """How well a rendered resume answers one job description.

    ``coverage`` measures the *surfaced overlap*: of the skills this job asks
    for that the candidate genuinely has, how many appear on the resume.

    Scoring against every skill the JD names instead makes the metric
    unreachable by construction. A Figma posting wanting Ruby, SAML, Snowflake
    and PostgreSQL caps an honest resume at 47%, and no rewriting fixes that
    without lying - which is exactly what the truthfulness validator exists to
    prevent. Measuring the surfaced overlap asks the only question the Tailor
    can actually act on.

    ``jd_skill_match`` keeps the other number visible: what share of the JD's
    requirements the candidate has at all. That is a *fit* signal, useful for
    reporting, not something rewriting can improve.
    """

    extractable: bool
    char_count: int
    coverage: float                       # surfaced / relevant
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unmet: list[str] = field(default_factory=list)   # JD asks, profile lacks
    jd_skill_match: float = 0.0           # relevant / all JD skills
    order_ok: bool = True
    pages: int = 1
    max_pages: int = 1
    notes: list[str] = field(default_factory=list)

    @property
    def failures(self) -> list[str]:
        """Why this did not pass, in the order the checks run."""
        out: list[str] = []
        if not self.extractable:
            out.append(f"text not extractable ({self.char_count} chars)")
        if not self.order_ok:
            out.append("sections extract out of order")
        if self.pages > self.max_pages:
            out.append(f"{self.pages} pages, target {self.max_pages}")
        if self.coverage < MIN_COVERAGE:
            out.append(f"surfaced {self.coverage:.0%} of relevant skills, target {MIN_COVERAGE:.0%}")
        return out

    @property
    def passed(self) -> bool:
        # Page count is a gate, not a note. At ~1 year of experience a
        # two-page resume reads as padding, and the retry loop can fix it by
        # cutting bullets - which is what the Tailor is for.
        return (
            self.extractable
            and self.order_ok
            and self.pages <= self.max_pages
            and self.coverage >= MIN_COVERAGE
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "extractable": self.extractable,
            "char_count": self.char_count,
            "coverage": round(self.coverage, 3),
            "jd_skill_match": round(self.jd_skill_match, 3),
            "matched": self.matched,
            "missing": self.missing,
            "unmet": self.unmet,
            "order_ok": self.order_ok,
            "pages": self.pages,
            "max_pages": self.max_pages,
            "notes": self.notes,
        }


# Progressively tighter layouts. A resume that is marginally over one page is
# a typesetting problem, not a content problem - asking the model to cut more
# costs an LLM call and loses real material, when 0.4pt of leading would have
# fixed it. Only if every step still overflows is the content genuinely too long.
# Step 0 reproduces the source Overleaf document's measurements: 10pt body,
# 10.9pt headings, a 24.8pt name. Later steps tighten from there.
# Step 0 reproduces the LaTeX source exactly. The document is
# \documentclass[letterpaper,11pt], so the sizes are that class's:
#   normal 10.95pt · \small 10pt · \large 12.1pt · \Huge 24.88pt
# and the margins are 0.5in (\oddsidemargin -0.5in with \textwidth +1in).
DENSITY_STEPS = [
    {"font": 10.95, "line": 1.22, "margin": "0.5in", "name": 24.88, "heading": 12.1,
     "small": 10.0, "gap": 7, "bullet_gap": 1.6, "section_gap": 11},
    {"font": 10.4,  "line": 1.18, "margin": "0.46in", "name": 23.5, "heading": 11.5,
     "small": 9.5, "gap": 6, "bullet_gap": 1.3, "section_gap": 10},
    {"font": 9.9,   "line": 1.15, "margin": "0.42in", "name": 22.0, "heading": 11.0,
     "small": 9.1, "gap": 5, "bullet_gap": 1.1, "section_gap": 9},
    {"font": 9.4,   "line": 1.12, "margin": "0.38in", "name": 20.5, "heading": 10.4,
     "small": 8.7, "gap": 4, "bullet_gap": 0.9, "section_gap": 8},
]


def render_html(
    profile: Profile,
    *,
    experience: list[Any],
    projects: list[Any],
    skills: Any,
    achievements: list[str],
    summary: str = "",
    density: dict[str, Any] | None = None,
) -> str:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env.get_template(TEMPLATE_NAME).render(
        profile=profile,
        experience=experience,
        projects=projects,
        skills=skills,
        achievements=achievements,
        summary=summary,
        density=density or DENSITY_STEPS[0],
    )


def count_pages(pdf_path: Path) -> int:
    from pdfminer.high_level import extract_text

    try:
        return max(1, extract_text(str(pdf_path)).count("\f"))
    except Exception:  # noqa: BLE001
        return 1


def render_fitting(
    profile: Profile,
    out_path: Path,
    *,
    max_pages: int = 1,
    **content: Any,
) -> tuple[Path, dict[str, Any], int]:
    """Render, tightening the layout until it fits. Returns (path, density, pages)."""
    pages = 99
    density = DENSITY_STEPS[0]
    for density in DENSITY_STEPS:
        html_to_pdf(render_html(profile, density=density, **content), out_path)
        pages = count_pages(out_path)
        if pages <= max_pages:
            return out_path, density, pages
    return out_path, density, pages


def html_to_pdf(html: str, out_path: Path) -> Path:
    """Print the HTML to PDF with headless Chromium."""
    from playwright.sync_api import sync_playwright

    from jobhunt.sources.browser.session import launch_with_fallback

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = launch_with_fallback(p.chromium.launch, headless=True)
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            page.pdf(
                path=str(out_path),
                format="A4",
                print_background=False,
                margin={"top": "12mm", "bottom": "12mm", "left": "13mm", "right": "13mm"},
            )
        finally:
            browser.close()
    return out_path


def requirements_section(text: str) -> str:
    """Trim a job description to the part a resume can answer.

    Raw frequency over a whole posting is dominated by EEO and benefits
    boilerplate - on a real Plaid posting the top keywords included "gender",
    "sexual", "disability" and "accommodations".
    """
    if not text:
        return ""
    cut = len(text)
    for m in _BOILERPLATE_START.finditer(text):
        # Only trust a boilerplate marker in the back half; some postings open
        # with a diversity statement before any real content.
        if m.start() > len(text) * 0.4:
            cut = min(cut, m.start())
    return text[:cut]


def keywords(
    text: str, *, limit: int = 40, company: str = "", extra_vocab: set[str] | None = None
) -> list[str]:
    """The *skill* terms a job description asks for, most frequent first.

    Deliberately not "frequent content words". Measured against those, a
    correctly tailored resume scored 25-38%, because the terms it lacked were
    "systems", "collaborate", "bring", "looking" - prose no honest resume
    contains and no ATS matches on.
    """
    own = {w.lower() for w in re.split(r"[^A-Za-z0-9]+", company or "") if len(w) > 2}
    vocab = TECH_VOCAB | {w.lower() for w in (extra_vocab or set())}

    counts: dict[str, int] = {}
    for m in _WORD.finditer(requirements_section(text)):
        w = m.group(0).lower().strip(".")
        if w in _STOP or w in _HR_BOILERPLATE or w in own or len(w) < 2:
            continue
        if w not in vocab:
            continue
        counts[w] = counts.get(w, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [w for w, _ in ranked[:limit]]


def _one_per_meaning(terms: list[str]) -> list[str]:
    """Collapse different spellings of the same skill to a single entry.

    A JD that writes both "full-stack" and "fullstack" is asking for one
    thing. Counting both put it in the denominator twice, so a resume that
    happened to use neither spelling lost two points instead of one - which is
    precisely how a genuinely well-matched resume scored 83% against an 85%
    gate and never shipped.

    Terms arrive frequency-ranked, so keeping the first occurrence keeps the
    spelling the posting leans on.
    """
    seen: set[frozenset[str]] = set()
    out: list[str] = []
    for term in terms:
        group = equivalents(term)
        if group in seen:
            continue
        seen.add(group)
        out.append(term)
    return out


def verify_pdf(
    pdf_path: Path,
    job_description: str = "",
    *,
    max_pages: int = 1,
    company: str = "",
    candidate_vocab: set[str] | None = None,
) -> ATSReport:
    """Parse the generated PDF back and check a machine can actually read it.

    ``max_pages`` defaults to 1: at ~1 year of experience a two-page resume
    reads as padding, and the Tailor is expected to select bullets down to a
    single page rather than include everything.

    ``candidate_vocab`` is everything the candidate can honestly claim. It both
    widens keyword extraction and decides which of the JD's skills are the
    Tailor's responsibility to surface.
    """
    from pdfminer.high_level import extract_text

    try:
        text = extract_text(str(pdf_path))
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return ATSReport(
            extractable=False,
            char_count=0,
            coverage=0.0,
            notes=[f"pdfminer could not parse the file: {exc}"],
        )

    stripped = text.strip()
    report = ATSReport(
        extractable=len(stripped) > 400,
        char_count=len(stripped),
        coverage=0.0,
        max_pages=max_pages,
    )
    if not report.extractable:
        report.notes.append(
            f"only {len(stripped)} characters extracted - the text layer is missing or broken"
        )
        return report

    lowered = text.lower()

    # Headings must come out in the order the template lays them out. If they
    # do not, the extractor is reading across columns and an ATS will
    # interleave unrelated lines.
    #
    # The expected sequence is read from the template rather than hardcoded:
    # pinning one order broke the moment the layout was changed to match
    # the original resume layout, which puts education before skills.
    expected = [h for h in SECTION_ORDER if h in lowered]
    positions = [lowered.index(h) for h in expected]
    report.order_ok = positions == sorted(positions)
    if not report.order_ok:
        report.notes.append(f"sections extract out of order: {expected}")

    report.pages = max(1, text.count("\f"))
    if report.pages > max_pages:
        report.notes.append(f"{report.pages} pages - target is {max_pages}; trim bullets")

    if not job_description:
        report.coverage = 1.0
        return report

    have = {w.lower() for w in (candidate_vocab or set())}
    wanted = _one_per_meaning(keywords(job_description, company=company, extra_vocab=have))

    # Matching runs through the synonym table in both directions. A JD asking
    # for "K8s" is asking for Kubernetes: it is a skill the candidate has, and a resume
    # saying "Kubernetes" answers it. Comparing the raw strings got this wrong
    # twice over - the term landed in `unmet` as a gap he does not have, and in
    # `missing` as one the resume failed to surface.
    relevant = [w for w in wanted if known_to(w, have)] if have else wanted
    report.unmet = [w for w in wanted if not known_to(w, have)] if have else []
    report.jd_skill_match = (len(relevant) / len(wanted)) if wanted else 1.0

    present = {w for w in relevant if matches_any(w, lowered)}
    report.matched = sorted(present)
    report.missing = [w for w in relevant if w not in present]
    report.coverage = (len(present) / len(relevant)) if relevant else 1.0

    if report.missing:
        report.notes.append(
            "in the profile but not surfaced: " + ", ".join(report.missing[:10])
        )

    return report
