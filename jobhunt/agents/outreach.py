"""Outreach - write to a recruiter, and follow up once or twice.

Capped at 15 personalised emails a day, enforced against the database counter.
That cap is not timidity: blasting recruiters from a personal Gmail gets the
account spam-flagged, which would break the Tracker *and* real correspondence.
Fifteen genuinely specific emails beat two hundred templated ones on reply rate
anyway.

Follow-ups stop the moment anyone replies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlmodel import select

from jobhunt.config import settings, targets
from jobhunt.llm.adapter import MODEL_JUDGE, LLMError, complete_json
from jobhunt.resume.profile import Profile
from jobhunt.sources.base import Fetcher
from jobhunt.sources.contacts import Contact as FoundContact
from jobhunt.sources.contacts import enrich, find_people
from jobhunt.sources.gmail import GmailUnavailable, send
from jobhunt.store.db import consume_quota, remaining_quota, session
from jobhunt.store.models import ApprovalStatus, Channel, Contact, Job, JobStatus, Outreach

log = logging.getLogger(__name__)

MIN_CONFIDENCE = 0.7
FOLLOWUP_DAYS = (3, 7)
MAX_WORDS = 130

_SYSTEM = """You write one short cold email to a recruiter or hiring manager
about a specific open role.

Hard rules:
- 130 words maximum, including the sign-off.
- Subject line: the role, and one concrete hook. No "Application" alone, no
  clickbait, no exclamation marks.
- Open with why you are writing to *this* company about *this* role. Never
  "I hope this email finds you well".
- Two sentences maximum of evidence, drawn only from the facts given. Never
  invent experience, numbers or employers.
- One clear ask: a short conversation, or the right person to talk to.
- Sign off with the candidate's name. No "Best regards, looking forward to
  hearing from you at your earliest convenience".
- Plain text. No markdown, no bullet points.

Return JSON: {"subject": "<line>", "body": "<email>"}"""

_FOLLOWUP_SYSTEM = """You write a follow-up to an unanswered cold email.

- 60 words maximum.
- Reference the original briefly; do not repeat its content.
- Add one new, genuinely useful detail if the facts given support one.
- Make it easy to ignore. No guilt, no "just bumping this to the top of your
  inbox", no third-person urgency.
- Sign off with the candidate's name.

Return JSON: {"subject": "<line>", "body": "<email>"}"""


@dataclass
class OutreachStats:
    jobs_considered: int = 0
    contacts_found: int = 0
    drafts_queued: int = 0
    sent: int = 0
    followups_sent: int = 0
    skipped_low_confidence: int = 0
    quota_left: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "jobs_considered": self.jobs_considered,
            "contacts_found": self.contacts_found,
            "drafts_queued": self.drafts_queued,
            "sent": self.sent,
            "followups_sent": self.followups_sent,
            "skipped_low_confidence": self.skipped_low_confidence,
            "quota_left": self.quota_left,
            "errors": self.errors,
        }


def _domain_for(job: Job) -> str | None:
    """Best guess at the company's mail domain, from its own posting URL."""
    from urllib.parse import urlparse

    host = urlparse(job.url or "").netloc.lower()
    # ATS-hosted URLs say nothing about the employer's own domain.
    if any(
        marker in host
        for marker in ("greenhouse", "ashby", "lever", "myworkdayjobs", "amazon.jobs")
    ):
        return None
    return host[4:] if host.startswith("www.") else host or None


def build_prompt(profile: Profile, job: Job, contact: FoundContact) -> str:
    current = profile.experience[0] if profile.experience else None
    highlights = "\n".join(f"- {b}" for b in (current.bullets[:3] if current else []))

    return f"""# CANDIDATE

{profile.identity.name}, {profile.years_text()} years' experience.
{profile.current_role() or "Looking for a first full-time role."}
LinkedIn: {profile.identity.linkedin}   GitHub: {profile.identity.github}

Shipped:
{highlights}

Skills: {", ".join(profile.all_skills()[:14])}

# RECIPIENT

{contact.name} - {contact.title}
{contact.company}

# ROLE

{job.title} ({job.location})
{job.url}

Why it fits: {job.score_summary or "strong overlap with what the candidate ships"}

Write the email."""


def draft(profile: Profile, job: Job, contact: FoundContact) -> tuple[str, str]:
    data = complete_json(
        build_prompt(profile, job, contact),
        system=_SYSTEM,
        model=MODEL_JUDGE,
        max_tokens=1500,
    )
    subject = (data.get("subject") or f"{job.title} - {profile.identity.name}").strip()
    body = (data.get("body") or "").strip()

    words = body.split()
    if len(words) > MAX_WORDS + 30:
        body = " ".join(words[:MAX_WORDS]).rstrip(",;:") + f"\n\n{profile.identity.name}"
        log.info("trimmed an over-long draft for job %s", job.id)
    return subject, body


def run(*, limit: int = 5, verify: bool = True) -> OutreachStats:
    """Find recruiters for ready jobs and queue a personalised email each."""
    stats = OutreachStats()
    stats.quota_left = remaining_quota("cold_email")
    profile = Profile.load(settings().profile_path)

    with session() as s:
        job_ids = list(
            s.exec(
                select(Job.id)
                .where(Job.status.in_((JobStatus.READY, JobStatus.APPLIED)))
                .order_by(Job.score.desc())
                .limit(limit)
            ).all()
        )

    with Fetcher() as fetcher:
        for job_id in job_ids:
            with session() as s:
                job = s.get(Job, job_id)
            if job is None:
                continue
            stats.jobs_considered += 1

            try:
                people = find_people(fetcher, job.company)
            except Exception as exc:  # noqa: BLE001 - one company must not abort the run
                stats.errors.append(f"{job.company}: {exc}")
                continue

            domain = _domain_for(job)
            if domain:
                people = enrich(fetcher, people, domain, verify=verify)
            stats.contacts_found += len(people)

            for person in people[:2]:
                if person.confidence < MIN_CONFIDENCE or not person.email:
                    # Not wasted: queue it as a LinkedIn message instead.
                    stats.skipped_low_confidence += 1
                    if person.linkedin_url:
                        _queue(job, person, "", "", Channel.LINKEDIN_DM)
                        stats.drafts_queued += 1
                    continue

                try:
                    subject, body = draft(profile, job, person)
                except LLMError as exc:
                    stats.errors.append(f"job {job_id}: {exc}")
                    continue
                if body:
                    _queue(job, person, subject, body, Channel.EMAIL)
                    stats.drafts_queued += 1

    return stats


def _queue(job: Job, person: FoundContact, subject: str, body: str, channel: Channel) -> None:
    with session() as s:
        contact = None
        if person.email:
            contact = s.exec(select(Contact).where(Contact.email == person.email)).first()
        if contact is None and person.linkedin_url:
            contact = s.exec(
                select(Contact).where(Contact.linkedin_url == person.linkedin_url)
            ).first()
        if contact is None:
            contact = Contact(
                company=job.company,
                name=person.name,
                title=person.title,
                email=person.email,
                linkedin_url=person.linkedin_url,
                discovery_source=person.source,
                email_pattern=person.pattern,
                email_verified=person.verified,
                confidence=person.confidence,
            )
            s.add(contact)
            s.flush()

        exists = s.exec(
            select(Outreach).where(
                Outreach.job_id == job.id, Outreach.contact_id == contact.id
            )
        ).first()
        if exists:
            return

        s.add(
            Outreach(
                job_id=job.id,
                contact_id=contact.id,
                channel=channel,
                subject=subject or None,
                body=body or "(queued for LinkedIn - no verified email)",
                status=ApprovalStatus.QUEUED,
            )
        )


def send_approved() -> OutreachStats:
    """Send everything you have approved, inside the daily cap."""
    stats = OutreachStats()
    cfg = targets().get("autonomy", {})
    stats.quota_left = remaining_quota("cold_email")

    if cfg.get("shadow_mode", True):
        log.info("shadow mode - not sending")
        return stats

    with session() as s:
        approved = s.exec(
            select(Outreach).where(
                Outreach.status == ApprovalStatus.APPROVED,
                Outreach.channel == Channel.EMAIL,
            )
        ).all()

        for item in approved:
            if not consume_quota("cold_email"):
                log.warning("daily cold-email cap reached")
                break
            contact = s.get(Contact, item.contact_id)
            if contact is None or not contact.email:
                continue
            try:
                send(contact.email, item.subject or "Hello", item.body)
            except GmailUnavailable as exc:
                stats.errors.append(str(exc))
                break
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(f"{contact.email}: {exc}")
                continue
            item.status = ApprovalStatus.SENT
            item.sent_at = datetime.now(UTC)
            stats.sent += 1

    stats.quota_left = remaining_quota("cold_email")
    return stats


def send_followups() -> OutreachStats:
    """Day 3 and day 7 nudges, stopping the moment anyone replies."""
    stats = OutreachStats()
    cfg = targets().get("autonomy", {})
    if cfg.get("shadow_mode", True) or not cfg.get("auto_followups", True):
        return stats

    profile = Profile.load(settings().profile_path)
    now = datetime.now(UTC)

    with session() as s:
        sent = s.exec(
            select(Outreach).where(
                Outreach.status == ApprovalStatus.SENT,
                Outreach.channel == Channel.EMAIL,
                Outreach.replied_at.is_(None),
                Outreach.followup_stage < len(FOLLOWUP_DAYS),
            )
        ).all()

        for item in sent:
            if item.sent_at is None:
                continue
            due_after = FOLLOWUP_DAYS[item.followup_stage]
            if now - item.sent_at < timedelta(days=due_after):
                continue
            if not consume_quota("cold_email"):
                break

            contact = s.get(Contact, item.contact_id)
            job = s.get(Job, item.job_id) if item.job_id else None
            if contact is None or not contact.email or job is None:
                continue

            try:
                data = complete_json(
                    f"Original subject: {item.subject}\n"
                    f"Original email:\n{item.body}\n\n"
                    f"Role: {job.title} at {job.company}\n"
                    f"Candidate: {profile.identity.name}\n\n"
                    f"This is follow-up number {item.followup_stage + 1}.",
                    system=_FOLLOWUP_SYSTEM,
                    model=MODEL_JUDGE,
                    max_tokens=800,
                )
                send(
                    contact.email,
                    data.get("subject") or f"Re: {item.subject}",
                    (data.get("body") or "").strip(),
                )
            except (LLMError, GmailUnavailable) as exc:
                stats.errors.append(str(exc))
                continue

            item.followup_stage += 1
            item.sent_at = now
            stats.followups_sent += 1

    return stats
