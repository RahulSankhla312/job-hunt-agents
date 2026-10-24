"""Onboarding and settings pages: Profile, Job filters, Connections.

On a fresh install these are a four-step wizard - profile, filters,
connections, then the Runs page to start the first search - and the rest of
the dashboard is locked until the profile exists. Afterwards the same pages
are where you change any of it.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import UTC
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from jobhunt.config import (
    local_targets,
    profile_exists,
    save_local_targets,
    settings,
    targets,
    update_env,
)
from jobhunt.resume.profile import Profile
from jobhunt.web import setup as forms
from jobhunt.web.common import ctx, templates
from jobhunt.web.connections import ai_summary, connections, validate_client_secret

log = logging.getLogger(__name__)
router = APIRouter()

MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Settings whose change alters which jobs pass. Changing any of these re-runs
# the (free, deterministic) filters over what is already in the database.
TARGETING_KEYS = ("compensation", "level", "scoring", "skip_companies")

STEPS = [
    ("setup", "Your profile"),
    ("filters", "Job filters"),
    ("connections", "Connections"),
    ("runs", "Start"),
]


def _step_ctx(current: str, onboarding: bool) -> dict[str, Any]:
    return {"steps": STEPS, "step": current, "onboarding": onboarding}


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------


def _load_base() -> tuple[Profile, bool]:
    """The profile to show: an unsaved resume import first, then the saved one."""
    s = settings()
    for path, is_draft in ((s.profile_draft_path, True), (s.profile_path, False)):
        if path.exists():
            try:
                return Profile.load(path), is_draft
            except Exception:  # noqa: BLE001 - a broken file must not lock you out
                log.exception("could not read %s", path)
    return Profile(), False


def _render_setup(
    request: Request,
    profile: Profile,
    *,
    errors: list[str] | None = None,
    draft: bool = False,
    notice: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    onboarding = not profile_exists()
    return templates.TemplateResponse(
        request,
        "setup.html",
        ctx(
            p=profile,
            errors=errors or [],
            draft=draft,
            notice=notice,
            eeo=forms.EEO_SUGGESTIONS,
            has_resume=settings().uploaded_resume_path.exists(),
            **_step_ctx("setup", onboarding),
        ),
        status_code=status_code,
    )


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, parsed: str = "", error: str = "", saved: str = ""):
    profile, draft = _load_base()
    notice = ""
    if parsed == "llm":
        notice = "Your resume was read with AI. Check every field, then save."
    elif parsed == "heuristic":
        notice = (
            "The AI engine was not available, so your resume was read with the basic "
            "parser. Check the work history carefully before saving."
        )
    elif saved:
        notice = "Profile saved."
    return _render_setup(
        request, profile, draft=draft, notice=notice, errors=[error] if error else []
    )


@router.post("/setup", response_class=HTMLResponse)
async def setup_save(request: Request):
    form = await request.form()
    first_time = not profile_exists()
    base, _ = _load_base()
    result = forms.parse(form, base)
    if not result.ok:
        return _render_setup(
            request,
            result.profile,
            errors=result.errors,
            draft=settings().profile_draft_path.exists(),
            status_code=400,
        )

    s = settings()
    result.profile.save(s.profile_path)
    s.profile_draft_path.unlink(missing_ok=True)
    log.info("profile saved (%s)", "first time" if first_time else "update")
    return _redirect("/filters?onboarding=1" if first_time else "/setup?saved=1")


@router.post("/setup/resume")
def setup_resume(resume: UploadFile = File(...), use_ai: str = Form("")):
    """Read an uploaded resume into the form, as an unsaved draft."""
    raw = resume.file.read(MAX_UPLOAD_BYTES + 1)
    if not raw:
        return _redirect("/setup?error=" + quote("Choose a PDF first."))
    if len(raw) > MAX_UPLOAD_BYTES:
        return _redirect("/setup?error=" + quote("That file is over 10 MB."))
    if not raw.startswith(b"%PDF"):
        return _redirect("/setup?error=" + quote("That file is not a PDF."))

    s = settings()
    s.profile_dir.mkdir(parents=True, exist_ok=True)
    s.uploaded_resume_path.write_bytes(raw)

    from jobhunt.resume.extract import extract_profile

    try:
        parsed, method = extract_profile(s.uploaded_resume_path, prefer_llm=bool(use_ai))
    except ValueError as exc:  # scanned PDF with no text layer
        return _redirect("/setup?error=" + quote(str(exc)[:300]))
    except Exception as exc:  # noqa: BLE001 - shown on the page
        log.exception("resume import failed")
        return _redirect("/setup?error=" + quote(f"Could not read that PDF: {exc}"[:300]))

    base, _ = _load_base()
    merged = forms.merge_parsed(parsed, base if profile_exists() or base.identity.name else None)
    merged.save(s.profile_draft_path)
    return _redirect(f"/setup?parsed={method}")


@router.post("/setup/discard")
def setup_discard():
    settings().profile_draft_path.unlink(missing_ok=True)
    return _redirect("/setup")


# --------------------------------------------------------------------------
# Job filters
# --------------------------------------------------------------------------


def _render_filters(
    request: Request,
    cfg: dict[str, Any],
    *,
    onboarding: bool,
    errors: list[str] | None = None,
    notice: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    from jobhunt.graph.scheduler import schedule_times
    from jobhunt.web.setup import COMMON_TIMEZONES

    return templates.TemplateResponse(
        request,
        "filters.html",
        ctx(
            t=cfg,
            errors=errors or [],
            notice=notice,
            timezones=COMMON_TIMEZONES,
            times=schedule_times(),
            customised=bool(local_targets()),
            resume_file=_resume_file_info(),
            **_step_ctx("filters", onboarding),
        ),
        status_code=status_code,
    )


@router.get("/filters", response_class=HTMLResponse)
def filters_page(
    request: Request,
    onboarding: str = "",
    saved: str = "",
    refilter: str = "",
    msg: str = "",
    error: str = "",
):
    notice = msg
    if saved:
        notice = "Filters saved."
        if refilter:
            notice += " Re-applying them to the jobs already found - see Runs."
    return _render_filters(
        request,
        targets(),
        onboarding=bool(onboarding),
        notice=notice,
        errors=[error] if error else None,
    )


@router.post("/filters", response_class=HTMLResponse)
async def filters_save(request: Request):
    from jobhunt.config import deep_merge
    from jobhunt.graph.runner import runner

    form = await request.form()
    onboarding = bool(form.get("onboarding"))
    errors: list[str] = []
    overrides = forms.parse_targets(form, errors)
    wants_original = overrides.get("resume", {}).get("mode") == "original"
    if wants_original and not settings().uploaded_resume_path.exists():
        errors.append("Upload your resume PDF (under 'Resume sent with applications') first")
    if errors:
        preview = deep_merge(targets(), overrides)
        return _render_filters(
            request, preview, onboarding=onboarding, errors=errors, status_code=400
        )

    before = {k: targets().get(k) for k in TARGETING_KEYS}
    mode_before = targets().get("resume", {}).get("mode", "tailored")
    save_local_targets(overrides)
    after = {k: targets().get(k) for k in TARGETING_KEYS}
    if wants_original and mode_before != "original":
        # Free in this direction: attaching your PDF is a copy, not an AI call.
        runner.start_or_queue("refresh", "switched to your own resume")

    refilter = False
    if before != after and _has_jobs():
        refilter, _ = runner.start_or_queue("refilter", "filters changed")

    _apply_schedule_setting()
    if onboarding:
        return _redirect("/connections?onboarding=1")
    return _redirect("/filters?saved=1" + ("&refilter=1" if refilter else ""))


@router.post("/filters/reset")
def filters_reset():
    """Forget your filter changes; keep whether the scheduler is on."""
    from jobhunt.graph.runner import runner

    path = settings().local_targets_path
    enabled = local_targets().get("schedule", {}).get("enabled")
    path.unlink(missing_ok=True)
    if enabled is not None:
        save_local_targets({"schedule": {"enabled": enabled}})
    if _has_jobs():
        runner.start_or_queue("refilter", "filters reset")
    _apply_schedule_setting()
    return _redirect("/filters?saved=1")


def _resume_file_info() -> dict[str, Any] | None:
    path = settings().uploaded_resume_path
    try:
        st = path.stat()
    except OSError:
        return None
    from datetime import datetime

    return {
        "size_kb": round(st.st_size / 1024),
        "uploaded": datetime.fromtimestamp(st.st_mtime, tz=UTC),
    }


@router.post("/filters/resume")
def filters_resume_upload(resume: UploadFile = File(...)):
    """Store the resume PDF that "send my own resume" mode attaches."""
    from jobhunt.graph.runner import runner

    raw = resume.file.read(MAX_UPLOAD_BYTES + 1)
    if not raw.startswith(b"%PDF") or len(raw) > MAX_UPLOAD_BYTES:
        return _redirect("/filters?error=" + quote("Choose a PDF under 10 MB.") + "#resume")
    path = settings().uploaded_resume_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    note = "Resume PDF saved."
    if targets().get("resume", {}).get("mode") == "original" and _has_jobs():
        runner.start_or_queue("refresh", "new resume uploaded")
        note += " Attaching it to your jobs now - see Runs."
    return _redirect("/filters?msg=" + quote(note) + "#resume")


@router.get("/filters/resume.pdf")
def filters_resume_file():
    from fastapi.responses import FileResponse

    path = settings().uploaded_resume_path
    if not path.exists():
        return HTMLResponse("No resume uploaded yet", status_code=404)
    return FileResponse(path, media_type="application/pdf")


def _has_jobs() -> bool:
    from sqlmodel import func, select

    from jobhunt.store.db import session
    from jobhunt.store.models import Job

    with session() as s:
        return s.exec(select(func.count(Job.id))).one() > 0


def _apply_schedule_setting() -> None:
    """Start, stop or re-time the dashboard's scheduler to match the config."""
    from jobhunt.graph.runner import runner

    enabled = bool(targets().get("schedule", {}).get("enabled"))
    try:
        if enabled and profile_exists():
            if runner.scheduler_running():
                runner.reload_scheduler()
            else:
                runner.start_scheduler()
        else:
            runner.stop_scheduler()
    except Exception:  # noqa: BLE001 - reported on the Runs page, never a 500
        log.exception("could not apply the schedule setting")


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------


@router.get("/connections", response_class=HTMLResponse)
def connections_page(request: Request, onboarding: str = "", msg: str = "", error: str = ""):
    from jobhunt.sources.gmail import is_configured

    s = settings()
    gmail_ok, gmail_detail = is_configured()
    return templates.TemplateResponse(
        request,
        "connections.html",
        ctx(
            status=connections.snapshot(),
            ai=ai_summary(),
            gmail_secret=s.google_client_secret_path.exists(),
            gmail_ok=gmail_ok,
            gmail_detail=gmail_detail,
            busy=connections.busy(),
            notice=msg,
            errors=[error] if error else [],
            is_windows=os.name == "nt",
            **_step_ctx("connections", bool(onboarding)),
        ),
    )


@router.get("/connections/status")
def connections_status():
    return JSONResponse(
        {
            k: {"ok": v.ok, "running": v.running, "message": v.message, "action": v.action}
            for k, v in connections.snapshot().items()
        }
    )


def _back(onboarding: str, **params: str) -> RedirectResponse:
    query = "&".join(f"{k}={quote(v)}" for k, v in params.items() if v)
    if onboarding:
        query = "onboarding=1" + ("&" + query if query else "")
    return _redirect("/connections" + ("?" + query if query else ""))


@router.post("/connections/ai")
def connections_ai_save(
    backend: str = Form("claude_cli"),
    api_key: str = Form(""),
    cli_path: str = Form(""),
    onboarding: str = Form(""),
):
    if backend not in ("claude_cli", "anthropic_api"):
        return _back(onboarding, error="Unknown AI engine.")
    values = {"JOBHUNT_LLM_BACKEND": backend, "CLAUDE_CLI_PATH": cli_path.strip()}
    if api_key.strip():
        values["ANTHROPIC_API_KEY"] = api_key.strip()
    if backend == "anthropic_api" and not (api_key.strip() or settings().anthropic_api_key):
        return _back(onboarding, error="Paste an Anthropic API key to use the API engine.")
    try:
        update_env(values)
    except (OSError, ValueError) as exc:
        return _back(onboarding, error=f"Could not save: {exc}")
    connections.check_ai()
    return _back(onboarding, msg="Saved. Testing the AI engine now...")


@router.post("/connections/ai/check")
def connections_ai_check(onboarding: str = Form("")):
    connections.check_ai()
    return _back(onboarding)


@router.post("/connections/ai/claude-login")
def connections_claude_login(onboarding: str = Form("")):
    """Open the Claude Code CLI in its own console so you can run /login."""
    from jobhunt.llm.adapter import discover_claude_cli

    exe = discover_claude_cli()
    if exe is None:
        return _back(onboarding, error="Claude Code CLI not found. Install it, or use an API key.")
    if os.name != "nt":
        return _back(onboarding, error=f"Run this in a terminal: {exe}  then type /login")
    try:
        subprocess.Popen([str(exe)], creationflags=subprocess.CREATE_NEW_CONSOLE)
    except OSError as exc:
        return _back(onboarding, error=f"Could not open it: {exc}")
    return _back(
        onboarding,
        msg="A Claude window opened. Type /login there, finish signing in, then press Test.",
    )


@router.post("/connections/linkedin/signin")
def connections_linkedin_signin(onboarding: str = Form("")):
    from jobhunt.sources.browser.session import BROWSER_BUSY, browser_in_use

    # Say so now rather than promising a window that is not coming.
    if browser_in_use():
        return _back(onboarding, error=f"LinkedIn sign-in: {BROWSER_BUSY}")
    started, why = connections.sign_in_linkedin()
    if not started:
        return _back(onboarding, error=f"LinkedIn sign-in: {why}")
    return _back(
        onboarding,
        msg="A browser window opened on LinkedIn. Sign in there - this page updates by itself.",
    )


@router.post("/connections/linkedin/check")
def connections_linkedin_check(onboarding: str = Form("")):
    connections.check_linkedin()
    return _back(onboarding)


@router.post("/connections/linkedin/cancel")
def connections_linkedin_cancel(onboarding: str = Form("")):
    connections.cancel_sign_in()
    return _back(onboarding)


@router.post("/connections/gmail/secret")
def connections_gmail_secret(secret: UploadFile = File(...), onboarding: str = Form("")):
    raw = secret.file.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        return _back(onboarding, error="That file is too large to be an OAuth client JSON.")
    problem = validate_client_secret(raw)
    if problem:
        return _back(onboarding, error=f"Gmail: {problem}")
    path = settings().google_client_secret_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return _back(onboarding, msg="OAuth client saved. Now press Connect Gmail.")


@router.post("/connections/gmail/connect")
def connections_gmail_connect(onboarding: str = Form("")):
    if not settings().google_client_secret_path.exists():
        return _back(onboarding, error="Upload the OAuth client JSON first.")
    started, why = connections.connect_gmail()
    if not started:
        return _back(onboarding, error=f"Gmail: {why}")
    return _back(
        onboarding,
        msg="A Google consent page opened in your browser. Approve it - this page updates by itself.",
    )


@router.post("/connections/gmail/disconnect")
def connections_gmail_disconnect(onboarding: str = Form("")):
    connections.disconnect_gmail()
    return _back(onboarding, msg="Gmail disconnected.")
