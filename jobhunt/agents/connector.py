"""Connector - find a referral path, draft the ask, queue it for you to send.

The agent does the research and the writing. It does not send. LinkedIn
prohibits automated messaging and a restricted account would cost far more than
any single referral is worth, so the last step is a human one: you read the
draft, edit if you want, and send it from your own session.

Each referral opens a 24-hour SLA. The evening sweep applies it: a reply means
apply *with* the referral, silence means apply anyway. A job never stalls
waiting on a human.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlmodel import select

from jobhunt.config import settings, targets
from jobhunt.llm.adapter import MODEL_JUDGE, LLMError, complete_json
from jobhunt.resume.profile import Profile
from jobhunt.sources.browser.linkedin import Person, search_people
from jobhunt.sources.browser.session import SessionError, browser
from jobhunt.store.db import session
from jobhunt.store.models import ApprovalStatus, Contact, Job, JobStatus, Referral

log = logging.getLogger(__name__)

MAX_WORDS = 65

_SYSTEM = """You write one short LinkedIn message asking for a referral.

Hard rules:
- 65 words maximum. Long messages from strangers do not get read.
- Name one specific, true thing the two people share, if there is one. Do not
  invent a connection or imply you have met.
- State the role and one concrete reason this candidate fits it, drawn only
  from the facts given. Never invent experience.
- Ask one clear question: would they be willing to refer, or point to the right
  person. Make it easy to say no.
- No flattery, no "I hope this finds you well", no "I am passionate about".
- Write as the candidate, first person, plain sentences.

Return JSON: {"message": "<the message>", "rationale": "<one line: why this
person and this angle>"}"""


@dataclass
class ConnectorStats:
    jobs_considered: int = 0
    searches: int = 0
    contacts_found: int = 0
    drafts_queued: int = 0
    skipped_no_contacts: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "jobs_considered": self.jobs_considered,
            "searches": self.searches,
            "contacts_found": self.contacts_found,
            "drafts_queued": self.drafts_queued,
            "skipped_no_contacts": self.skipped_no_contacts,
            "errors": self.errors,
        }


def profile_anchors(profile: Profile) -> list[str]:
    """Things the candidate genuinely shares with someone - never invented."""
    anchors: list[str] = []
    for edu in profile.education:
        if edu.institution:
            anchors.append(edu.institution)
            # "Indian Institute of Information Technology, Pune" is rarely
            # written out in a headline; the short form is what people use.
            if "indian institute" in edu.institution.lower():
                anchors.append("IIIT")
            if "," in edu.institution:
                anchors.append(edu.institution.split(",")[-1].strip())
    anchors += [e.company for e in profile.experience if e.company]
    anchors += [s for s in profile.all_skills() if len(s) > 3][:12]
    return list(dict.fromkeys(a for a in anchors if a))


def build_prompt(profile: Profile, job: Job, person: Person) -> str:
    current = profile.experience[0] if profile.experience else None
    highlights = "\n".join(f"- {b}" for b in (current.bullets[:4] if current else []))

    return f"""# WHO IS WRITING

{profile.identity.name}, {profile.years_text()} years' experience.
Currently {profile.current_role() or "looking for a first full-time role"}.

What the candidate has actually shipped there:
{highlights}

Skills: {", ".join(profile.all_skills()[:14])}

# WHO HE IS WRITING TO

{person.name} - {person.headline}
Company: {person.company}
Connection degree: {person.degree or "unknown"}
Genuinely shared with the candidate: {", ".join(person.shared_context) or "nothing identified"}

# THE ROLE

{job.title} at {job.company} ({job.location})
{job.url}

Why it fits, from the scoring pass: {job.score_summary or "strong skills overlap"}

Write the message."""


def draft_message(profile: Profile, job: Job, person: Person) -> tuple[str, str]:
    data = complete_json(
        build_prompt(profile, job, person),
        system=_SYSTEM,
        model=MODEL_JUDGE,
        max_tokens=1200,
    )
    message = (data.get("message") or "").strip()
    rationale = (data.get("rationale") or "").strip()

    words = message.split()
    if len(words) > MAX_WORDS + 15:
        # The cap is the whole point of the message working; enforce it rather
        # than trusting the instruction.
        message = " ".join(words[:MAX_WORDS]).rstrip(",;:") + "..."
        log.info("trimmed an over-long draft for job %s", job.id)
    return message, rationale


def _store(job: Job, person: Person, message: str, sla_hours: int) -> None:
    with session() as s:
        contact = s.exec(
            select(Contact).where(Contact.linkedin_url == person.profile_url)
        ).first()
        if contact is None:
            contact = Contact(
                company=job.company,
                name=person.name,
                title=person.headline,
                linkedin_url=person.profile_url,
                discovery_source="linkedin",
                connection_degree=person.degree,
                shared_context=", ".join(person.shared_context) or None,
                confidence=min(1.0, person.referral_score() / 7.0),
            )
            s.add(contact)
            s.flush()

        already = s.exec(
            select(Referral).where(
                Referral.job_id == job.id, Referral.contact_id == contact.id
            )
        ).first()
        if already:
            return

        s.add(
            Referral(
                job_id=job.id,
                contact_id=contact.id,
                message=message,
                status=ApprovalStatus.QUEUED,
                sla_deadline=datetime.now(UTC) + timedelta(hours=sla_hours),
            )
        )
        tracked = s.get(Job, job.id)
        if tracked is not None:
            tracked.status = JobStatus.AWAITING_REFERRAL
            tracked.status_reason = f"referral drafted for {person.name}"


def run(*, limit: int = 5, per_job: int = 1, headless: bool = True) -> ConnectorStats:
    """Draft referral asks for the top tailored jobs."""
    cfg = targets().get("limits", {})
    sla_hours = int(cfg.get("referral_sla_hours", 24))
    stats = ConnectorStats()

    profile = Profile.load(settings().profile_path)
    anchors = profile_anchors(profile)

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
        log.info("no jobs are tailored and ready")
        return stats

    with browser(headless=headless) as sess:
        for job_id in job_ids:
            with session() as s:
                job = s.get(Job, job_id)
            if job is None:
                continue
            stats.jobs_considered += 1

            try:
                people = search_people(sess, job.company, anchors=anchors, limit=per_job * 3)
                stats.searches += 1
            except SessionError as exc:
                stats.errors.append(str(exc))
                log.warning("stopping: %s", exc)
                break

            if not people:
                stats.skipped_no_contacts += 1
                continue
            stats.contacts_found += len(people)

            for person in people[:per_job]:
                try:
                    message, _ = draft_message(profile, job, person)
                except LLMError as exc:
                    stats.errors.append(f"job {job_id}: {exc}")
                    continue
                if message:
                    _store(job, person, message, sla_hours)
                    stats.drafts_queued += 1

    return stats


def expire_overdue() -> int:
    """Mark referrals whose SLA has passed, so the job can proceed.

    Called by the evening sweep: a referral that has not been answered by the
    deadline must not hold an application hostage.
    """
    now = datetime.now(UTC)
    moved = 0
    with session() as s:
        overdue = s.exec(
            select(Referral).where(
                Referral.status.in_((ApprovalStatus.QUEUED, ApprovalStatus.SENT)),
                Referral.sla_deadline.is_not(None),
                Referral.sla_deadline < now,
                Referral.replied_at.is_(None),
            )
        ).all()
        for referral in overdue:
            referral.status = ApprovalStatus.EXPIRED
            job = s.get(Job, referral.job_id)
            if job is not None and job.status is JobStatus.AWAITING_REFERRAL:
                job.status = JobStatus.READY
                job.status_reason = "referral SLA expired - proceeding without one"
            moved += 1
    return moved
