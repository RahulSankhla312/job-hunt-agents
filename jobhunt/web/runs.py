"""The Runs page: start a run now, switch the daily schedule, see history."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlmodel import select

from jobhunt.config import save_local_targets, targets
from jobhunt.graph.runner import RUNS, runner
from jobhunt.store.db import session
from jobhunt.store.models import RunLog
from jobhunt.web.common import ctx, templates

router = APIRouter()

RUN_HELP = {
    "morning": (
        "Pulls new postings from every source, filters them by your settings, scores "
        "the survivors with AI, writes tailored resumes for the best, and drafts "
        "referral asks. Takes 10-30 minutes."
    ),
    "evening": (
        "Applies to jobs that are ready (only submits in live mode, only clean forms), "
        "drafts recruiter emails, sends the ones you approved, and reads replies."
    ),
    "refilter": (
        "Re-applies your filters to every job already found. No AI calls, no browsing. "
        "Runs by itself when you change the filters."
    ),
    "refresh": (
        "Rewrites every resume made with an older layout or an older version of your "
        "profile, for jobs still in play. One AI call per resume."
    ),
}


def summarise(stats: dict[str, Any] | None) -> list[tuple[str, str]]:
    """A run's per-stage stats as short "stage: n key, n key" lines."""
    out = []
    for stage, value in (stats or {}).items():
        if isinstance(value, dict):
            bits = [
                f"{v} {k.replace('_', ' ')}"
                for k, v in value.items()
                if isinstance(v, int | float) and not isinstance(v, bool) and v
            ]
            text = ", ".join(bits[:5]) or "nothing to do"
            errors = value.get("errors")
            if isinstance(errors, list) and errors:
                more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
                text += f" — {str(errors[0])[:140]}{more}"
            out.append((stage, text))
        else:
            out.append((stage, str(value)[:120]))
    return out


@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, welcome: str = "", msg: str = "", error: str = ""):
    from jobhunt.graph.scheduler import schedule_times
    from jobhunt.web.connections import ai_summary, connections

    with session() as s:
        history = s.exec(select(RunLog).order_by(RunLog.started_at.desc()).limit(25)).all()
    morning_at, evening_at, tz = schedule_times()
    ai_status = connections.get("ai")
    try:
        from jobhunt.agents.tailor import outdated_jobs

        outdated = len(outdated_jobs())
    except Exception:  # noqa: BLE001 - a count on a card must not 500 the page
        outdated = 0
    return templates.TemplateResponse(
        request,
        "runs.html",
        ctx(
            runs=RUNS,
            run_help=RUN_HELP,
            state=runner.state,
            last=runner.last,
            busy=runner.busy(),
            history=[{"row": r, "summary": summarise(r.stats)} for r in history],
            scheduler_on=runner.scheduler_running(),
            schedule_enabled=bool(targets().get("schedule", {}).get("enabled")),
            next_runs=runner.next_runs(),
            morning_at=morning_at,
            evening_at=evening_at,
            tz=tz,
            ai=ai_summary(),
            ai_status=ai_status,
            outdated=outdated,
            welcome=bool(welcome),
            notice=msg,
            errors=[error] if error else [],
        ),
    )


@router.get("/runs/status")
def runs_status():
    st = runner.state
    return JSONResponse(
        {
            "running": runner.busy(),
            "which": st.which,
            "label": st.label,
            "stage": st.stage,
            "elapsed": st.elapsed_s,
            "ok": st.ok,
            "message": st.message,
        }
    )


@router.post("/runs/start")
def runs_start(which: str = Form(...)):
    started, message = runner.start(which, "manual")
    key = "msg" if started else "error"
    return RedirectResponse(f"/runs?{key}={quote(message)}", status_code=303)


@router.post("/runs/scheduler")
def runs_scheduler(enabled: str = Form("")):
    on = enabled == "1"
    save_local_targets({"schedule": {"enabled": on}})
    try:
        if on:
            runner.start_scheduler()
        else:
            runner.stop_scheduler()
    except Exception as exc:  # noqa: BLE001 - shown on the page
        return RedirectResponse(
            f"/runs?error={quote(f'Scheduler: {exc}')}", status_code=303
        )
    message = "Daily schedule is on." if on else "Daily schedule is off."
    return RedirectResponse(f"/runs?msg={quote(message)}", status_code=303)
