"""Shared dashboard plumbing: templates, the per-page context, small helpers.

Kept apart from the routes so every router can render a page without
importing another router's module.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates
from sqlmodel import func, select

from jobhunt.config import profile_exists, settings, targets
from jobhunt.store.db import session
from jobhunt.store.models import ApprovalStatus, Job, JobStatus, Outreach, Referral

TEMPLATES = Path(__file__).parent / "templates"
log = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(TEMPLATES))

# Prefixes _note() writes when a step refuses to continue. The card reads these
# back so a blocked job announces itself: clicking Approve and seeing nothing
# happen, with the reason only in a status field nobody scrolls to, is how you
# end up asking why it is not applying.
BLOCKED_PREFIXES = ("tailoring failed", "apply failed", "cover letter failed")


def is_blocked(job: Job) -> bool:
    return (job.status_reason or "").startswith(BLOCKED_PREFIXES)


def _display_tz() -> ZoneInfo:
    from jobhunt.graph.scheduler import schedule_times

    return ZoneInfo(schedule_times()[2])


def localtime(value: datetime | str | None, fmt: str = "%d %b %H:%M") -> str:
    """A UTC timestamp in your configured timezone, for display."""
    if value is None or value == "":
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(_display_tz()).strftime(fmt)


def duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


templates.env.filters["localtime"] = localtime
templates.env.filters["duration"] = duration


def counts() -> dict[str, int]:
    with session() as s:
        def n(status: JobStatus) -> int:
            return s.exec(select(func.count(Job.id)).where(Job.status == status)).one()

        return {
            "ready": n(JobStatus.READY),
            "queued": n(JobStatus.QUEUED_APPROVAL),
            "awaiting_referral": n(JobStatus.AWAITING_REFERRAL),
            "scored": n(JobStatus.SCORED),
            "gated": n(JobStatus.GATED),
            "applied": n(JobStatus.APPLIED),
            "archived": n(JobStatus.ARCHIVED),
            "total": s.exec(select(func.count(Job.id))).one(),
            "pending_referrals": s.exec(
                select(func.count(Referral.id)).where(Referral.status == ApprovalStatus.QUEUED)
            ).one(),
            "pending_outreach": s.exec(
                select(func.count(Outreach.id)).where(Outreach.status == ApprovalStatus.QUEUED)
            ).one(),
        }


def me() -> dict[str, str]:
    """Name and initials for the sidebar, from the saved profile."""
    from jobhunt.web.setup import initials

    if not profile_exists():
        return {"name": "New user", "initials": "?"}
    try:
        from jobhunt.resume.profile import Profile

        name = Profile.load(settings().profile_path).identity.name or "You"
    except Exception:  # noqa: BLE001 - a malformed profile must not 500 every page
        name = "You"
    return {"name": name, "initials": initials(name)}


def filters_summary(cfg: dict[str, Any] | None = None) -> list[str]:
    """The active search filters, as short phrases for the Queue header."""
    cfg = cfg or targets()
    comp = cfg.get("compensation", {})
    level = cfg.get("level", {})
    out = []
    if comp.get("india_min_ctc_lpa"):
        out.append(f"₹{comp['india_min_ctc_lpa']:g}L+ in India")
    if comp.get("us_remote_min_usd"):
        out.append(f"${int(comp['us_remote_min_usd']) // 1000}k+ US/remote")
    titles = level.get("direct_titles") or []
    if titles:
        more = f" +{len(titles) - 3}" if len(titles) > 3 else ""
        out.append(", ".join(titles[:3]) + more)
    if level.get("max_years_required") is not None:
        out.append(f"≤{level['max_years_required']} yrs asked")
    out.append(f"score ≥ {cfg.get('scoring', {}).get('apply_threshold', 3.7)}")
    return out


def ctx(**extra: Any) -> dict[str, Any]:
    """Context every full page gets: counts, identity, autonomy, run state."""
    from jobhunt.graph.runner import runner

    cfg = targets()
    return {
        "counts": counts(),
        "shadow_mode": cfg.get("autonomy", {}).get("shadow_mode", True),
        "threshold": cfg.get("scoring", {}).get("apply_threshold", 3.75),
        "is_blocked": is_blocked,
        "me": me(),
        "setup_done": profile_exists(),
        "run_state": runner.state,
        **extra,
    }
