"""Tracker - read application replies and move the pipeline forward.

Classification is regex-first and LLM-second. Most ATS mail is templated, so
patterns settle the great majority for free; only genuinely ambiguous messages
cost an LLM call.

Matching a reply to an application is done on the company, never on the
subject line alone - "Software Engineer" appears in mail from a dozen employers
and would attach a Google rejection to an Amazon application.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz
from sqlmodel import select

from jobhunt.llm.adapter import MODEL_BULK, LLMError, complete_json
from jobhunt.sources.gmail import GmailUnavailable, Message, job_related_query, search
from jobhunt.store.db import session
from jobhunt.store.models import Application, ApprovalStatus, Job, Outreach, Referral

log = logging.getLogger(__name__)

# Ordered: the first match wins, so the decisive outcomes are checked before
# the generic acknowledgements they often accompany.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("offer", re.compile(r"\b(offer letter|pleased to offer|extend an offer|offer of employment)\b", re.I)),
    ("rejected", re.compile(
        r"\b(unfortunately|we regret|not (?:be )?(?:moving|proceeding) forward|"
        r"decided not to (?:move|proceed)|other candidates|not selected|"
        r"will not be progressing|unable to offer)\b", re.I)),
    ("interview", re.compile(
        r"\b(schedule (?:a|an|your)|interview|phone screen|technical screen|"
        r"availability|next steps?|onsite|coding (?:round|challenge)|"
        r"assessment|hackerrank|codesignal)\b", re.I)),
    ("confirmed", re.compile(
        r"\b(thank you for applying|application (?:received|submitted|confirmation)|"
        r"we(?:'ve| have) received your application|successfully applied)\b", re.I)),
]

STATUS_RANK = {"submitted": 0, "confirmed": 1, "interview": 2, "offer": 3, "rejected": 3}


@dataclass
class TrackStats:
    messages_read: int = 0
    matched: int = 0
    updated: int = 0
    replies_marked: int = 0
    llm_calls: int = 0
    unmatched: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "messages_read": self.messages_read,
            "matched": self.matched,
            "updated": self.updated,
            "replies_marked": self.replies_marked,
            "llm_calls": self.llm_calls,
            "unmatched": self.unmatched[:20],
        }


def classify(message: Message) -> tuple[str, bool]:
    """Return ``(status, used_llm)``.

    Falls back to the model only when no pattern matches, which on templated
    ATS mail is rare.
    """
    blob = f"{message.subject}\n{message.snippet}"
    for status, pattern in PATTERNS:
        if pattern.search(blob):
            return status, False

    try:
        data = complete_json(
            "Classify this job-application email into exactly one of: "
            "confirmed, interview, rejected, offer, other.\n\n"
            f"From: {message.sender}\nSubject: {message.subject}\n{message.snippet}\n\n"
            'Return {"status": "<one of those>"}',
            model=MODEL_BULK,
            max_tokens=100,
        )
        status = str(data.get("status", "other")).strip().lower()
        return (status if status in STATUS_RANK or status == "other" else "other"), True
    except LLMError as exc:
        log.debug("classification fell back to 'other': %s", exc)
        return "other", False


def _domain(email: str) -> str:
    return email.split("@")[-1].lower().split(">")[0].strip()


def match_application(message: Message, applications: list[tuple[Application, Job]]) -> Application | None:
    """Find which application this message is about.

    Company-first, because subject lines are not distinctive: half of all
    application mail says "Software Engineer", and matching on that alone
    attaches replies to the wrong employer.
    """
    sender_domain = _domain(message.sender)
    blob = f"{message.subject} {message.snippet}".lower()

    best: tuple[float, Application] | None = None
    for application, job in applications:
        company = (job.company or "").lower()
        if not company:
            continue

        score = 0.0
        # The company name appearing in the sender domain is the strongest
        # signal available ("no-reply@rubrik.com").
        if company.replace(" ", "") in sender_domain.replace("-", ""):
            score += 5.0
        if company in blob:
            score += 3.0
        else:
            score += fuzz.partial_ratio(company, blob) / 100.0
        if job.title and job.title.lower()[:28] in blob:
            score += 1.5

        if score >= 3.0 and (best is None or score > best[0]):
            best = (score, application)

    return best[1] if best else None


def run(*, days: int = 30, limit: int = 60) -> TrackStats:
    stats = TrackStats()

    try:
        messages = search(job_related_query(days), limit=limit)
    except GmailUnavailable as exc:
        log.warning("gmail unavailable: %s", exc)
        return stats
    stats.messages_read = len(messages)

    with session() as s:
        pairs = [
            (application, job)
            for application in s.exec(select(Application)).all()
            if (job := s.get(Job, application.job_id)) is not None
        ]

        for message in messages:
            application = match_application(message, pairs)
            if application is None:
                stats.unmatched.append(f"{message.sender}: {message.subject[:60]}")
                continue
            stats.matched += 1

            status, used_llm = classify(message)
            stats.llm_calls += int(used_llm)
            if status == "other":
                continue

            tracked = s.get(Application, application.id)
            if tracked is None:
                continue
            # Never walk a pipeline backwards: a templated "we received your
            # application" often arrives after a real interview invitation.
            if STATUS_RANK.get(status, 0) > STATUS_RANK.get(tracked.status, 0):
                tracked.status = status
                tracked.confirmation_email_id = message.id
                tracked.notes = f"{message.sender}: {message.subject[:120]}"
                stats.updated += 1

        # A recruiter replying by email also answers any outreach or referral
        # still waiting on them.
        senders = {_domain(m.sender) for m in messages}
        for referral in s.exec(
            select(Referral).where(Referral.status == ApprovalStatus.SENT)
        ).all():
            job = s.get(Job, referral.job_id)
            if job and job.company and job.company.lower().replace(" ", "") in "".join(senders):
                referral.status = ApprovalStatus.REPLIED
                stats.replies_marked += 1

        for item in s.exec(select(Outreach).where(Outreach.status == ApprovalStatus.SENT)).all():
            job = s.get(Job, item.job_id) if item.job_id else None
            if job and job.company and job.company.lower().replace(" ", "") in "".join(senders):
                item.status = ApprovalStatus.REPLIED
                stats.replies_marked += 1

    return stats
