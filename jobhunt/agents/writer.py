"""Writer - free-text application answers and cover letters.

Everything here goes through the same truthfulness gate as the resume: the
model may only use facts from the master profile and claims made by the job
description itself. A "why this company" answer that invents an interest, a
project or an admiration is worse than no answer, because it has to be defended
in an interview.

What the model *may* do is reason about fit: connect something the posting
actually says to something the profile actually contains. That is the whole
job, and it is not fabrication.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from jobhunt.llm.adapter import MODEL_JUDGE, LLMError, complete_json
from jobhunt.resume.profile import FactRegistry, Profile
from jobhunt.store.models import Job

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
ANSWER_WORDS = 130
COVER_WORDS = 260

_SHARED_RULES = """
ABSOLUTE RULE: every factual claim must come from the CANDIDATE PROFILE or the
JOB POSTING below. You may connect the two - that is the point - but you may
not invent:
- experience, employers, projects, technologies or metrics not in the profile
- a personal history with the company ("I have admired", "I have long used")
- opinions about products the candidate has not demonstrably used
- degrees, certifications, publications or awards

Numbers are copied exactly. "87 defects" never becomes "nearly 100".

Style:
- Plain, specific sentences. No "passionate", "thrilled", "excited to apply",
  "dynamic", "fast-paced", "leverage", "synergy".
- Lead with substance, not with enthusiasm.
- Never open with "I hope this finds you well" or "I am writing to apply".
- Concrete over general: name the actual system, the actual number.
"""

_ANSWER_SYSTEM = f"""You answer one free-text question on a job application,
writing as the candidate in the first person.
{_SHARED_RULES}
Answer the question that was asked, directly, in at most {ANSWER_WORDS} words.
If the question asks about interest in the company, ground it in what the
posting itself describes - the product, the technical problem, the team's
mandate - and connect it to specific work the candidate has actually done.

Return JSON: {{"answer": "<the answer>"}}"""

_COVER_SYSTEM = f"""You write a cover letter for one specific role.
{_SHARED_RULES}
Structure, at most {COVER_WORDS} words total:
1. One opening sentence naming the role and the single strongest reason this
   candidate is a credible fit. No preamble.
2. Two short paragraphs of evidence. Each takes one requirement from the
   posting and answers it with something the candidate has actually shipped,
   with the real numbers.
3. One closing sentence: what they want next. No "I look forward to hearing
   from you at your earliest convenience".

No address block, no date, no "Dear Hiring Manager" unless a name is given.
Start with the salutation line if a name is known, otherwise start with the
opening sentence.

Return JSON: {{"letter": "<the letter>"}}"""


@dataclass
class WrittenText:
    text: str = ""
    attempts: int = 0
    violations: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.violations and not self.error


def _context(profile: Profile, job: Job) -> str:
    experience = "\n".join(
        f"- {e.title} at {e.company} ({e.start} - {e.end})\n"
        + "\n".join(f"    * {b}" for b in e.bullets)
        for e in profile.experience
    )
    projects = "\n".join(
        f"- {p.name} [{', '.join(p.tech)}]\n" + "\n".join(f"    * {b}" for b in p.bullets)
        for p in profile.projects
    )

    return f"""# CANDIDATE PROFILE

{profile.identity.name} - {profile.years_text()} years' experience.
Currently {profile.current_role() or "looking for a first full-time role"}.

## Experience
{experience}

## Projects
{projects}

## Skills
{", ".join(profile.all_skills())}

## Achievements
{chr(10).join("- " + a for a in profile.achievements)}

# JOB POSTING

Company: {job.company}
Role: {job.title}
Location: {job.location}

{(job.description or "")[:5000]}"""


def _generate(
    profile: Profile,
    job: Job,
    system: str,
    task: str,
    key: str,
    *,
    max_tokens: int = 2000,
) -> WrittenText:
    """Generate, fact-check, and retry once against the violations."""
    result = WrittenText()
    facts: FactRegistry = profile.facts()
    correction = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        result.attempts = attempt
        try:
            data = complete_json(
                f"{_context(profile, job)}\n\n# TASK\n\n{task}\n{correction}",
                system=system,
                model=MODEL_JUDGE,
                max_tokens=max_tokens,
            )
        except LLMError as exc:
            result.error = str(exc)
            return result

        text = (data.get(key) or "").strip()
        if not text:
            result.error = f"model returned no {key}"
            return result

        # The posting's own vocabulary is legitimate to quote back; only claims
        # about the *candidate* have to trace to the profile.
        violations = facts.check(_strip_posting_terms(text, job))
        if not violations:
            result.text = text
            result.violations = []
            return result

        result.violations = violations
        if attempt < MAX_ATTEMPTS:
            correction = (
                "\n\n# CORRECTION REQUIRED\n"
                "Your previous draft made claims that are not in the candidate "
                "profile. Remove or rephrase each of these, using only profile "
                "facts:\n" + "\n".join(f"  - {v}" for v in violations[:20])
            )
    return result


def _strip_posting_terms(text: str, job: Job) -> str:
    """Remove wording the posting itself supplies before fact-checking.

    Naming the employer, the role, or a technology the job asks for is quoting
    the posting back, not claiming experience with it. Checking those against
    the profile would flag every correctly-written cover letter.
    """
    terms = {job.company, job.title, job.location or ""}
    for token in re.findall(r"[A-Z][A-Za-z0-9+#.]{1,}", (job.description or "")[:5000]):
        terms.add(token)

    cleaned = text
    for term in sorted((t for t in terms if t and len(t) > 1), key=len, reverse=True):
        cleaned = re.sub(rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])", " ", cleaned)
    return cleaned


def answer_question(profile: Profile, job: Job, question: str) -> WrittenText:
    """Answer one free-text application question."""
    return _generate(
        profile,
        job,
        _ANSWER_SYSTEM,
        f'Answer this application question:\n\n"{question.strip()}"',
        "answer",
        max_tokens=1500,
    )


def cover_letter(profile: Profile, job: Job, recipient: str = "") -> WrittenText:
    """Write a cover letter for one role."""
    who = f"Address it to {recipient}." if recipient else "No recipient name is known."
    return _generate(
        profile,
        job,
        _COVER_SYSTEM,
        f"Write the cover letter for this role. {who}",
        "letter",
        max_tokens=2500,
    )


_FOLLOWUP_SYSTEM = f"""You write a short follow-up email after a job interview,
as the candidate, in the first person.
{_SHARED_RULES}
At most 120 words. Structure:
1. One line thanking them for the specific conversation.
2. One or two sentences that add something - a point the candidate wishes they
   had made, a clearer answer to something they fumbled, or a concrete link
   between what was discussed and work they have actually shipped. This is the
   only reason the email is worth sending, so it must carry real content.
3. One closing line about next steps.

Never restate the whole resume. Never say "I am excited about this
opportunity", "thank you for your time and consideration", or "please do not
hesitate to reach out". Do not invent details of a conversation you were not
told about - if no interview notes are supplied, keep the middle grounded in
the role's own requirements and the candidate's real work.

Return JSON: {{"note": "<the email body, no subject line>"}}"""

# What stage the note is written for, and what it should do there.
FOLLOWUP_STAGES = {
    "screen": "a recruiter screen - keep it brief and confirm interest in the role",
    "interview": "a technical interview with engineers",
    "onsite": "a final round or onsite loop, likely with several interviewers",
    "offer": "a conversation where an offer was discussed",
}


def follow_up(
    profile: Profile,
    job: Job,
    stage: str = "interview",
    *,
    interviewer: str = "",
    notes: str = "",
) -> WrittenText:
    """Draft the note to send after an interview.

    The highest-leverage email in the funnel and the one most often skipped,
    because writing it well means remembering what was actually said. Pass
    ``notes`` and it uses them; leave them empty and it stays general rather
    than inventing a conversation.
    """
    context = f"This is after {FOLLOWUP_STAGES.get(stage, FOLLOWUP_STAGES['interview'])}."
    if interviewer:
        context += f" Address it to {interviewer}."
    if notes:
        context += "\n\nWhat was actually discussed:\n" + notes
    else:
        context += (
            " No notes from the conversation were supplied, so do not describe"
            " what was discussed - ground the middle in the role's requirements."
        )

    return _generate(
        profile, job, _FOLLOWUP_SYSTEM, f"Write the follow-up note. {context}",
        "note", max_tokens=1200,
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

_LETTER_CSS = """
@page { size: A4; margin: 22mm 20mm; }
body { font-family: Georgia, "Times New Roman", serif; font-size: 11pt;
       line-height: 1.5; color: #111; margin: 0; }
.head { margin-bottom: 16pt; }
.name { font-size: 15pt; font-weight: 700; letter-spacing: .3px; }
.contact { font-size: 9.2pt; color: #333; margin-top: 2pt; }
.date { font-size: 9.2pt; color: #444; margin: 14pt 0 12pt; }
p { margin: 0 0 9pt; }
"""


def render_cover_letter(profile: Profile, job: Job, text: str, out_path: Path) -> Path:
    """Render the letter to PDF, for forms that want an upload."""
    from html import escape

    from jobhunt.resume.render import html_to_pdf

    ident = profile.identity
    contact = "  |  ".join(
        x for x in (ident.phone, ident.email, ident.linkedin, ident.github) if x
    )
    paragraphs = "\n".join(
        f"<p>{escape(p.strip())}</p>" for p in text.split("\n") if p.strip()
    )

    html = f"""<meta charset="utf-8" />
<title>{escape(ident.name)} - {escape(job.company)}</title>
<style>{_LETTER_CSS}</style>
<div class="head">
  <div class="name">{escape(ident.name)}</div>
  <div class="contact">{escape(contact)}</div>
</div>
<div class="date">{datetime.now():%d %B %Y}</div>
{paragraphs}"""
    return html_to_pdf(html, out_path)
