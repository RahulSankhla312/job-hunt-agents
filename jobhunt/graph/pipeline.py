"""The two daily runs.

**Morning** finds and prepares work: source, filter, score, tailor, and draft
referral asks.

**Evening** acts on it: expire referral SLAs that have lapsed, apply, chase
recruiters, read replies, and write the digest.

The split exists so a referral gets a working day to arrive before the
application goes out without one. Every stage is wrapped: a stage that throws
is recorded and the run continues, because a dead job board must not cost you
the day's applications.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlmodel import select

from jobhunt.config import profile_exists, settings, targets
from jobhunt.store.db import session
from jobhunt.store.models import (
    Application,
    ApprovalStatus,
    Job,
    JobStatus,
    Outreach,
    Referral,
    RunLog,
)

log = logging.getLogger(__name__)

StageHook = Callable[[str], None]


class RunInProgress(RuntimeError):
    """Another run - here or in another process - has not finished yet."""


class ProfileMissing(RuntimeError):
    """Setup has not been completed, so there is nobody to apply for."""


@contextmanager
def exclusive(label: str) -> Iterator[None]:
    """Hold the machine-wide run lock for the duration of one run."""
    from jobhunt.util.lock import FileLock

    lock = FileLock(settings().data_dir / ".run.lock")
    if not lock.acquire(label):
        raise RunInProgress(
            "another run is still in progress"
            + (f" ({lock.holder()})" if lock.holder() else "")
        )
    try:
        yield
    finally:
        lock.release()


def _require_profile() -> None:
    if not profile_exists():
        raise ProfileMissing(
            "no profile yet - finish setup in the dashboard (hireloop web) "
            "or run: hireloop onboard"
        )


@dataclass
class RunReport:
    run_type: str
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    stages: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def _stage(
    report: RunReport, name: str, fn: Callable[[], Any], on_stage: StageHook | None = None
) -> Any:
    """Run one stage, recording the outcome either way.

    A failure is logged and the run continues. The alternative - aborting the
    whole run because one source is down - loses the day's applications for a
    reason that has nothing to do with them.
    """
    if on_stage is not None:
        on_stage(name)
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 - the whole point is to keep going
        report.errors[name] = f"{type(exc).__name__}: {exc}"
        log.error("stage %s failed: %s", name, exc)
        log.debug(traceback.format_exc())
        return None

    report.stages[name] = result.as_dict() if hasattr(result, "as_dict") else result
    log.info("stage %s: %s", name, report.stages[name])
    return result


def morning(
    *,
    score_limit: int | None = None,
    tailor_limit: int = 5,
    referral_limit: int = 3,
    on_stage: StageHook | None = None,
) -> RunReport:
    """Find and prepare work: source, filter, score, tailor, draft referrals."""
    from jobhunt.agents import analyst, connector, gatekeeper, scout, tailor
    from jobhunt.store.models import Eligibility

    _require_profile()
    with exclusive("morning"):
        report = RunReport(run_type="morning")

        def stage(name: str, fn: Callable[[], Any]) -> Any:
            return _stage(report, name, fn, on_stage)

        stage("scout", lambda: scout.run())
        stage("gatekeeper", lambda: gatekeeper.run())
        stage(
            "analyst",
            lambda: analyst.run(
                limit=score_limit or int(targets().get("scoring", {}).get("per_run", 40)),
                # Roles the candidate can actually take are scored first, so a
                # run cut short by rate limits still produces usable results.
                eligibility=[Eligibility.INDIA_ELIGIBLE, Eligibility.SPONSORSHIP_OFFERED],
            ),
        )
        stage("tailor", lambda: tailor.run(limit=tailor_limit)[0])

        if targets().get("autonomy", {}).get("linkedin_dm") != "off":
            stage("connector", lambda: connector.run(limit=referral_limit))

        _finish(report)
    return report


def evening(
    *, apply_limit: int = 5, outreach_limit: int = 5, on_stage: StageHook | None = None
) -> RunReport:
    """Act on it: release lapsed referrals, apply, chase recruiters, read replies."""
    from jobhunt.agents import applicant, connector, outreach, tracker

    _require_profile()
    with exclusive("evening"):
        report = RunReport(run_type="evening")

        def stage(name: str, fn: Callable[[], Any]) -> Any:
            return _stage(report, name, fn, on_stage)

        # Release jobs whose referral window has closed, before applying.
        stage("referral_sweep", lambda: {"expired": connector.expire_overdue()})
        stage("applicant", lambda: applicant.run(limit=apply_limit)[0])
        stage("outreach", lambda: outreach.run(limit=outreach_limit))
        stage("outreach_send", lambda: outreach.send_approved())
        stage("followups", lambda: outreach.send_followups())
        stage("tracker", lambda: tracker.run())

        _finish(report)
    return report


def refilter(*, on_stage: StageHook | None = None) -> RunReport:
    """Re-apply the job filters to everything already in the database.

    Runs after the filters change on the settings page. Deterministic and free:
    no sourcing and no LLM calls - the gate is recomputed from scratch, work
    already past it is re-audited, and existing scores are re-routed against
    the new threshold.
    """
    from jobhunt.agents import analyst, gatekeeper

    with exclusive("refilter"):
        report = RunReport(run_type="refilter")

        def stage(name: str, fn: Callable[[], Any]) -> Any:
            return _stage(report, name, fn, on_stage)

        stage("gate_reset", lambda: {"reset": gatekeeper.reset()})
        stage("gatekeeper", lambda: gatekeeper.run())
        stage(
            "audit",
            lambda: {
                "rejected": len((r := gatekeeper.audit_progressed(apply_changes=True)).rejected),
                "rebanded": len(r.rebanded),
            },
        )
        stage("reroute", lambda: analyst.reroute())
        _finish(report)
    return report


def refresh_resumes(*, on_stage: StageHook | None = None) -> RunReport:
    """Rewrite the resume of every active job whose resume is out of date.

    After the resume layout or your profile changes, the resumes already
    written are superseded. This rewrites them all at once instead of one
    Approve at a time - one AI call per job.
    """
    from jobhunt.agents import tailor

    _require_profile()
    with exclusive("refresh"):
        report = RunReport(run_type="refresh")
        _stage(report, "backfill", tailor.backfill_layout_records, on_stage)
        _stage(report, "rewrite", tailor.refresh_outdated, on_stage)
        _finish(report)
    return report


def _finish(report: RunReport) -> None:
    with session() as s:
        s.add(
            RunLog(
                run_type=report.run_type,
                started_at=report.started_at,
                finished_at=datetime.now(UTC),
                ok=report.ok,
                stats=report.stages,
                error="; ".join(f"{k}: {v}" for k, v in report.errors.items()) or None,
            )
        )


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------


def digest() -> dict[str, Any]:
    """What happened, and what needs you."""
    with session() as s:
        def count(status: JobStatus) -> int:
            return len(s.exec(select(Job).where(Job.status == status)).all())

        ready = s.exec(
            select(Job).where(Job.status == JobStatus.READY).order_by(Job.score.desc()).limit(10)
        ).all()
        queued = s.exec(
            select(Job)
            .where(Job.status == JobStatus.QUEUED_APPROVAL)
            .order_by(Job.score.desc())
            .limit(10)
        ).all()
        pending_referrals = s.exec(
            select(Referral).where(Referral.status == ApprovalStatus.QUEUED)
        ).all()
        pending_outreach = s.exec(
            select(Outreach).where(Outreach.status == ApprovalStatus.QUEUED)
        ).all()
        applications = s.exec(select(Application)).all()

        by_status: dict[str, int] = {}
        for a in applications:
            by_status[a.status] = by_status.get(a.status, 0) + 1

        return {
            "needs_you": {
                "jobs": [
                    {"id": j.id, "company": j.company, "title": j.title, "score": j.score}
                    for j in queued
                ],
                "referrals": len(pending_referrals),
                "outreach": len(pending_outreach),
            },
            "ready_to_apply": [
                {"id": j.id, "company": j.company, "title": j.title, "score": j.score}
                for j in ready
            ],
            "pipeline": {
                "sourced": len(s.exec(select(Job)).all()),
                "gated": count(JobStatus.GATED),
                "scored": count(JobStatus.SCORED),
                "ready": count(JobStatus.READY),
                "awaiting_referral": count(JobStatus.AWAITING_REFERRAL),
                "applied": count(JobStatus.APPLIED),
            },
            "applications": by_status,
        }
