"""SQLModel schema.

One SQLite file holds the whole pipeline. Every row that an agent acts on
carries the *reason* for the decision, not just the outcome - without that the
filters are impossible to tune, and a silently-dropped good job is invisible.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import Column, Index, Text
from sqlmodel import JSON, Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class JobStatus(str, Enum):
    NEW = "new"                    # sourced, not yet filtered
    REJECTED_GATE = "rejected_gate"  # failed a deterministic filter
    GATED = "gated"                # passed the gate, awaiting the Analyst
    SCORED = "scored"              # Analyst has scored it
    ARCHIVED = "archived"          # scored below threshold
    TAILORING = "tailoring"        # resume being generated
    READY = "ready"                # resume done, awaiting referral/apply
    AWAITING_REFERRAL = "awaiting_referral"
    QUEUED_APPROVAL = "queued_approval"
    APPLIED = "applied"
    FAILED = "failed"


class Eligibility(str, Enum):
    INDIA_ELIGIBLE = "india_eligible"
    SPONSORSHIP_OFFERED = "sponsorship_offered"
    US_ONLY_NO_SPONSORSHIP = "us_only_no_sponsorship"
    UNKNOWN = "unknown"


class LevelBand(str, Enum):
    DIRECT = "direct"
    STRETCH = "stretch"
    AI_TRACK = "ai_track"
    REJECT = "reject"


class ApprovalStatus(str, Enum):
    QUEUED = "queued"
    APPROVED = "approved"
    REJECTED = "rejected"
    SENT = "sent"
    REPLIED = "replied"
    EXPIRED = "expired"


class Channel(str, Enum):
    LINKEDIN_DM = "linkedin_dm"
    EMAIL = "email"


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------


class Job(SQLModel, table=True):
    __tablename__ = "job"

    id: int | None = Field(default=None, primary_key=True)

    # Provenance
    source: str = Field(index=True)          # greenhouse | ashby | remotive | ...
    source_id: str = Field(index=True)       # id within that source
    url: str
    ats: str | None = Field(default=None)    # which ATS hosts the application
    ats_slug: str | None = None

    # Posting
    company: str = Field(index=True)
    title: str
    location: str | None = None
    remote: bool = False
    description: str | None = Field(default=None, sa_column=Column(Text))
    posted_at: datetime | None = None

    # Deterministic filter output - each with its reason
    dedupe_hash: str = Field(index=True)
    eligibility: Eligibility = Field(default=Eligibility.UNKNOWN, index=True)
    eligibility_reason: str | None = None
    level_band: LevelBand = Field(default=LevelBand.DIRECT, index=True)
    level_reason: str | None = None

    # Compensation - published values win over estimates
    comp_min: float | None = None
    comp_max: float | None = None
    comp_currency: str | None = None
    comp_is_estimated: bool = False
    comp_reason: str | None = None

    # Analyst output
    score: float | None = Field(default=None, index=True)
    score_detail: dict = Field(default_factory=dict, sa_column=Column(JSON))
    score_summary: str | None = Field(default=None, sa_column=Column(Text))

    status: JobStatus = Field(default=JobStatus.NEW, index=True)
    status_reason: str | None = None

    first_seen: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    __table_args__ = (
        Index("ix_job_source_unique", "source", "source_id", unique=True),
        Index("ix_job_status_score", "status", "score"),
    )


class Contact(SQLModel, table=True):
    __tablename__ = "contact"

    id: int | None = Field(default=None, primary_key=True)
    company: str = Field(index=True)
    name: str
    title: str | None = None
    linkedin_url: str | None = None
    email: str | None = Field(default=None, index=True)

    # How we found them and how much we trust the address
    discovery_source: str | None = None      # linkedin | cse | company_site | github
    email_pattern: str | None = None         # first.last | first | flast
    email_verified: bool = False
    confidence: float = 0.0                  # only >= 0.7 is emailed

    # Referral relevance
    connection_degree: int | None = None      # 1 | 2 | 3
    shared_context: str | None = None         # "IIIT Pune", "Northwind", ...

    created_at: datetime = Field(default_factory=utcnow)


class Resume(SQLModel, table=True):
    __tablename__ = "resume"

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True)
    pdf_path: str
    ats_score: float | None = None            # keyword coverage 0-1
    ats_detail: dict = Field(default_factory=dict, sa_column=Column(JSON))
    truthfulness_passed: bool = False
    changes_summary: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow)


class Referral(SQLModel, table=True):
    __tablename__ = "referral"

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True)
    contact_id: int = Field(foreign_key="contact.id", index=True)
    message: str = Field(sa_column=Column(Text))
    status: ApprovalStatus = Field(default=ApprovalStatus.QUEUED, index=True)
    sla_deadline: datetime | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    sent_at: datetime | None = None
    replied_at: datetime | None = None


class Outreach(SQLModel, table=True):
    """A cold email or LinkedIn message to a recruiter / hiring manager."""

    __tablename__ = "outreach"

    id: int | None = Field(default=None, primary_key=True)
    job_id: int | None = Field(default=None, foreign_key="job.id", index=True)
    contact_id: int = Field(foreign_key="contact.id", index=True)
    channel: Channel = Field(default=Channel.EMAIL)
    subject: str | None = None
    body: str = Field(sa_column=Column(Text))
    status: ApprovalStatus = Field(default=ApprovalStatus.QUEUED, index=True)
    followup_stage: int = 0                    # 0 = initial, 1 = d+3, 2 = d+7
    created_at: datetime = Field(default_factory=utcnow)
    sent_at: datetime | None = None
    replied_at: datetime | None = None


class Application(SQLModel, table=True):
    __tablename__ = "application"

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True)
    resume_id: int | None = Field(default=None, foreign_key="resume.id")
    referral_id: int | None = Field(default=None, foreign_key="referral.id")
    method: str = "ats_auto"                   # ats_auto | ats_manual | email
    status: str = "submitted"                  # submitted | confirmed | rejected | interview | offer
    confirmation_email_id: str | None = None   # Gmail message id, from Tracker
    screenshot_path: str | None = None
    notes: str | None = Field(default=None, sa_column=Column(Text))
    applied_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class RunLog(SQLModel, table=True):
    """One row per scheduled run - the funnel counts live here."""

    __tablename__ = "run_log"

    id: int | None = Field(default=None, primary_key=True)
    run_type: str                              # morning | evening | manual
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    ok: bool = True
    stats: dict = Field(default_factory=dict, sa_column=Column(JSON))
    error: str | None = Field(default=None, sa_column=Column(Text))


class RateLimit(SQLModel, table=True):
    """Daily counters that enforce the caps in ``config/targets.yaml``."""

    __tablename__ = "rate_limit"

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True)               # cold_email | application | linkedin_view
    day: str = Field(index=True)               # YYYY-MM-DD, Asia/Kolkata
    count: int = 0

    __table_args__ = (Index("ix_rate_key_day", "key", "day", unique=True),)
