"""Tailor - rewrite the resume against one job description.

The model may **select, reorder and rephrase** what is in the master profile.
It may not add anything. Two independent mechanisms enforce that, because
prompt instructions alone are not a guarantee:

* the prompt gives it only the profile's own bullets to work from;
* :class:`FactRegistry` re-checks the generated text and fails the render if
  any company, technology, credential or metric is not traceable to the
  profile.

The cost of a fabricated bullet is walking into an interview behind a claim
you cannot defend, so a violation blocks the PDF rather than warning about it.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from jobhunt.agents.selection import pick_for_tailoring
from jobhunt.config import settings, targets
from jobhunt.llm.adapter import MODEL_JUDGE, LLMError, complete_json
from jobhunt.resume.profile import Experience, Profile, Project, Skills
from jobhunt.resume.render import (
    DENSITY_STEPS,
    MIN_COVERAGE,
    TECH_VOCAB,
    ATSReport,
    layout_fingerprint,
    original_fingerprint,
    profile_fingerprint,
    render_fitting,
    resume_mode,
    verify_pdf,
)
from jobhunt.store.db import session
from jobhunt.store.models import Job, JobStatus, Resume

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 2

_SYSTEM = """You tailor one candidate's resume to one job description.

ABSOLUTE RULE: you may only use facts present in the MASTER PROFILE below.
Select which bullets to include, reorder them, and rewrite their wording to
foreground what this job asks for. You may not introduce a company,
technology, metric, credential, title or date that is not already in the
profile. Inventing "Kubernetes" because the job wants it is a failure, not a
tailoring decision. Every number must survive unchanged: "87 defects" may not
become "~100 defects" or "nearly 100 defects".

What good tailoring looks like:
- lead each role with the bullets closest to this job's requirements
- rewrite bullets to use the job's own vocabulary WHERE THE UNDERLYING FACT
  ALREADY MATCHES ("Playwright/Cypress automation" -> "end-to-end test
  automation" is fine; adding "Selenium" is not)
- drop bullets irrelevant to this role - a shorter, sharper resume wins
- order the skills lists so the job's required skills come first, but do not
  add skills the profile does not list

Target ONE page. That means roughly 10-14 bullets in total across all roles
and projects. Cut, do not pad."""


@dataclass
class TailorResult:
    job_id: int
    ok: bool
    pdf_path: Path | None = None
    ats: ATSReport | None = None
    violations: list[str] = field(default_factory=list)
    attempts: int = 0
    changes: str = ""
    error: str = ""


def _profile_block(profile: Profile) -> str:
    lines = ["## EXPERIENCE"]
    for i, e in enumerate(profile.experience):
        lines.append(f"[exp{i}] {e.title} | {e.company} | {e.location} | {e.start} - {e.end}")
        for j, b in enumerate(e.bullets):
            lines.append(f"    [exp{i}.b{j}] {b}")
        if e.tech:
            lines.append(f"    tech: {', '.join(e.tech)}")
    lines.append("\n## PROJECTS")
    for i, p in enumerate(profile.projects):
        lines.append(f"[proj{i}] {p.name} | {', '.join(p.tech)}")
        for j, b in enumerate(p.bullets):
            lines.append(f"    [proj{i}.b{j}] {b}")
    lines.append("\n## SKILLS (the complete set - do not add to it)")
    s = profile.skills
    lines.append(f"languages: {', '.join(s.languages)}")
    lines.append(f"frameworks: {', '.join(s.frameworks)}")
    lines.append(f"tools: {', '.join(s.tools)}")
    lines.append(f"domains: {', '.join(s.domains)}")
    lines.append("\n## ACHIEVEMENTS")
    for i, a in enumerate(profile.achievements):
        lines.append(f"[ach{i}] {a}")
    return "\n".join(lines)


def build_prompt(profile: Profile, job: Job, retry_note: str = "") -> str:
    schema = """Return JSON:
{
  "experience": [{"index": <int, position in MASTER PROFILE>,
                  "bullets": ["<rewritten>", ...]}],
  "projects":   [{"index": <int>, "bullets": ["<rewritten>", ...]}],
  "skills": {"languages": [], "frameworks": [], "tools": [], "domains": []},
  "achievements": ["<selected, verbatim or lightly trimmed>"],
  "changes": "<one short paragraph: what you foregrounded and what you cut, and why>"
}"""
    return f"""# MASTER PROFILE

{_profile_block(profile)}

# TARGET ROLE

company: {job.company}
title: {job.title}
location: {job.location}

description:
{(job.description or "")[:6000]}

{retry_note}

{schema}"""


def _rebuild(profile: Profile, data: dict) -> tuple[list[Experience], list[Project], Skills, list[str]]:
    """Apply the model's selections onto the real profile objects.

    Structural fields - company, title, dates, location - are copied from the
    profile and never taken from the model, so they cannot drift even if the
    response is malformed.
    """
    experience: list[Experience] = []
    for item in data.get("experience") or []:
        idx = int(item.get("index", -1))
        if not 0 <= idx < len(profile.experience):
            continue
        source = profile.experience[idx]
        experience.append(
            Experience(
                company=source.company,
                title=source.title,
                location=source.location,
                start=source.start,
                end=source.end,
                bullets=[b for b in (item.get("bullets") or []) if b.strip()],
                tech=source.tech,
            )
        )

    projects: list[Project] = []
    for item in data.get("projects") or []:
        idx = int(item.get("index", -1))
        if not 0 <= idx < len(profile.projects):
            continue
        source = profile.projects[idx]
        projects.append(
            Project(
                name=source.name,
                tech=source.tech,
                url=source.url,
                bullets=[b for b in (item.get("bullets") or []) if b.strip()],
            )
        )

    raw_skills = data.get("skills") or {}
    known = {s.lower(): s for s in profile.all_skills()}

    def keep(values: list[str]) -> list[str]:
        # Drop anything the profile does not list, rather than trusting the
        # model not to have added to the set.
        return [known[v.lower()] for v in values or [] if v.lower() in known]

    skills = Skills(
        languages=keep(raw_skills.get("languages")),
        frameworks=keep(raw_skills.get("frameworks")),
        tools=keep(raw_skills.get("tools")),
        domains=keep(raw_skills.get("domains")),
    )
    # A malformed response must not produce a resume with no skills at all.
    if not any((skills.languages, skills.frameworks, skills.tools, skills.domains)):
        skills = profile.skills

    achievements = [a for a in (data.get("achievements") or []) if a.strip()]
    return experience or profile.experience, projects, skills, achievements


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40]


_VOCAB_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#.-]{1,}")


def _candidate_vocab(profile: Profile) -> set[str]:
    """Every *technical* term the candidate can honestly claim.

    Two details matter:

    * multi-word skills are decomposed. "REST APIs" stored whole means a JD
      asking for "apis" reads as a skill the candidate lacks.
    * words from bullets are kept only when they are recognised technology.
      Taking every bullet word admitted "build", "communication", "critical"
      and "multiple" into the skill vocabulary, which then scored as matched
      ATS keywords and made coverage meaningless.
    """
    vocab: set[str] = set()
    for skill in profile.all_skills():
        vocab.add(skill.lower())
        vocab.update(w.lower() for w in _VOCAB_WORD.findall(skill))
    for bullet in profile.all_bullets():
        for word in _VOCAB_WORD.findall(bullet.lower()):
            if word in TECH_VOCAB:
                vocab.add(word)
    return {w for w in vocab if len(w) > 1}


def tailor_one(profile: Profile, job: Job, *, out_dir: Path | None = None) -> TailorResult:
    out_dir = out_dir or settings().resumes_dir
    facts = profile.facts()
    # Everything the candidate can honestly claim, used both to widen keyword
    # extraction and to decide which JD skills are the Tailor's to surface.
    profile_vocab = _candidate_vocab(profile)
    result = TailorResult(job_id=job.id or 0, ok=False)
    retry_note = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        result.attempts = attempt
        try:
            data = complete_json(
                build_prompt(profile, job, retry_note),
                system=_SYSTEM,
                model=MODEL_JUDGE,
                max_tokens=12000,
            )
        except LLMError as exc:
            result.error = str(exc)
            return result

        experience, projects, skills, achievements = _rebuild(profile, data)
        result.changes = (data.get("changes") or "").strip()

        # --- truthfulness gate ------------------------------------------
        generated = "\n".join(
            [*(b for e in experience for b in e.bullets),
             *(b for p in projects for b in p.bullets), *achievements]
        )
        violations = facts.check(generated)
        result.violations = violations
        if violations:
            log.warning("job %s attempt %s: %d fabrication(s)", job.id, attempt, len(violations))
            if attempt < MAX_ATTEMPTS:
                retry_note = (
                    "# CORRECTION REQUIRED\n"
                    "Your previous attempt introduced claims that are NOT in the master "
                    "profile. Remove or rephrase each of these, using only profile facts:\n"
                    + "\n".join(f"  - {v}" for v in violations[:25])
                )
                continue
            result.error = f"{len(violations)} unverifiable claims after {attempt} attempts"
            return result

        # --- render + ATS check -----------------------------------------
        # Shrink-to-fit before asking the model to cut again: a resume that is
        # marginally over one page is a typesetting problem, and spending an
        # LLM call to delete real material to fix 0.4pt of leading is worse
        # than tightening the layout.
        stamp = datetime.now().strftime("%Y%m%d")
        pdf_path = out_dir / f"{_slug(job.company)}_{_slug(job.title)}_{stamp}.pdf"
        _, density, pages = render_fitting(
            profile,
            pdf_path,
            max_pages=1,
            experience=experience,
            projects=projects,
            skills=skills,
            achievements=achievements,
        )
        if density is not DENSITY_STEPS[0]:
            log.info("job %s: tightened layout to fit (%s)", job.id, density["font"])

        report = verify_pdf(
            pdf_path,
            job.description or "",
            max_pages=1,
            company=job.company,
            # The profile's own skills count as vocabulary too, so a JD asking
            # for something the candidate genuinely has is always scoreable.
            candidate_vocab=profile_vocab,
        )
        result.pdf_path = pdf_path
        result.ats = report

        if report.passed:
            result.ok = True
            return result

        if attempt < MAX_ATTEMPTS:
            retry_note = _revision_note(report)
            continue

        # Report what actually failed. Naming coverage unconditionally was
        # misleading: this resume was rejected at 100% coverage purely for
        # running to two pages, and the message read "ATS coverage 100%".
        result.error = f"{'; '.join(report.failures)} (after {attempt} attempts)"
    return result


def _revision_note(report: ATSReport) -> str:
    """Tell the model which gate it missed, and what to do about it."""
    parts = ["# REVISION REQUIRED", "The rendered PDF did not pass:"]
    parts += [f"  - {f}" for f in report.failures]

    if report.pages > report.max_pages:
        parts.append(
            "\nCUT LENGTH FIRST. This is the binding constraint. Drop the least "
            "relevant bullets entirely rather than rewording them: keep 9-11 "
            "bullets in total across all roles and projects, at most 3 for any "
            "one role. A shorter resume that fits is "
            "strictly better than a complete one that does not."
        )
    if report.coverage < MIN_COVERAGE and report.missing:
        parts.append(
            "\nThese are already in the profile but did not reach the page - "
            "work them into the bullets you keep, WHERE THE UNDERLYING FACT "
            "ALREADY SUPPORTS THEM:\n  " + ", ".join(report.missing[:25])
        )
    return "\n".join(parts)


@dataclass
class TailorStats:
    attempted: int = 0
    succeeded: int = 0
    blocked_fabrication: int = 0
    failed_ats: int = 0
    failed_llm: int = 0

    def as_dict(self) -> dict:
        return {
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "blocked_fabrication": self.blocked_fabrication,
            "failed_ats": self.failed_ats,
            "failed_llm": self.failed_llm,
        }


def attach_original(profile: Profile, job: Job) -> TailorResult:
    """Attach your own resume PDF, unchanged, to this job.

    The "send my own resume" mode. No AI call and no rewriting: the file is
    copied as-is (so the Applied board keeps a record of exactly what went
    out, even after you upload a newer one) and only measured for keyword
    coverage, which is shown but never blocks it.
    """
    src = settings().uploaded_resume_path
    if not src.exists():
        return TailorResult(
            job_id=job.id or 0,
            ok=False,
            error="no resume PDF uploaded - add yours on the Filters page",
        )
    name = re.sub(r"[^A-Za-z0-9]+", "_", profile.identity.name or "").strip("_") or "Resume"
    dest = settings().resumes_dir / "original" / str(job.id) / f"{name}_Resume.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    try:
        report = verify_pdf(
            dest,
            job.description or "",
            max_pages=2,
            company=job.company,
            candidate_vocab=_candidate_vocab(profile),
        )
    except Exception as exc:  # noqa: BLE001 - coverage is informational here
        log.warning("job %s: could not measure your resume: %s", job.id, exc)
        report = None
    return TailorResult(
        job_id=job.id or 0,
        ok=True,
        pdf_path=dest,
        ats=report,
        changes="Your own resume, sent exactly as you uploaded it.",
    )


def run(*, limit: int = 5, job_id: int | None = None) -> tuple[TailorStats, list[TailorResult]]:
    profile = Profile.load(settings().profile_path)
    mode = resume_mode()
    threshold = float(targets().get("scoring", {}).get("apply_threshold", 3.75))
    stats = TailorStats()
    results: list[TailorResult] = []

    with session() as s:
        if job_id:
            ids = [job_id]
        else:
            # Score order alone would hand every slot to whichever company
            # happens to dominate the board; selection caps it per company.
            ids = pick_for_tailoring(s, threshold=threshold, limit=limit)

    for jid in ids:
        with session() as s:
            job = s.get(Job, jid)
            if job is None:
                continue
            stats.attempted += 1
            if mode == "original":
                result = attach_original(profile, job)
            else:
                result = tailor_one(profile, job)
            results.append(result)

            if result.ok and result.pdf_path:
                stats.succeeded += 1
                detail = result.ats.as_dict() if result.ats else {}
                # What the profile said when this was rendered, so a later run
                # can tell whether the PDF still reflects it. Lives in the
                # existing JSON column rather than a new one, which keeps this
                # free of a migration.
                if mode == "original":
                    detail["kind"] = "original"
                    detail["original_sha"] = original_fingerprint()
                else:
                    detail["profile_fingerprint"] = profile_fingerprint(profile)
                    detail["layout_fingerprint"] = layout_fingerprint()
                s.add(
                    Resume(
                        job_id=jid,
                        pdf_path=str(result.pdf_path),
                        ats_score=result.ats.coverage if result.ats else None,
                        ats_detail=detail,
                        truthfulness_passed=True,
                        changes_summary=result.changes,
                    )
                )
                job.status = JobStatus.READY
                coverage = f"ATS {result.ats.coverage:.0%}" if result.ats else "ATS not measured"
                job.status_reason = (
                    f"your resume attached, {coverage}"
                    if mode == "original"
                    else f"resume tailored, {coverage}"
                )
            elif result.violations:
                stats.blocked_fabrication += 1
                job.status = JobStatus.QUEUED_APPROVAL
                job.status_reason = f"manual review: {result.error}"
            elif result.error and not result.ats:
                stats.failed_llm += 1
                job.status_reason = f"tailor failed: {result.error}"
            else:
                stats.failed_ats += 1
                job.status = JobStatus.QUEUED_APPROVAL
                job.status_reason = f"manual review: {result.error}"

    return stats, results


# --------------------------------------------------------------------------
# Resumes written under an older layout
# --------------------------------------------------------------------------

# Jobs still in play: a stale resume on one of these could still be sent.
ACTIVE = (JobStatus.SCORED, JobStatus.READY, JobStatus.QUEUED_APPROVAL, JobStatus.AWAITING_REFERRAL)


def backfill_layout_records() -> dict[str, int]:
    """Record a layout verdict on resumes written before layouts were tracked.

    Those rows carry no layout fingerprint, so every staleness check had to
    read the PDF back. Judging each one once, by its section headings, and
    storing the verdict keeps the dashboard from re-reading PDFs on every page.
    """
    from sqlmodel import select

    from jobhunt.resume.render import layout_fingerprint, pdf_follows_layout

    current = layout_fingerprint()
    counts = {"current": 0, "outdated": 0, "unreadable": 0}
    with session() as s:
        for row in s.exec(select(Resume)).all():
            detail = dict(row.ats_detail or {})
            if (
                detail.get("layout_fingerprint")
                or detail.get("layout_outdated")
                or detail.get("kind") == "original"
            ):
                continue
            follows = pdf_follows_layout(Path(row.pdf_path))
            if follows is None:
                counts["unreadable"] += 1
                continue
            if follows:
                detail["layout_fingerprint"] = current
                counts["current"] += 1
            else:
                detail["layout_outdated"] = True
                counts["outdated"] += 1
            row.ats_detail = detail
            s.add(row)
    return counts


def outdated_jobs() -> list[tuple[int, str]]:
    """(job id, reason) for active jobs whose newest resume is out of date."""
    from sqlmodel import select

    from jobhunt.resume.render import resume_staleness

    profile = Profile.load(settings().profile_path)
    out = []
    with session() as s:
        rows = s.exec(
            select(Resume, Job)
            .join(Job, Job.id == Resume.job_id)
            .where(Job.status.in_(ACTIVE))
            .order_by(Resume.job_id, Resume.id.desc())
        ).all()
        seen: set[int] = set()
        for resume, job in rows:
            if job.id in seen:
                continue
            seen.add(job.id)
            reason = resume_staleness(resume.pdf_path, resume.ats_detail, profile)
            if reason:
                out.append((job.id, reason))
    return out


@dataclass
class RefreshStats:
    outdated: int = 0
    rewritten: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "outdated": self.outdated,
            "rewritten": self.rewritten,
            "failed": self.failed,
            "errors": self.errors[:5],
        }


def refresh_outdated() -> RefreshStats:
    """Write a fresh resume for every active job whose resume is out of date."""
    stats = RefreshStats()
    todo = outdated_jobs()
    stats.outdated = len(todo)
    for job_id, reason in todo:
        log.info("job %s: rewriting resume because %s", job_id, reason)
        try:
            _, results = run(limit=1, job_id=job_id)
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the rest
            stats.failed += 1
            stats.errors.append(f"job {job_id}: {exc}")
            continue
        if results and results[0].ok:
            stats.rewritten += 1
        else:
            stats.failed += 1
            stats.errors.append(f"job {job_id}: {results[0].error if results else 'no result'}")
    return stats
