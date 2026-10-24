#!/usr/bin/env python
"""Job-hunt agent team - command line.

Most people only ever need one command::

    python run.py web        # dashboard: setup, filters, runs, schedule, queue

Everything else is here for scripting and debugging::

    python run.py demo                # try it with a made-up candidate first
    python run.py doctor              # verify every dependency end to end
    python run.py run-now morning     # one full pass, in the terminal
    python run.py source --dry-run    # pull + dedupe, write nothing
    python run.py score --explain     # score the shortlist
"""

from __future__ import annotations

import logging
import sys

# The Windows console defaults to cp1252, which cannot encode the rupee sign,
# check marks or middots this CLI prints - it raises UnicodeEncodeError mid
# render. Force UTF-8 before anything writes output.
for _stream in (sys.stdout, sys.stderr):
    if getattr(_stream, "encoding", "") and _stream.encoding.lower() not in ("utf-8", "utf8"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):  # pragma: no cover - exotic shells
            pass

import typer  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

app = typer.Typer(add_completion=False, help="HireLoop: your AI job-search team.")
console = Console()


def _log(level: str = "WARNING", kind: str = "cli"):
    """Console at ``level``; the full log always goes to <data>/logs/."""
    from jobhunt.logs import configure

    return configure(kind, getattr(logging, level))


def main() -> None:
    app()


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


@app.command()
def doctor(
    skip_llm: bool = typer.Option(False, "--skip-llm", help="Skip the live LLM round-trip."),
    skip_net: bool = typer.Option(False, "--skip-net", help="Skip job-source reachability."),
) -> None:
    """Verify every dependency. Run this first, and after any config change."""
    results: list[tuple[str, bool, str]] = []

    def check(name: str, fn) -> None:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append((name, ok, detail))

    # --- runtime ---------------------------------------------------------
    def _python():
        v = sys.version_info
        return v >= (3, 11), f"{v.major}.{v.minor}.{v.micro}"

    check("python >= 3.11", _python)

    def _imports():
        missing = []
        for mod in ("sqlmodel", "httpx", "yaml", "jinja2", "rapidfuzz", "tenacity", "fastapi"):
            try:
                __import__(mod)
            except ImportError:
                missing.append(mod)
        return not missing, ("all present" if not missing else f"missing: {', '.join(missing)}")

    check("core dependencies", _imports)

    # --- config ----------------------------------------------------------
    def _config():
        from jobhunt.config import company_tiers, targets

        t, c = targets(), company_tiers()
        if not t:
            return False, "jobhunt/defaults/targets.yaml missing or empty"
        floor = t["compensation"]["india_min_ctc_lpa"]
        n = len(c.get("companies", []))
        shadow = t["autonomy"]["shadow_mode"]
        return True, f"floor ₹{floor}L · {n} companies · shadow_mode={shadow}"

    check("default settings", _config)

    # --- database --------------------------------------------------------
    def _db():
        from sqlmodel import SQLModel

        from jobhunt.config import settings
        from jobhunt.store.db import init_db

        init_db()
        return True, f"{settings().db_path.name} · {len(SQLModel.metadata.tables)} tables"

    check("sqlite schema", _db)

    # --- llm -------------------------------------------------------------
    if skip_llm:
        results.append(("llm backend", True, "skipped (--skip-llm)"))
    else:
        def _llm():
            from jobhunt.llm.adapter import get_backend

            be = get_backend()
            ok, detail = be.healthcheck()
            return ok, f"[{be.name}] {detail}"

        check("llm backend", _llm)

    # --- playwright ------------------------------------------------------
    def _playwright():
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return False, "not installed - pip install playwright && playwright install chromium"
        from jobhunt.sources.browser.session import launch_with_fallback

        with sync_playwright() as p:
            try:
                browser = launch_with_fallback(p.chromium.launch, headless=True)
                version = browser.version
                browser.close()
            except Exception as exc:  # noqa: BLE001
                # Playwright's message is a 20-line ASCII box; the first line
                # is the only part that identifies the problem.
                first = str(exc).splitlines()[0][:120]
                return False, f"run: playwright install chromium  ({first})"
        return True, f"chromium {version}"

    check("playwright", _playwright)

    # --- job sources -----------------------------------------------------
    if skip_net:
        results.append(("job sources", True, "skipped (--skip-net)"))
    else:
        def _sources():
            import httpx

            probes = {
                "greenhouse": "https://boards-api.greenhouse.io/v1/boards/rubrik/jobs",
                "ashby": "https://api.ashbyhq.com/posting-api/job-board/cursor",
                "lever": "https://api.lever.co/v0/postings/cred?mode=json",
                "remotive": "https://remotive.com/api/remote-jobs?limit=1",
            }
            up = []
            for name, url in probes.items():
                try:
                    r = httpx.get(url, timeout=12, follow_redirects=True)
                    up.append(f"{name}{'✓' if r.status_code == 200 else '✗'}")
                except Exception:  # noqa: BLE001
                    up.append(f"{name}✗")
            good = sum(1 for x in up if "✓" in x)
            return good >= 3, " ".join(up)

        check("job sources", _sources)

    # --- profile ---------------------------------------------------------
    def _profile():
        from jobhunt.config import settings

        p = settings().profile_path
        if not p.exists():
            return False, "not created yet - open the dashboard (python run.py web)"
        return True, str(p)

    check("master profile", _profile)

    # --- linkedin session ------------------------------------------------
    def _linkedin():
        from jobhunt.sources.browser.session import is_logged_in

        ok, detail = is_logged_in("linkedin")
        return ok, detail

    if skip_net:
        results.append(("linkedin session (optional)", True, "skipped (--skip-net)"))
    else:
        check("linkedin session (optional)", _linkedin)

    # --- gmail -----------------------------------------------------------
    def _gmail():
        from jobhunt.sources.gmail import is_configured

        return is_configured()

    check("gmail (optional)", _gmail)

    # --- contact discovery ------------------------------------------------
    def _cse():
        from jobhunt.config import settings

        cfg = settings()
        if cfg.google_cse_api_key and cfg.google_cse_cx:
            return True, "Programmable Search configured (100 queries/day free)"
        return False, "no CSE key - recruiter search falls back to DuckDuckGo"

    check("google search (optional)", _cse)

    # --- report ----------------------------------------------------------
    table = Table(title="doctor", show_lines=False, header_style="bold")
    table.add_column("check", style="cyan", no_wrap=True)
    table.add_column("", width=3)
    table.add_column("detail", overflow="fold")

    required_failed = 0
    for name, ok, detail in results:
        optional = "optional" in name
        mark = "[green]OK[/]" if ok else ("[yellow]--[/]" if optional else "[red]XX[/]")
        table.add_row(name, mark, detail)
        if not ok and not optional:
            required_failed += 1

    console.print(table)
    if required_failed:
        console.print(f"\n[red]{required_failed} required check(s) failed.[/] See details above.")
        raise typer.Exit(1)
    console.print("\n[green]All required checks passed.[/]")


# --------------------------------------------------------------------------
# placeholders wired in later milestones
# --------------------------------------------------------------------------


@app.command()
def onboard(
    resume: str = typer.Option("", "--resume", help="Path to the resume PDF."),
    yes: bool = typer.Option(False, "--yes", help="Accept defaults, ask nothing."),
    no_llm: bool = typer.Option(False, "--no-llm", help="Force the deterministic parser."),
) -> None:
    """Build the master profile from your resume PDF. Run this once."""
    from pathlib import Path

    from jobhunt.agents import onboard as agent

    agent.run(
        Path(resume) if resume else None,
        interactive=not yes,
        use_llm=not no_llm,
    )


@app.command()
def source(
    dry_run: bool = typer.Option(False, "--dry-run", help="Report counts, write nothing."),
    no_boards: bool = typer.Option(False, "--no-boards", help="Skip company ATS boards."),
    no_feeds: bool = typer.Option(False, "--no-feeds", help="Skip the free remote feeds."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Pull jobs from every source, dedupe, and store them."""
    from jobhunt.agents import scout
    from jobhunt.store.db import init_db

    _log("INFO" if verbose else "WARNING")
    init_db()

    with console.status("sourcing…", spinner="dots"):
        stats = scout.run(dry_run=dry_run, boards=not no_boards, feeds=not no_feeds)

    src = Table(title="sources", header_style="bold")
    src.add_column("source", style="cyan")
    src.add_column("jobs", justify="right")
    for name, count in sorted(stats.by_source.items(), key=lambda kv: -kv[1]):
        src.add_row(name, str(count))
    console.print(src)

    funnel = Table(title="funnel", header_style="bold")
    funnel.add_column("stage", style="cyan")
    funnel.add_column("count", justify="right")
    funnel.add_row("raw pulled", str(stats.raw))
    funnel.add_row("- exact duplicates", f"-{stats.exact_duplicates}")
    funnel.add_row("- near duplicates", f"-{stats.near_duplicates}")
    funnel.add_row("- already in db", f"-{stats.already_known}")
    funnel.add_row("[bold]new[/]", f"[bold]{stats.new}[/]")
    console.print(funnel)

    if dry_run:
        console.print("[yellow]--dry-run: nothing was written.[/]")


@app.command()
def discover(
    names: str = typer.Option("", "--names", help="Comma-separated company names."),
    from_file: str = typer.Option("", "--from-file", help="JSON list with a 'name' key each."),
    workday: bool = typer.Option(True, "--workday/--no-workday", help="Also brute-probe Workday."),
    append: bool = typer.Option(False, "--append", help="Append hits to jobhunt/defaults/company_tiers.yaml."),
) -> None:
    """Find which ATS hosts a company's board, and under what slug."""
    import json
    from pathlib import Path

    import yaml

    from jobhunt.config import CONFIG, company_tiers
    from jobhunt.sources.base import Fetcher
    from jobhunt.sources.discover import discover as find_board

    _log("INFO")

    wanted: list[str] = [n.strip() for n in names.split(",") if n.strip()]
    if from_file:
        data = json.loads(Path(from_file).read_text(encoding="utf-8"))
        wanted += [d["name"] if isinstance(d, dict) else str(d) for d in data]
    if not wanted:
        console.print("[red]Give --names or --from-file.[/]")
        raise typer.Exit(1)

    known = {c["name"].lower() for c in company_tiers().get("companies", [])}
    todo = [n for n in dict.fromkeys(wanted) if n.lower() not in known]
    console.print(f"probing {len(todo)} companies ({len(wanted) - len(todo)} already configured)")

    found = []
    for i, name in enumerate(todo, 1):
        with Fetcher() as f:
            board = find_board(f, name, include_workday=workday)
        if board:
            found.append(board)
        console.print(
            f"  [{i}/{len(todo)}] {name[:30]:30} "
            + (f"[green]{board.ats}/{board.slug}[/] ({board.total})" if board else "[dim]-[/]")
        )

    console.print("")
    console.print(f"[green]{len(found)}[/] of {len(todo)} have a public board")
    if found and append:
        path = CONFIG / "company_tiers.yaml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc["companies"].extend(b.as_config() for b in found)
        path.write_text(
            yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=110),
            encoding="utf-8",
        )
        console.print(f"appended {len(found)} to {path}")
    elif found:
        console.print("[dim]re-run with --append to add them to the config[/]")


def _gate_audit(gatekeeper, *, fix: bool) -> None:
    """Show what a config change means for work already past the gate."""
    report = gatekeeper.audit_progressed(apply_changes=fix)
    verb = "corrected" if fix else "would change"

    console.print(
        f"[dim]re-checked {report.checked} job(s) already past the gate[/]"
    )

    if report.rejected:
        t = Table(
            title=f"no longer passes the level rules ({verb})", header_style="bold red"
        )
        for col in ("id", "score", "status", "company", "title", "why"):
            t.add_column(col, style="cyan" if col != "why" else "red")
        for f in sorted(report.rejected, key=lambda f: -(f.score or 0)):
            t.add_row(
                str(f.job_id),
                f"{f.score:.2f}" if f.score else "-",
                f.status,
                f.company,
                f.title[:44],
                f.reason,
            )
        console.print(t)

    if report.rebanded:
        t = Table(title=f"level band {verb}", header_style="bold yellow")
        for col in ("id", "score", "company", "title", "band"):
            t.add_column(col, style="cyan")
        for f in sorted(report.rebanded, key=lambda f: -(f.score or 0)):
            t.add_row(
                str(f.job_id),
                f"{f.score:.2f}" if f.score else "-",
                f.company,
                f.title[:44],
                f"{f.old_band} -> {f.new_band}",
            )
        console.print(t)

    if not report.rejected and not report.rebanded:
        console.print("[green]everything past the gate still passes today's rules.[/]")
    elif not fix:
        console.print("[dim]nothing was written. Re-run with --fix to apply.[/]")


@app.command()
def gate(
    limit: int = typer.Option(0, "--limit", help="Only process this many jobs."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    reset: bool = typer.Option(
        False, "--reset", help="Re-filter every job. Use after changing the filters."
    ),
    audit: bool = typer.Option(
        False,
        "--audit",
        help="Re-check already-scored/tailored jobs against today's rules. Reports only.",
    ),
    fix: bool = typer.Option(
        False, "--fix", help="With --audit: actually apply the corrections."
    ),
) -> None:
    """Deterministic filters: level, work authorisation, compensation."""
    from jobhunt.agents import gatekeeper
    from jobhunt.store.db import init_db

    init_db()

    if audit:
        _gate_audit(gatekeeper, fix=fix)
        if not reset:
            return

    if reset:
        n = gatekeeper.reset()
        console.print(f"[yellow]reset {n} jobs to unfiltered[/]")
    with console.status("filtering…", spinner="dots"):
        stats = gatekeeper.run(limit=limit or None, dry_run=dry_run)

    funnel = Table(title="gatekeeper", header_style="bold")
    funnel.add_column("stage", style="cyan")
    funnel.add_column("count", justify="right")
    funnel.add_row("considered", str(stats.considered))
    funnel.add_row("- wrong level", f"-{stats.rejected_level}")
    funnel.add_row("- US-only, no sponsorship", f"-{stats.rejected_auth}")
    funnel.add_row("- below comp floor", f"-{stats.rejected_comp}")
    funnel.add_row("[bold]passed[/]", f"[bold]{stats.passed}[/]")
    console.print(funnel)

    breakdown = Table(title="survivors", header_style="bold")
    breakdown.add_column("level band", style="cyan")
    breakdown.add_column("n", justify="right")
    breakdown.add_column("eligibility", style="cyan")
    breakdown.add_column("n", justify="right")
    bands = [(k, v) for k, v in stats.by_band.items() if k != "reject"]
    elig = list(stats.by_eligibility.items())
    for i in range(max(len(bands), len(elig))):
        b = bands[i] if i < len(bands) else ("", "")
        e = elig[i] if i < len(elig) else ("", "")
        breakdown.add_row(str(b[0]), str(b[1]), str(e[0]), str(e[1]))
    console.print(breakdown)

    if dry_run:
        console.print("[yellow]--dry-run: nothing was written.[/]")


@app.command()
def score(
    limit: int = typer.Option(10, "--limit", help="How many gated jobs to score."),
    explain: bool = typer.Option(False, "--explain", help="Show per-dimension reasoning."),
    batch: int = typer.Option(5, "--batch", help="Postings per LLM call."),
    eligible_only: bool = typer.Option(
        False, "--eligible-only", help="Only jobs you can actually take (India / sponsored)."
    ),
    reroute: bool = typer.Option(
        False, "--reroute", help="Re-apply the threshold to existing scores. No LLM calls."
    ),
) -> None:
    """Analyst: LLM fit scoring over jobs that cleared the Gatekeeper."""
    from jobhunt.agents import analyst
    from jobhunt.store.db import init_db
    from jobhunt.store.models import Eligibility

    _log("WARNING")
    init_db()

    if reroute:
        moved = analyst.reroute()
        console.print(
            f"re-routed with the current threshold: "
            f"[green]+{moved['to_scored']}[/] promoted, "
            f"[yellow]{moved['to_archived']}[/] archived, "
            f"{moved['unchanged']} unchanged"
        )

    with console.status(f"scoring up to {limit} jobs…", spinner="dots"):
        stats = analyst.run(
            limit=limit or None,
            batch_size=batch,
            eligibility=(
                [Eligibility.INDIA_ELIGIBLE, Eligibility.SPONSORSHIP_OFFERED]
                if eligible_only
                else None
            ),
        )

    summary = Table(title="analyst", header_style="bold")
    summary.add_column("stage", style="cyan")
    summary.add_column("count", justify="right")
    summary.add_row("scored", str(stats.scored))
    summary.add_row("llm calls", str(stats.llm_calls))
    summary.add_row("eligibility resolved", str(stats.reclassified))
    summary.add_row("archived below threshold", str(stats.archived))
    summary.add_row("[bold]above threshold[/]", f"[bold]{stats.above_threshold}[/]")
    if stats.failed:
        summary.add_row("[red]failed[/]", f"[red]{stats.failed}[/]")
    console.print(summary)

    results = analyst.top(15)
    if not results:
        console.print("[yellow]Nothing above threshold yet.[/]")
        return

    board = Table(title="top matches", header_style="bold")
    board.add_column("score", justify="right", style="green")
    board.add_column("company", style="cyan")
    board.add_column("title")
    board.add_column("location")
    board.add_column("band")
    for j in results:
        board.add_row(
            f"{j.score:.2f}", j.company[:16], j.title[:42], (j.location or "")[:22],
            j.level_band.value,
        )
    console.print(board)

    if explain:
        for j in results[:6]:
            dims = (j.score_detail or {}).get("dimensions", {})
            bits = "  ".join(f"{k.split('_')[0]}={v}" for k, v in dims.items())
            console.print(f"\n[bold cyan]{j.company}[/] — {j.title}  [green]{j.score:.2f}[/]")
            console.print(f"  [dim]{bits}[/]")
            console.print(f"  {j.score_summary}")
            console.print(f"  [dim]{j.url}[/]")


@app.command()
def tailor(
    limit: int = typer.Option(3, "--limit", help="How many top-scored jobs to tailor for."),
    job_id: int = typer.Option(0, "--job-id", help="Tailor for one specific job."),
) -> None:
    """Rewrite the resume per job, fact-check it, render and verify the PDF."""
    from jobhunt.agents import tailor as agent
    from jobhunt.store.db import init_db

    _log("WARNING")
    init_db()

    with console.status("tailoring…", spinner="dots"):
        stats, results = agent.run(limit=limit, job_id=job_id or None)

    t = Table(title="tailor", header_style="bold")
    t.add_column("outcome", style="cyan")
    t.add_column("n", justify="right")
    t.add_row("attempted", str(stats.attempted))
    t.add_row("[green]rendered[/]", f"[green]{stats.succeeded}[/]")
    if stats.blocked_fabrication:
        t.add_row("[red]blocked: unverifiable claims[/]", f"[red]{stats.blocked_fabrication}[/]")
    if stats.failed_ats:
        t.add_row("[yellow]queued: ATS coverage[/]", f"[yellow]{stats.failed_ats}[/]")
    if stats.failed_llm:
        t.add_row("[red]llm error[/]", f"[red]{stats.failed_llm}[/]")
    console.print(t)

    for r in results:
        head = f"[bold]job {r.job_id}[/] — attempt {r.attempts}"
        if r.ok and r.ats:
            console.print(
                f"\n{head}  [green]OK[/]  ATS {r.ats.coverage:.0%}, "
                f"{r.ats.pages}p, {r.ats.char_count} chars"
            )
            console.print(f"  [dim]{r.pdf_path}[/]")
            if r.changes:
                console.print(f"  {r.changes[:260]}")
        else:
            console.print(f"\n{head}  [red]{r.error}[/]")
            for v in r.violations[:6]:
                console.print(f"  [red]·[/] {v}")


@app.command()
def login(site: str = typer.Option("linkedin", "--site", help="linkedin | glassdoor")) -> None:
    """Open a browser so you can sign in. No password is ever read or stored."""
    from jobhunt.sources.browser.session import interactive_login, is_logged_in

    ok, detail = is_logged_in(site)
    if ok:
        console.print(f"[green]Already signed in to {site}.[/] {detail}")
        return

    console.print(f"Opening {site}. Sign in yourself in the window that appears.")
    if interactive_login(site):
        console.print("[green]Signed in.[/] The session persists for future runs.")
    else:
        console.print("[red]Still not signed in.[/] Try again, or finish any 2FA prompt first.")
        raise typer.Exit(1)


@app.command()
def referrals(
    limit: int = typer.Option(3, "--limit", help="How many ready jobs to work."),
    per_job: int = typer.Option(1, "--per-job", help="Drafts per job."),
    show_browser: bool = typer.Option(False, "--show-browser", help="Run headed, to watch."),
    sweep: bool = typer.Option(False, "--sweep", help="Only expire overdue SLAs."),
) -> None:
    """Find who could refer you, and draft the ask. Nothing is sent."""
    from jobhunt.agents import connector
    from jobhunt.sources.browser.linkedin import daily_budget
    from jobhunt.store.db import init_db

    _log("WARNING")
    init_db()

    if sweep:
        n = connector.expire_overdue()
        console.print(f"expired {n} overdue referral(s); those jobs may now proceed")
        return

    budget = daily_budget()
    console.print(
        f"[dim]budget today: {budget['searches_left']}/{budget['search_cap']} searches, "
        f"{budget['views_left']}/{budget['view_cap']} profile views[/]"
    )

    with console.status("searching LinkedIn...", spinner="dots"):
        stats = connector.run(limit=limit, per_job=per_job, headless=not show_browser)

    t = Table(title="connector", header_style="bold")
    t.add_column("stage", style="cyan")
    t.add_column("n", justify="right")
    t.add_row("jobs considered", str(stats.jobs_considered))
    t.add_row("searches run", str(stats.searches))
    t.add_row("people found", str(stats.contacts_found))
    t.add_row("[bold]drafts queued[/]", f"[bold]{stats.drafts_queued}[/]")
    if stats.skipped_no_contacts:
        t.add_row("no contacts found", str(stats.skipped_no_contacts))
    console.print(t)
    for err in stats.errors[:5]:
        console.print(f"[red]{err[:140]}[/]")
    if stats.drafts_queued:
        console.print("Review and send them from the dashboard - nothing was sent.")


@app.command()
def handover(
    job_id: int = typer.Option(..., "--job-id", help="Which job to prepare."),
    minutes: int = typer.Option(10, "--minutes", help="How long to leave the window open."),
) -> None:
    """Fill an application in a visible browser and hand it to you to submit.

    The typing is automated, the submission is you. Ashby refused two
    automated submissions as possible spam, which is the ATS declining
    automation - so the part that carries your name is yours to click.
    """
    from jobhunt.agents.applicant import handover as run_handover
    from jobhunt.store.db import init_db

    _log("INFO")
    init_db()

    console.print("[cyan]Opening the form. Fill gaps if any, then press Submit in that window.[/]")
    result = run_handover(job_id, timeout_s=minutes * 60)

    tone = {"submitted": "green", "refused": "red", "abandoned": "yellow", "error": "red"}
    console.print(f"\n[{tone.get(result.outcome, 'white')}]{result.outcome.upper()}[/] — {result.detail}")
    if result.filled:
        console.print(f"[dim]filled {len(result.filled)}: {', '.join(sorted(result.filled))}[/]")
    for u in result.unanswered[:8]:
        console.print(f"  [yellow]left blank:[/] {u}")
    if result.screenshot:
        console.print(f"[dim]{result.screenshot}[/]")


@app.command()
def apply(
    limit: int = typer.Option(3, "--limit", help="How many ready jobs to work."),
    show_browser: bool = typer.Option(False, "--show-browser", help="Run headed, to watch."),
) -> None:
    """Fill ATS forms. Submits only clean forms, and only outside shadow mode."""
    from jobhunt.agents import applicant
    from jobhunt.config import targets
    from jobhunt.store.db import init_db, remaining_quota

    _log("WARNING")
    init_db()

    if targets().get("autonomy", {}).get("shadow_mode", True):
        console.print(
            "[yellow]Shadow mode is on[/] - forms will be filled and screenshotted, "
            "but nothing is submitted."
        )
    console.print(f"[dim]{remaining_quota('application')} applications left in today's cap[/]")

    with console.status("filling forms...", spinner="dots"):
        stats, results = applicant.run(limit=limit, headless=not show_browser)

    t = Table(title="applicant", header_style="bold")
    t.add_column("outcome", style="cyan")
    t.add_column("n", justify="right")
    t.add_row("considered", str(stats.considered))
    t.add_row("[green]submitted[/]", f"[green]{stats.submitted}[/]")
    t.add_row("[yellow]queued for you[/]", f"[yellow]{stats.queued}[/]")
    if stats.failed:
        t.add_row("[red]failed[/]", f"[red]{stats.failed}[/]")
    if stats.quota_blocked:
        t.add_row("blocked by daily cap", str(stats.quota_blocked))
    if stats.company_capped:
        t.add_row("held: per-company cap", str(stats.company_capped))
    console.print(t)

    for r in results:
        head = f"job {r.job_id} [{r.ats}]"
        if r.submitted:
            console.print(f"[green]{head} submitted[/]")
        elif r.error:
            console.print(f"[red]{head} {r.error}[/]")
        else:
            console.print(f"[yellow]{head} queued[/] - {r.queued_reason}")
        if r.filled:
            console.print(f"  [dim]filled: {', '.join(sorted(r.filled))}[/]")
        for q in r.essay_questions[:3]:
            console.print(f"  [cyan]needs your answer:[/] {q}")
        if r.screenshot:
            console.print(f"  [dim]{r.screenshot}[/]")


@app.command()
def outreach(
    limit: int = typer.Option(5, "--limit"),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip SMTP address checks."),
    send: bool = typer.Option(False, "--send", help="Send everything you approved."),
    followups: bool = typer.Option(False, "--followups", help="Send due day-3/day-7 nudges."),
) -> None:
    """Find recruiters and draft cold emails. Sending is a separate, capped step."""
    from jobhunt.agents import outreach as agent
    from jobhunt.store.db import init_db

    _log("WARNING")
    init_db()

    if send:
        stats = agent.send_approved()
        console.print(f"sent [green]{stats.sent}[/]; {stats.quota_left} left in today's cap")
    elif followups:
        stats = agent.send_followups()
        console.print(f"follow-ups sent: [green]{stats.followups_sent}[/]")
    else:
        with console.status("finding recruiters...", spinner="dots"):
            stats = agent.run(limit=limit, verify=not no_verify)
        t = Table(title="outreach", header_style="bold")
        t.add_column("stage", style="cyan")
        t.add_column("n", justify="right")
        t.add_row("jobs considered", str(stats.jobs_considered))
        t.add_row("contacts found", str(stats.contacts_found))
        t.add_row("[bold]drafts queued[/]", f"[bold]{stats.drafts_queued}[/]")
        t.add_row("queued as LinkedIn instead", str(stats.skipped_low_confidence))
        t.add_row("daily email cap left", str(stats.quota_left))
        console.print(t)
        console.print("Review them in the dashboard; nothing is sent until you approve.")
    for err in stats.errors[:5]:
        console.print(f"[red]{err[:140]}[/]")


@app.command()
def track(days: int = typer.Option(30, "--days"), limit: int = typer.Option(60, "--limit")) -> None:
    """Read application replies from Gmail and advance the pipeline."""
    from jobhunt.agents import tracker
    from jobhunt.sources.gmail import is_configured
    from jobhunt.store.db import init_db

    _log("WARNING")
    init_db()

    ok, detail = is_configured()
    if not ok:
        console.print(f"[yellow]Gmail not set up:[/] {detail}")
        raise typer.Exit(1)

    with console.status("reading replies...", spinner="dots"):
        stats = tracker.run(days=days, limit=limit)

    t = Table(title="tracker", header_style="bold")
    t.add_column("stage", style="cyan")
    t.add_column("n", justify="right")
    t.add_row("messages read", str(stats.messages_read))
    t.add_row("matched to an application", str(stats.matched))
    t.add_row("[bold]statuses updated[/]", f"[bold]{stats.updated}[/]")
    t.add_row("replies marked", str(stats.replies_marked))
    t.add_row("llm calls", str(stats.llm_calls))
    console.print(t)


@app.command("gmail-auth")
def gmail_auth() -> None:
    """Authorise Gmail. Opens your browser; the token stays on this machine."""
    from jobhunt.sources.gmail import search, token_path

    try:
        search("in:inbox", limit=1)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    console.print(f"[green]Authorised.[/] Token saved to {token_path()}")


@app.command()
def run_now(
    which: str = typer.Argument("morning", help="morning | evening | refilter"),
) -> None:
    """Run one pass immediately, in this terminal."""
    from jobhunt.graph.pipeline import ProfileMissing, RunInProgress, evening, morning, refilter
    from jobhunt.store.db import init_db

    runs = {"morning": morning, "evening": evening, "refilter": refilter}
    if which not in runs:
        console.print(f"[red]Unknown run {which!r}.[/] Choose: {', '.join(runs)}")
        raise typer.Exit(2)

    _log("INFO")
    init_db()

    try:
        report = runs[which]()
    except (RunInProgress, ProfileMissing) as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(1) from exc

    t = Table(title=f"{which} run", header_style="bold")
    t.add_column("stage", style="cyan")
    t.add_column("result", overflow="fold")
    for name, result in report.stages.items():
        t.add_row(name, str(result)[:110])
    for name, err in report.errors.items():
        t.add_row(f"[red]{name}[/]", f"[red]{err[:110]}[/]")
    console.print(t)
    if report.errors:
        raise typer.Exit(1)


@app.command()
def digest() -> None:
    """What happened, and what needs you."""
    from jobhunt.graph.pipeline import digest as build
    from jobhunt.store.db import init_db

    init_db()
    data = build()

    p = data["pipeline"]
    stats = Table(title="pipeline", header_style="bold")
    for key in p:
        stats.add_column(key, justify="right")
    stats.add_row(*[str(v) for v in p.values()])
    console.print(stats)

    needs = data["needs_you"]
    console.print("")
    console.print(
        f"[bold]Needs you:[/] {len(needs['jobs'])} job(s), "
        f"{needs['referrals']} referral draft(s), {needs['outreach']} outreach draft(s)"
    )
    for j in needs["jobs"][:8]:
        console.print(f"  [yellow]{j['score']:.2f}[/] {j['company']} - {j['title'][:50]}")

    if data["ready_to_apply"]:
        console.print("")
        console.print("[bold]Tailored and ready:[/]")
        for j in data["ready_to_apply"][:8]:
            console.print(f"  [green]{j['score']:.2f}[/] {j['company']} - {j['title'][:50]}")

    if data["applications"]:
        console.print("")
        console.print(f"[bold]Applications:[/] {data['applications']}")


@app.command()
def schedule(
    install: bool = typer.Option(False, "--install", help="Print the Windows task command."),
) -> None:
    """Run the daily scheduler headless, without the dashboard.

    Most people should instead switch the schedule on from the dashboard's
    Runs page. Use this on a machine where the dashboard is not left running.
    """
    from jobhunt.graph.scheduler import run_forever, windows_task_command
    from jobhunt.store.db import init_db

    if install:
        console.print("Run this once, in an ordinary terminal:")
        console.print(f"[cyan]{windows_task_command()}[/]")
        return

    _log("INFO", kind="scheduler")
    init_db()
    run_forever()


@app.command()
def web(
    port: int = typer.Option(8787, "--port"),
    host: str = typer.Option(
        "127.0.0.1", "--host",
        help="Use 0.0.0.0 to reach it from your phone on the same wifi.",
    ),
    open_browser: bool = typer.Option(
        True, "--open/--no-open", help="Open the dashboard in your browser."
    ),
) -> None:
    """The dashboard: first-run setup, filters, runs, schedule and the queue."""
    import threading
    import webbrowser

    from jobhunt.config import profile_exists
    from jobhunt.store.db import init_db
    from jobhunt.web.app import serve

    log_path = _log("WARNING", kind="web")
    init_db()
    from jobhunt.config import settings

    if host != "127.0.0.1" and not settings().jobhunt_read_only:
        console.print(
            "[yellow]Binding beyond localhost.[/] The database holds your full job "
            "search, including draft messages. Only do this on a network you trust."
        )
    url = f"http://{'127.0.0.1' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
    console.print(f"[green]Dashboard:[/] {url}")
    if not profile_exists():
        console.print("First run: the dashboard opens on the setup page.")
    console.print(f"[dim]Logs: {log_path}   Stop with Ctrl-C.[/]")
    if open_browser:
        threading.Timer(1.5, webbrowser.open, args=[url]).start()
    serve(host=host, port=port)



@app.command()
def demo(
    port: int = typer.Option(8788, "--port"),
    host: str = typer.Option("127.0.0.1", "--host", help="0.0.0.0 to serve it publicly."),
    data_dir: str = typer.Option(
        "demo-data", "--data-dir", help="Folder for the demo. Reset on every run."
    ),
    read_only: bool = typer.Option(
        False, "--read-only", help="Switch off every button that changes something (public demo)."
    ),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
) -> None:
    """Try the dashboard with a made-up candidate and sample jobs.

    Uses its own data folder, so your real profile and jobs are never touched.
    """
    import os
    from pathlib import Path

    from jobhunt.demo import seed

    if read_only:
        os.environ["JOBHUNT_READ_ONLY"] = "1"
    folder = seed(Path(data_dir))
    console.print(
        f"[green]Demo data:[/] {folder}  [dim](made-up candidate and companies; "
        "practice mode, nothing is ever submitted)[/]"
    )
    web(port=port, host=host, open_browser=open_browser)

if __name__ == "__main__":
    main()
