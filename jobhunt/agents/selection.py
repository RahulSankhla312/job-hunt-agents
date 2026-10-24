"""Which jobs get worked on next, and how many of them per company.

Pure score order is the wrong queue when one employer dominates the board.
Amazon alone accounts for 40 of the 55 India-eligible roles above the bar, and
every one of those postings lands in the same applicant-tracking system in
front of the same recruiting org. Forty applications from one candidate reads
as volume rather than interest, and each marginal one costs a tailored resume
for no extra chance of a callback.

So the queue is score-ordered *within* a per-company budget: the best few roles
at each company get the work, the rest wait until one of them resolves. A cap
of 0 (or a missing key) restores the old unbounded behaviour.

The budget counts **committed** jobs - anything the pipeline has already spent
a resume on or put in front of a recruiter. A job that gets rejected or
archived frees its slot again, which is the behaviour you want: the cap limits
how many live applications you have at one company, not how many you may ever
send there.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from sqlmodel import Session, select

from jobhunt.config import targets
from jobhunt.store.models import Job, JobStatus

log = logging.getLogger(__name__)

# Statuses that consume a company slot. TAILORING is included so two runs
# overlapping in time cannot both spend the same budget.
COMMITTED = (
    JobStatus.TAILORING,
    JobStatus.READY,
    JobStatus.AWAITING_REFERRAL,
    JobStatus.QUEUED_APPROVAL,
    JobStatus.APPLIED,
)


def company_cap() -> int | None:
    """How many live applications one company may hold. ``None`` = unlimited."""
    raw = targets().get("limits", {}).get("applications_per_company", 0)
    try:
        cap = int(raw)
    except (TypeError, ValueError):
        log.warning("applications_per_company is not a number: %r - treating as unlimited", raw)
        return None
    return cap if cap > 0 else None


def _key(company: str | None) -> str:
    return (company or "").strip().casefold()


def committed_by_company(s: Session) -> dict[str, int]:
    """Slots already spent, keyed by normalised company name."""
    counts: dict[str, int] = defaultdict(int)
    for company in s.exec(select(Job.company).where(Job.status.in_(COMMITTED))).all():
        counts[_key(company)] += 1
    return dict(counts)


def pick_for_tailoring(s: Session, *, threshold: float, limit: int) -> list[int]:
    """The next jobs worth a tailored resume, best first, cap respected.

    Walks the scored backlog in score order and skips any job whose company has
    no budget left, rather than stopping at the first one - otherwise a wall of
    Amazon postings at the top would starve everything beneath it.
    """
    rows = s.exec(
        select(Job.id, Job.company)
        .where(Job.status == JobStatus.SCORED, Job.score >= threshold)
        .order_by(Job.score.desc())
    ).all()

    cap = company_cap()
    if cap is None:
        return [row[0] for row in rows[:limit]]

    spent = committed_by_company(s)
    picked: list[int] = []
    deferred = 0

    for job_id, company in rows:
        if len(picked) >= limit:
            break
        key = _key(company)
        if spent.get(key, 0) >= cap:
            deferred += 1
            continue
        spent[key] = spent.get(key, 0) + 1
        picked.append(job_id)

    if deferred:
        log.info("company cap %d deferred %d scored job(s)", cap, deferred)
    return picked


def over_company_cap(s: Session, job: Job) -> bool:
    """Is this ready job outside its company's top-``cap`` by score?

    A second check at submit time, because a job can reach READY without going
    through :func:`pick_for_tailoring` - tailored by hand from the dashboard,
    or queued before the cap was lowered.
    """
    cap = company_cap()
    if cap is None or job.id is None:
        return False

    siblings = s.exec(
        select(Job.id)
        .where(Job.company == job.company, Job.status.in_(COMMITTED))
        .order_by(Job.score.desc(), Job.id)
    ).all()
    allowed = set(siblings[:cap])
    return job.id not in allowed
