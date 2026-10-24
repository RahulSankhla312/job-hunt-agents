"""Approval dashboard.

One FastAPI process, server-rendered. No build step, and it reads fine on a
phone over the LAN - the tiered-autonomy design assumes the queue gets cleared
in a couple of minutes a day from wherever you happen to be.

Bound to 127.0.0.1 by default. The database holds an unredacted copy of your
job search; it does not belong on a shared network without a deliberate
decision.

Pages live in three routers: this module (queue, jobs, applications), the
onboarding router (profile, filters, connections) and the runs router.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from sqlmodel import func, select

from jobhunt.agents.analyst import MANUAL_REJECT_PREFIX
from jobhunt.config import ROOT, profile_exists, settings, targets
from jobhunt.store.db import init_db, session
from jobhunt.store.models import (
    Application,
    ApprovalStatus,
    Contact,
    Eligibility,
    Job,
    JobStatus,
    Outreach,
    Referral,
    Resume,
)
from jobhunt.web.common import BLOCKED_PREFIXES, ctx, filters_summary, is_blocked, templates

log = logging.getLogger(__name__)

_ctx = ctx
__all__ = ["BLOCKED_PREFIXES", "app", "is_blocked", "serve"]


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    from jobhunt.graph.runner import runner

    init_db()
    read_only = settings().jobhunt_read_only
    if not read_only:
        threading.Thread(target=_backfill_resumes, name="resume-backfill", daemon=True).start()
    if not read_only and targets().get("schedule", {}).get("enabled") and profile_exists():
        try:
            runner.start_scheduler()
        except Exception:  # noqa: BLE001 - the dashboard must come up regardless
            log.exception("could not start the scheduler")
    yield
    runner.stop_scheduler()


def _backfill_resumes() -> None:
    """Judge pre-fingerprint resumes once, so pages never re-read PDFs."""
    from jobhunt.agents.tailor import backfill_layout_records

    try:
        counts = backfill_layout_records()
        if counts["current"] or counts["outdated"]:
            log.info("resume layout backfill: %s", counts)
    except Exception:  # noqa: BLE001 - an optimisation, never fatal
        log.exception("resume layout backfill failed")


app = FastAPI(title="HireLoop", docs_url=None, redoc_url=None, lifespan=_lifespan)


def _manual_site(job: Job) -> str | None:
    from jobhunt.agents.applicant import manual_apply_site

    return manual_apply_site(job)


templates.env.globals["manual_apply_site"] = _manual_site

# Reachable before setup is finished. Everything else sends you to /setup.
OPEN_PATHS = ("/setup", "/healthz", "/favicon.ico")


def _same_origin(request: Request) -> bool:
    """Whether a state-changing request came from this dashboard.

    The dashboard has no login - it is bound to localhost - so the thing to
    stop is another website in your browser posting to it ("approve and submit
    job 42"). Browsers always send Origin on cross-site form posts.
    """
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return True  # not a browser (curl, tests)
    if source == "null":
        return False
    return urlparse(source).netloc == request.headers.get("host", "")


@app.middleware("http")
async def _guard(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if settings().jobhunt_read_only:
            # The public demo: show what a button does, never do it.
            back = urlsplit(request.headers.get("referer") or "/").path or "/"
            return RedirectResponse(f"{back}?read_only=1", status_code=303)
        if not _same_origin(request):
            return PlainTextResponse("Cross-site request refused.", status_code=403)
    path = request.url.path
    if not path.startswith(OPEN_PATHS) and not profile_exists():
        return RedirectResponse("/setup", status_code=303)
    return await call_next(request)


@app.exception_handler(Exception)
async def _crash(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return templates.TemplateResponse(
        request,
        "error.html",
        {"message": f"{type(exc).__name__}: {exc}", "log_path": settings().logs_dir},
        status_code=500,
    )


@app.get("/healthz")
def healthz():
    return {"ok": True, "setup_done": profile_exists()}


def _include_routers() -> None:
    from jobhunt.web.onboarding import router as onboarding_router
    from jobhunt.web.runs import router as runs_router

    app.include_router(onboarding_router)
    app.include_router(runs_router)


_include_routers()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


# A posting's first days are worth more than the rest of its life: the pile of
# applicants grows, and a recruiter who has already shortlisted is reading the
# later ones differently. These thresholds only reorder and label - nothing is
# dropped for being old, because a stale posting at the right company still
# beats a fresh one at the wrong one.
FRESH_DAYS = 3
STALE_DAYS = 21


def _age_days(job: Job) -> int | None:
    """Days since the posting went up, or None when the source never said."""
    if job.posted_at is None:
        return None
    posted = job.posted_at
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    return max(0, (datetime.now(UTC) - posted).days)


def _freshness(job: Job) -> tuple[str, str] | None:
    """A label and tone for how old this posting is, or None if unknown."""
    age = _age_days(job)
    if age is None:
        return None
    if age <= FRESH_DAYS:
        return ("new" if age <= 1 else f"{age}d old", "done")
    if age >= STALE_DAYS:
        return (f"{age}d old", "review")
    return (f"{age}d old", "off")


def _staleness_checker():
    """A function giving why a Resume row is out of date, or "" if current."""
    from jobhunt.resume.profile import Profile
    from jobhunt.resume.render import resume_staleness

    try:
        profile = Profile.load(settings().profile_path)
    except Exception:  # noqa: BLE001 - no profile, nothing to compare against
        profile = None

    def check(resume: Resume | None) -> str:
        if resume is None or not resume.pdf_path:
            return ""
        return resume_staleness(resume.pdf_path, resume.ats_detail, profile)

    return check


def _job_rows(
    status: JobStatus,
    limit: int = 100,
    eligibility: list[Eligibility] | None = None,
    *,
    q: str = "",
    location: str = "",
    min_score: float | None = None,
) -> list[dict[str, Any]]:
    """Jobs in one status, best first, narrowed by the Queue's search bar."""
    staleness = _staleness_checker()
    with session() as s:
        query = select(Job).where(Job.status == status)
        if eligibility:
            query = query.where(Job.eligibility.in_(eligibility))
        if q:
            like = f"%{q.strip()}%"
            query = query.where(Job.title.ilike(like) | Job.company.ilike(like))
        if location:
            query = query.where(Job.location.ilike(f"%{location.strip()}%"))
        if min_score is not None:
            query = query.where(Job.score >= min_score)
        jobs = s.exec(query.order_by(Job.score.desc()).limit(limit)).all()
        out = []
        for job in jobs:
            resume = s.exec(
                select(Resume).where(Resume.job_id == job.id).order_by(Resume.id.desc())
            ).first()
            # Who could refer him here. The Connector already finds these; not
            # showing them on the card meant the referral decision happened in
            # a different place from the apply decision, so it mostly did not
            # happen at all.
            contacts = s.exec(
                select(Contact)
                .where(Contact.company == job.company)
                .order_by(Contact.connection_degree, Contact.confidence.desc())
                .limit(3)
            ).all()
            out.append(
                {
                    "job": job,
                    "resume": resume,
                    "resume_stale": staleness(resume),
                    "contacts": contacts,
                    "age_days": _age_days(job),
                    "freshness": _freshness(job),
                }
            )

        # Score still leads, but within a quarter-point band the fresher
        # posting goes first - at that distance the scores are not meaningfully
        # different and the age is.
        out.sort(
            key=lambda r: (
                -round((r["job"].score or 0) * 4),
                r["age_days"] if r["age_days"] is not None else 999,
            )
        )
        return out


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", location: str = "", min_score: str = ""):
    """The queue, plus what cleared the bar but has no resume yet.

    The scored list is the pipeline's actual output. Leaving it off this page
    hid 35 matches behind a Pipeline tab, which made a run that had found
    plenty look like nothing had happened.

    The search bar narrows what is shown; the saved filters (Filters page)
    decide what reaches this page at all.
    """
    try:
        floor = float(min_score) if min_score.strip() else None
    except ValueError:
        floor = None
    narrow = {"q": q.strip(), "location": location.strip(), "min_score": floor}
    from jobhunt.graph.runner import runner
    from jobhunt.store.models import RunLog

    with session() as s:
        last_run = s.exec(select(RunLog).order_by(RunLog.started_at.desc()).limit(1)).first()
    return templates.TemplateResponse(
        request,
        "index.html",
        _ctx(
            ready=_job_rows(JobStatus.READY, **narrow),
            queued=_job_rows(JobStatus.QUEUED_APPROVAL, **narrow),
            scored_india=_job_rows(
                JobStatus.SCORED, limit=40, eligibility=[Eligibility.INDIA_ELIGIBLE], **narrow
            ),
            scored_other=_job_rows(
                JobStatus.SCORED,
                limit=25,
                eligibility=[Eligibility.SPONSORSHIP_OFFERED, Eligibility.UNKNOWN],
                **narrow,
            ),
            filters=filters_summary(),
            search={"q": q, "location": location, "min_score": min_score},
            searching=any(v for v in (q.strip(), location.strip(), min_score.strip())),
            last_run=last_run,
            busy=runner.busy(),
        ),
    )


@app.get("/pipeline", response_class=HTMLResponse)
def pipeline(request: Request, status: str = "scored"):
    try:
        wanted = JobStatus(status)
    except ValueError:
        wanted = JobStatus.SCORED
    return templates.TemplateResponse(
        request,
        "pipeline.html",
        _ctx(rows=_job_rows(wanted, limit=200), current=wanted.value,
             statuses=[s.value for s in JobStatus]),
    )


@app.get("/job/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int):
    with session() as s:
        job = s.get(Job, job_id)
        if job is None:
            return HTMLResponse("Not found", status_code=404)
        resume = s.exec(
            select(Resume).where(Resume.job_id == job_id).order_by(Resume.id.desc())
        ).first()
        referrals = s.exec(select(Referral).where(Referral.job_id == job_id)).all()
        applications = s.exec(select(Application).where(Application.job_id == job_id)).all()
    return templates.TemplateResponse(
        request,
        "job.html",
        _ctx(
            job=job,
            resume=resume,
            resume_stale=_staleness_checker()(resume),
            referrals=referrals,
            applications=applications,
        ),
    )


# How an application's raw status reads on the Applied board. The wording
# follows the talent-portal convention: what the *employer* is doing with it,
# not what our pipeline called the row.
APPLIED_STATES = {
    "awaiting_your_submit": ("Needs your submit", "you"),
    "submitted": ("Under review", "review"),
    "confirmed": ("Confirmed", "done"),
    "screen": ("Recruiter screen", "pending"),
    "interview": ("Interviewing", "pending"),
    "onsite": ("Onsite / final", "pending"),
    "offer": ("Offer", "done"),
    "rejected": ("Closed", "off"),
}

# The funnel in order. The app used to stop paying attention at "submitted",
# which is exactly when a search starts going well - Tracker can set these from
# Gmail, and the buttons let you correct it when a recruiter phones instead.
STAGE_ORDER = [
    "awaiting_your_submit",
    "submitted",
    "screen",
    "interview",
    "onsite",
    "offer",
]

# What each stage can move to. Forward one step, or out.
NEXT_STAGE = {
    "awaiting_your_submit": ["submitted", "rejected"],
    "submitted": ["screen", "interview", "rejected"],
    "confirmed": ["screen", "interview", "rejected"],
    "screen": ["interview", "rejected"],
    "interview": ["onsite", "offer", "rejected"],
    "onsite": ["offer", "rejected"],
    "offer": ["rejected"],
    "rejected": [],
}


def _applied_rows(limit: int = 200) -> list[dict[str, Any]]:
    """Every application, newest first, with the job and resume behind it."""
    staleness = _staleness_checker()
    with session() as s:
        apps = s.exec(
            select(Application).order_by(Application.applied_at.desc()).limit(limit)
        ).all()
        rows = []
        for app_row in apps:
            job = s.get(Job, app_row.job_id)
            if job is None:
                continue
            resume = (
                s.get(Resume, app_row.resume_id)
                if app_row.resume_id
                else s.exec(
                    select(Resume).where(Resume.job_id == job.id).order_by(Resume.id.desc())
                ).first()
            )
            label, tone = APPLIED_STATES.get(app_row.status, (app_row.status, "off"))
            # A job can be rejected after its form was prefilled - a level rule
            # tightened, or you rejected it by hand. Still offering "finish
            # it" would walk him into applying for a role his own config says
            # no to, so the row stays visible as history but goes inert.
            stale = (
                job.status in (JobStatus.REJECTED_GATE, JobStatus.ARCHIVED)
                and app_row.status == "awaiting_your_submit"
            )
            if stale:
                label, tone = "Withdrawn", "off"
            from jobhunt.agents.applicant import application_url

            rows.append(
                {
                    "app": app_row,
                    "job": job,
                    "resume": resume,
                    "resume_stale": staleness(resume),
                    # Where you go to submit it yourself. The posting URL and
                    # the application URL differ on Ashby and Lever.
                    "apply_url": application_url(job, job.ats or ""),
                    "label": label,
                    "tone": tone,
                    "stale": stale,
                    "stale_reason": job.status_reason if stale else None,
                    # The skills the tailored resume actually surfaced for this
                    # JD - the honest analogue of a job board's "required
                    # skills", since it is measured rather than declared.
                    "skills": ((resume.ats_detail or {}).get("matched") or []) if resume else [],
                }
            )
        return rows


@app.get("/applied", response_class=HTMLResponse)
def applied(request: Request, state: str = "all"):
    """Everything that has gone out, and where each one stands."""
    rows = _applied_rows()
    # Counted by the label on the card, not the raw status - a withdrawn row is
    # still `awaiting_your_submit` underneath, and counting it under "Needs your
    # submit" would promise work that the card itself refuses to offer.
    tabs = [("all", "All", len(rows))]
    for label in dict.fromkeys(r["label"] for r in rows):
        tabs.append((label, label, sum(1 for r in rows if r["label"] == label)))

    # The funnel strip: every stage in order, including the empty ones, because
    # a zero at "screen" is the most informative number on the page.
    live = [r for r in rows if not r["stale"]]
    funnel = [
        (APPLIED_STATES[st][0], sum(1 for r in live if r["app"].status == st))
        for st in STAGE_ORDER
    ]

    if state != "all":
        rows = [r for r in rows if r["label"] == state]
    return templates.TemplateResponse(
        request,
        "applied.html",
        _ctx(rows=rows, tabs=tabs, current=state, funnel=funnel, next_stage=NEXT_STAGE,
             stage_labels={k: v[0] for k, v in APPLIED_STATES.items()}),
    )


@app.get("/screenshot/{app_id}")
def screenshot(app_id: int):
    """The filled form as the agent left it.

    The whole promise of shadow mode is that you can see exactly what would
    have been sent; without a way to open the image that promise is empty.
    """
    with session() as s:
        row = s.get(Application, app_id)
    if row is None or not row.screenshot_path:
        return HTMLResponse("No screenshot for this application", status_code=404)
    path = Path(row.screenshot_path)
    if not path.is_absolute():
        # Older rows stored "data/screenshots/..." relative to the project.
        path = ROOT / path
    if not path.exists():
        return HTMLResponse("Screenshot file is missing", status_code=404)
    return FileResponse(path, media_type="image/png")


@app.get("/resume/{resume_id}")
def resume_pdf(resume_id: int):
    with session() as s:
        resume = s.get(Resume, resume_id)
    if resume is None or not Path(resume.pdf_path).exists():
        return HTMLResponse("Resume file not found", status_code=404)
    return FileResponse(resume.pdf_path, media_type="application/pdf")


@app.get("/archive", response_class=HTMLResponse)
def archive(request: Request):
    """Why jobs were filtered out.

    Exposed deliberately: a filter whose rejections cannot be reviewed cannot
    be tuned, and a silently-dropped good job is invisible.
    """
    with session() as s:
        archived = s.exec(
            select(Job)
            .where(Job.status == JobStatus.ARCHIVED)
            .order_by(Job.score.desc())
            .limit(120)
        ).all()
        gate_rejected = s.exec(
            select(Job).where(Job.status == JobStatus.REJECTED_GATE).limit(60)
        ).all()
    return templates.TemplateResponse(
        request,
        "archive.html",
        _ctx(archived=archived, gate_rejected=gate_rejected)
    )


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------


# Below this many applications in a bucket, a percentage is noise dressed up as
# a finding. Every tool in this category quotes "3x more interviews" off
# methodology nobody publishes; the point of this page is to not do that.
MIN_FOR_RATE = 10

# What counts as the funnel actually moving.
SENT = {"submitted", "confirmed", "screen", "interview", "onsite", "offer", "rejected"}
REPLIED = {"confirmed", "screen", "interview", "onsite", "offer", "rejected"}
ADVANCED = {"screen", "interview", "onsite", "offer"}


def _rate(part: int, whole: int) -> str:
    """A percentage, or an honest refusal to quote one."""
    if whole == 0:
        return "—"
    if whole < MIN_FOR_RATE:
        return f"{part}/{whole}"
    return f"{part / whole:.0%}"


def _breakdown(rows: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Group (bucket, status) pairs into sent / replied / advanced counts."""
    buckets: dict[str, dict[str, int]] = {}
    for bucket, status in rows:
        b = buckets.setdefault(bucket, {"sent": 0, "replied": 0, "advanced": 0})
        if status in SENT:
            b["sent"] += 1
        if status in REPLIED:
            b["replied"] += 1
        if status in ADVANCED:
            b["advanced"] += 1
    out = [
        {
            "name": name,
            **counts,
            "reply_rate": _rate(counts["replied"], counts["sent"]),
            "advance_rate": _rate(counts["advanced"], counts["sent"]),
            "thin": counts["sent"] < MIN_FOR_RATE,
        }
        for name, counts in buckets.items()
    ]
    out.sort(key=lambda r: (-r["sent"], r["name"]))
    return out


def _score_band(score: float | None) -> str:
    if score is None:
        return "unscored"
    for low in (4.5, 4.25, 4.0, 3.75):
        if score >= low:
            return f"{low:.2f}+"
    return "below 3.75"


def _coverage_band(resume: Resume | None) -> str:
    if resume is None or resume.ats_score is None:
        return "no resume"
    pct = resume.ats_score
    if pct >= 1.0:
        return "100%"
    if pct >= 0.9:
        return "90-99%"
    if pct >= 0.85:
        return "85-89%"
    return "under 85%"


@app.get("/insights", response_class=HTMLResponse)
def insights(request: Request):
    """What actually converts.

    No competitor in this category reports this, because none of them has the
    whole funnel in one place. It is also the only thing that can tell you
    whether the comp floor and the score threshold are set right - and until
    there are enough applications to say, it says so rather than inventing a
    number.
    """
    with session() as s:
        apps = s.exec(select(Application)).all()
        pairs: list[dict[str, Any]] = []
        for a in apps:
            job = s.get(Job, a.job_id)
            if job is None:
                continue
            resume = s.get(Resume, a.resume_id) if a.resume_id else None
            pairs.append({"app": a, "job": job, "resume": resume})

        # How old the things you are about to apply to actually are. This is
        # the number that decides whether sourcing needs re-running, and it is
        # invisible everywhere else in the app.
        live = s.exec(
            select(Job).where(
                Job.score >= targets().get("scoring", {}).get("apply_threshold", 3.7),
                Job.status.in_(
                    [
                        JobStatus.SCORED,
                        JobStatus.READY,
                        JobStatus.QUEUED_APPROVAL,
                        JobStatus.AWAITING_REFERRAL,
                    ]
                ),
            )
        ).all()
        ages = sorted(a for a in (_age_days(j) for j in live) if a is not None)
        freshness = {
            "n": len(ages),
            "unknown": len(live) - len(ages),
            "median": ages[len(ages) // 2] if ages else None,
            "buckets": [
                ("this week", sum(1 for a in ages if a <= 7)),
                ("8-30 days", sum(1 for a in ages if 7 < a <= 30)),
                ("1-3 months", sum(1 for a in ages if 30 < a <= 90)),
                ("3-6 months", sum(1 for a in ages if 90 < a <= 180)),
                ("over 6 months", sum(1 for a in ages if a > 180)),
            ],
            "last_sourced": s.exec(select(func.max(Job.first_seen))).one(),
        }

        funnel = {
            "sourced": s.exec(select(func.count(Job.id))).one(),
            "passed gate": s.exec(
                select(func.count(Job.id)).where(
                    Job.status.notin_([JobStatus.NEW, JobStatus.REJECTED_GATE])
                )
            ).one(),
            "scored": s.exec(
                select(func.count(Job.id)).where(Job.score.is_not(None))
            ).one(),
            "above bar": s.exec(
                select(func.count(Job.id)).where(
                    Job.score >= targets().get("scoring", {}).get("apply_threshold", 3.7)
                )
            ).one(),
            "resume written": s.exec(select(func.count(Resume.id))).one(),
            "sent": sum(1 for p in pairs if p["app"].status in SENT),
            "replied": sum(1 for p in pairs if p["app"].status in REPLIED),
            "advanced": sum(1 for p in pairs if p["app"].status in ADVANCED),
        }

    def by(key) -> list[dict[str, Any]]:
        return _breakdown([(key(p), p["app"].status) for p in pairs])

    return templates.TemplateResponse(
        request,
        "insights.html",
        _ctx(
            funnel=funnel,
            freshness=freshness,
            stale_share=(
                round(
                    100
                    * sum(n for label, n in freshness["buckets"] if label
                          in ("1-3 months", "3-6 months", "over 6 months"))
                    / freshness["n"]
                )
                if freshness["n"]
                else 0
            ),
            sent_total=funnel["sent"],
            min_for_rate=MIN_FOR_RATE,
            groups=[
                ("Source", by(lambda p: p["job"].source)),
                ("Company", by(lambda p: p["job"].company)),
                ("Score band", by(lambda p: _score_band(p["job"].score))),
                ("ATS coverage", by(lambda p: _coverage_band(p["resume"]))),
                ("Level band", by(lambda p: p["job"].level_band.value)),
                ("Eligibility", by(lambda p: p["job"].eligibility.value.replace("_", " "))),
            ],
        ),
    )


@app.post("/application/{app_id}/followup", response_class=HTMLResponse)
def followup(request: Request, app_id: int, notes: str = Form("")):
    """Draft the note to send after an interview.

    Generated on demand and not stored: it is a few seconds of LLM time, it is
    always better written against fresh notes, and storing it would mean a
    schema migration for something you paste into Gmail once.
    """
    from jobhunt.agents.writer import follow_up
    from jobhunt.resume.profile import Profile

    with session() as s:
        row = s.get(Application, app_id)
        if row is None:
            return RedirectResponse("/applied", status_code=303)
        job = s.get(Job, row.job_id)
    if job is None:
        return RedirectResponse("/applied", status_code=303)

    profile = Profile.load(settings().profile_path)
    written = follow_up(profile, job, row.status, notes=notes.strip())

    return templates.TemplateResponse(
        request,
        "followup.html",
        _ctx(job=job, app=row, written=written, notes=notes,
             stage_label=APPLIED_STATES.get(row.status, (row.status, "off"))[0]),
    )


@app.post("/application/{app_id}/stage")
def advance_stage(app_id: int, stage: str = Form(...)):
    """Move one application along the funnel.

    Deliberately a plain form post rather than a drag-and-drop board: this gets
    used from a phone in the thirty seconds after a recruiter call.
    """
    if stage not in APPLIED_STATES:
        return RedirectResponse("/applied", status_code=303)
    with session() as s:
        row = s.get(Application, app_id)
        if row is None:
            return RedirectResponse("/applied", status_code=303)
        row.status = stage
        row.updated_at = datetime.now(UTC)
        s.add(row)
        # A job whose application reached an interview stage is no longer just
        # "applied" - keep the Job row honest so the pipeline view agrees.
        job = s.get(Job, row.job_id)
        if job is not None and stage in ("screen", "interview", "onsite", "offer"):
            job.status = JobStatus.APPLIED
            job.status_reason = f"{APPLIED_STATES[stage][0].lower()}"
            s.add(job)
    return RedirectResponse("/applied", status_code=303)


@app.post("/job/{job_id}/decision")
def decide(job_id: int, action: str = Form(...), note: str = Form("")):
    """Approve, reject or re-queue one job.

    Approving runs the whole chain - write the resume and cover letter, then
    fill the application form. Previously it only changed a status, which read
    as the button doing nothing.
    """
    approved = False
    with session() as s:
        job = s.get(Job, job_id)
        if job is None:
            return RedirectResponse("/", status_code=303)

        if action == "approve":
            job.status = JobStatus.READY
            job.status_reason = f"approved by you{': ' + note if note else ''}"
            approved = True
        elif action == "reject":
            job.status = JobStatus.ARCHIVED
            job.status_reason = f"{MANUAL_REJECT_PREFIX}{': ' + note if note else ''}"
        elif action == "retailor":
            job.status = JobStatus.SCORED
            job.status_reason = "queued for re-tailoring"
        elif action == "applied":
            job.status = JobStatus.APPLIED
            job.status_reason = "marked applied by you"
            s.add(Application(job_id=job_id, method="manual", status="submitted", notes=note))

    if approved:
        _advance(job_id)
    return RedirectResponse(f"/job/{job_id}", status_code=303)


def _advance(job_id: int, *, requested: bool = True) -> None:
    """Carry an approved job all the way through: resume, form, submit.

    Approving is a deliberate click on one posting, so it counts as consent in
    the same way the Submit button does - shadow mode exists to stop the
    *unattended* scheduler sending things nobody has looked at, and the evening
    run still obeys it.

    What does not change: an incomplete form is still not submitted. A
    half-filled application is worse than none, so an unanswered required
    question queues the job with the reason on the card.
    """
    if not _ensure_current_resume(job_id):
        return

    try:
        _apply_one(job_id, requested=requested)
    except Exception as exc:  # noqa: BLE001
        _note(job_id, f"apply failed: {exc}")


def _ensure_current_resume(job_id: int) -> bool:
    """Make sure this job's resume exists and is current; write it if not.

    Every path that attaches a resume to an application goes through here.
    An existing PDF is not automatically a current one: a resume rendered in
    a superseded layout stayed attached to its job and was offered - and once
    sent - long after the layout was fixed. Returns False, with the reason on
    the job, when a current resume could not be produced.
    """
    from jobhunt.resume.profile import Profile
    from jobhunt.resume.render import resume_staleness

    with session() as s:
        resume = s.exec(
            select(Resume).where(Resume.job_id == job_id).order_by(Resume.id.desc())
        ).first()

    stale = ""
    if resume is not None:
        stale = resume_staleness(
            resume.pdf_path, resume.ats_detail, Profile.load(settings().profile_path)
        )
        if not stale:
            return True
        log.info("job %s: re-tailoring because %s", job_id, stale)

    from jobhunt.agents.tailor import run as tailor_run

    try:
        _, results = tailor_run(limit=1, job_id=job_id)
    except Exception as exc:  # noqa: BLE001 - reported on the job, never 500s
        _note(job_id, f"tailoring failed: {exc}")
        return False
    if not results or not results[0].ok:
        _note(job_id, f"tailoring failed: {results[0].error if results else 'no result'}")
        return False
    return True


def _note(job_id: int, message: str) -> None:
    with session() as s:
        job = s.get(Job, job_id)
        if job is not None:
            job.status_reason = message[:300]


@app.post("/job/{job_id}/tailor")
def tailor_now(job_id: int):
    """Write the tailored resume and cover letter for one job, now.

    Runs synchronously: this is a single-user local tool, and a job the user
    just clicked on is worth waiting ~40s for rather than silently queueing.
    """
    from jobhunt.agents.tailor import run as tailor_run

    stats, results = tailor_run(limit=1, job_id=job_id)
    result = results[0] if results else None
    with session() as s:
        job = s.get(Job, job_id)
        if job is not None and result is not None and not result.ok:
            job.status_reason = f"tailoring failed: {result.error or 'unknown'}"
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.post("/job/{job_id}/submit")
def submit_now(job_id: int):
    """Apply to this job and actually send it, in one click.

    Clicking this is the consent shadow mode otherwise waits for: it is one
    posting, chosen deliberately, rather than an unattended run. Everything
    else still holds - a form with an unanswered required question is not
    submitted just because a submit was asked for, because a half-filled
    application is worse than none.
    """
    with session() as s:
        job = s.get(Job, job_id)
    if job is None:
        return RedirectResponse("/applied", status_code=303)

    if not _ensure_current_resume(job_id):
        return RedirectResponse(f"/job/{job_id}", status_code=303)

    try:
        _apply_one(job_id, requested=True)
    except Exception as exc:  # noqa: BLE001
        _note(job_id, f"apply failed: {exc}")
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.post("/job/{job_id}/handover")
def handover_now(job_id: int):
    """Open a visible browser, fill the form, and leave it for you to submit.

    Spawned detached rather than run inline: the window stays open for as long
    as it takes to read the form, and a web request should not be held for ten
    minutes waiting on a person.
    """
    import subprocess
    import sys

    with session() as s:
        job = s.get(Job, job_id)
    if job is not None and (site := _manual_site(job)):
        _note(job_id, f"apply on {site} yourself - open the posting")
        return RedirectResponse(f"/job/{job_id}", status_code=303)
    # The window attaches whatever resume is newest, so make it a current one.
    if not _ensure_current_resume(job_id):
        return RedirectResponse(f"/job/{job_id}", status_code=303)
    _note(job_id, "opening a browser window for you to submit")
    try:
        subprocess.Popen(
            [sys.executable, "-m", "jobhunt", "handover", "--job-id", str(job_id)],
            cwd=str(ROOT),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # noqa: BLE001
        _note(job_id, f"could not open the browser: {exc}")
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.post("/job/{job_id}/apply")
def apply_now(job_id: int):
    """Fill this job's application form now.

    Whether it is actually submitted is decided by the autonomy rules, not by
    this button - with shadow_mode on it fills the form and screenshots it so
    you can see exactly what would have been sent.
    """
    if not _ensure_current_resume(job_id):
        return RedirectResponse(f"/job/{job_id}", status_code=303)
    try:
        _apply_one(job_id)
    except Exception as exc:  # noqa: BLE001 - reported on the job, never 500s
        _note(job_id, f"apply failed: {exc}")
    return RedirectResponse(f"/job/{job_id}", status_code=303)


def _apply_one(job_id: int, *, requested: bool = False) -> None:
    from pathlib import Path

    from jobhunt.agents.applicant import _record, apply_to, manual_apply_site, prepare_cover_letter

    with session() as s:
        job = s.get(Job, job_id)
    if job is not None and (site := manual_apply_site(job)):
        _note(job_id, f"apply on {site} yourself - open the posting")
        return
    from jobhunt.resume.profile import Profile
    from jobhunt.sources.browser.session import browser
    from jobhunt.store.db import consume_quota

    profile = Profile.load(settings().profile_path)
    with session() as s:
        job = s.get(Job, job_id)
        resume = s.exec(
            select(Resume).where(Resume.job_id == job_id).order_by(Resume.id.desc())
        ).first()
    if job is None:
        return

    resume_path = Path(resume.pdf_path) if resume and resume.pdf_path else None
    # Rendered before the browser opens; nesting sync Playwright fails.
    cover = prepare_cover_letter(profile, job)
    with browser(headless=True) as sess:
        result = apply_to(
            sess, job, profile, resume_path, cover=cover, requested=requested
        )

    with session() as s:
        tracked = s.get(Job, job_id)
        if tracked is None:
            return
        if result.submitted:
            # The same counter the CLI path consumes. Without this the daily
            # cap could be walked straight past by clicking Apply repeatedly.
            consume_quota("application")
            tracked.status = JobStatus.APPLIED
            tracked.status_reason = f"submitted via {result.ats}"
        elif result.error:
            # apply_to returns early on a page it could not use, and that error
            # was being dropped for the queued_reason, which is empty in that
            # case. The card then read "form filled - " and said nothing about
            # the form never having been found.
            tracked.status = JobStatus.QUEUED_APPROVAL
            tracked.status_reason = f"apply failed: {result.error}"
        else:
            tracked.status = JobStatus.QUEUED_APPROVAL
            tracked.status_reason = f"form filled - {result.queued_reason}"
        # Shared with applicant.run() so re-filling a form from the dashboard
        # updates the open row instead of stacking another one. Clicking Apply
        # three times used to leave three identical rows on the Applied board.
        _record(
            s,
            job_id,
            resume_id=resume.id if resume else None,
            method=f"ats_{'auto' if result.submitted else 'prefilled'}:{result.ats}",
            status="submitted" if result.submitted else "awaiting_your_submit",
            screenshot_path=result.screenshot,
            notes=(
                f"{result.queued_reason}. filled: "
                f"{', '.join(sorted(result.filled)) or 'nothing'}"
            ),
        )


@app.post("/referral/{referral_id}/decision")
def decide_referral(referral_id: int, action: str = Form(...), message: str = Form("")):
    with session() as s:
        ref = s.get(Referral, referral_id)
        if ref is None:
            return RedirectResponse("/", status_code=303)
        if message.strip():
            ref.message = message.strip()
        ref.status = {
            "approve": ApprovalStatus.APPROVED,
            "reject": ApprovalStatus.REJECTED,
            "sent": ApprovalStatus.SENT,
        }.get(action, ref.status)
        job_id = ref.job_id
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.post("/outreach/{outreach_id}/decision")
def decide_outreach(outreach_id: int, action: str = Form(...), body: str = Form("")):
    with session() as s:
        item = s.get(Outreach, outreach_id)
        if item is None:
            return RedirectResponse("/", status_code=303)
        if body.strip():
            item.body = body.strip()
        item.status = {
            "approve": ApprovalStatus.APPROVED,
            "reject": ApprovalStatus.REJECTED,
            "sent": ApprovalStatus.SENT,
        }.get(action, item.status)
    return RedirectResponse("/", status_code=303)


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    import uvicorn
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    if host in LOOPBACK:
        # Refuse requests addressed to any other hostname. Without this, a
        # malicious page could rebind its own domain to 127.0.0.1 and read
        # the dashboard - the origin check only covers requests that change
        # something.
        app.add_middleware(
            TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"]
        )
    uvicorn.run(app, host=host, port=port, log_level="warning")
