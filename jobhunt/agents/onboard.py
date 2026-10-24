"""Onboarding - build the master profile once, reuse it forever.

Parses the resume PDF, then fills the gaps a resume never contains (current
CTC, notice period, work authorisation) by asking. Defaults come from
``defaults/targets.yaml`` so the common path is just pressing Enter.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from jobhunt.config import settings, targets
from jobhunt.resume.extract import extract_profile
from jobhunt.resume.profile import Profile

console = Console()

SEARCH_DIRS = [
    Path.home() / "Downloads",
    Path.home() / "Documents",
    Path.home() / "Desktop",
]


def find_resume_candidates() -> list[Path]:
    """Most recently modified resume-looking PDFs, newest first."""
    hits: list[Path] = []
    for d in SEARCH_DIRS:
        if not d.is_dir():
            continue
        for p in d.glob("*.pdf"):
            if any(k in p.name.lower() for k in ("resume", "cv", "curriculum")):
                hits.append(p)
    return sorted(hits, key=lambda p: p.stat().st_mtime, reverse=True)[:10]


def choose_resume(explicit: Path | None) -> Path:
    if explicit:
        if not explicit.exists():
            raise FileNotFoundError(explicit)
        return explicit

    candidates = find_resume_candidates()
    if not candidates:
        raw = Prompt.ask("Path to your resume PDF")
        return Path(raw.strip().strip('"'))

    console.print("\n[bold]Resumes found:[/]")
    for i, p in enumerate(candidates, 1):
        console.print(f"  [cyan]{i}[/]. {p.name}  [dim]({p.parent})[/]")
    console.print("  [cyan]0[/]. enter a different path")

    idx = IntPrompt.ask("Which one", default=1, choices=[str(i) for i in range(len(candidates) + 1)])
    if idx == 0:
        return Path(Prompt.ask("Path to your resume PDF").strip().strip('"'))
    return candidates[idx - 1]


def summarise(profile: Profile, method: str) -> None:
    t = Table(title=f"parsed profile  [{method}]", header_style="bold", show_lines=False)
    t.add_column("field", style="cyan", no_wrap=True)
    t.add_column("value", overflow="fold")

    ident = profile.identity
    t.add_row("name", ident.name or "[red]missing[/]")
    t.add_row("email", ident.email or "[red]missing[/]")
    t.add_row("phone", ident.phone or "[dim]-[/]")
    for edu in profile.education:
        t.add_row("education", f"{edu.degree or '?'} · {edu.institution} · {edu.grade} "
                               f"({edu.start}-{edu.end})")
    for e in profile.experience:
        t.add_row(
            "experience",
            f"[bold]{e.title}[/] @ {e.company} · {e.location} "
            f"({e.start} - {e.end}) · {len(e.bullets)} bullets",
        )
    for p in profile.projects:
        t.add_row("project", f"{p.name} · {len(p.bullets)} bullets · {', '.join(p.tech[:5])}")
    t.add_row("skills", f"{len(profile.all_skills())} — {', '.join(profile.all_skills()[:10])}…")
    t.add_row("achievements", str(len(profile.achievements)))
    console.print(t)


def _ask_float(label: str, current: float | None) -> float | None:
    """A number, or None when left blank. Nothing is assumed on your behalf."""
    raw = Prompt.ask(label, default="" if current is None else f"{current:g}")
    try:
        return float(raw) if raw.strip() else None
    except ValueError:
        console.print(f"[yellow]{raw!r} is not a number - left blank.[/]")
        return None


def _ask_int(label: str, current: int | None) -> int | None:
    value = _ask_float(label, None if current is None else float(current))
    return None if value is None else int(value)


def fill_gaps(profile: Profile, *, interactive: bool = True) -> Profile:
    """Ask for the things a resume never states.

    Blank answers stay blank: an application form question the profile cannot
    answer is reported as needing you, never filled with a guess.
    """
    cfg = targets().get("compensation", {})
    prefs = profile.preferences

    if not interactive:
        prefs.expected_min_ctc_lpa = prefs.expected_min_ctc_lpa or float(
            cfg.get("india_min_ctc_lpa", 45)
        )
        if not prefs.preferred_locations and profile.identity.location:
            prefs.preferred_locations = [profile.identity.location.split(",")[0].strip()]
        return profile

    console.print("\n[bold]A few things your resume does not say.[/] "
                  "[dim]Press Enter to keep the value shown; leave blank to skip.[/]\n")

    prefs.current_ctc_lpa = _ask_float("Current CTC (LPA)", prefs.current_ctc_lpa)
    prefs.expected_min_ctc_lpa = _ask_float(
        "Minimum CTC you would move for (LPA)",
        prefs.expected_min_ctc_lpa or float(cfg.get("india_min_ctc_lpa", 45)),
    )
    prefs.notice_period_days = _ask_int("Notice period (days)", prefs.notice_period_days)
    locations = Prompt.ask(
        "Preferred locations (comma separated)",
        default=", ".join(prefs.preferred_locations) or "Remote",
    )
    prefs.preferred_locations = [x.strip() for x in locations.split(",") if x.strip()]
    profile.answers.preferred_locations = list(prefs.preferred_locations)
    prefs.open_to_remote = Confirm.ask("Open to remote?", default=prefs.open_to_remote)
    prefs.willing_to_relocate = Confirm.ask(
        "Willing to relocate?", default=prefs.willing_to_relocate
    )
    profile.answers.willing_to_relocate_domestic = prefs.willing_to_relocate
    prefs.work_authorization = Prompt.ask(
        "Work authorisation (e.g. 'Indian citizen; needs sponsorship for US roles')",
        default=prefs.work_authorization or "",
    )
    prefs.years_experience = _ask_float(
        "Years of professional experience (blank = work it out from your dates)",
        prefs.years_experience,
    )

    ident = profile.identity
    if not ident.linkedin or "linkedin.com" not in ident.linkedin:
        ident.linkedin = Prompt.ask("LinkedIn URL", default=ident.linkedin or "")
    if not ident.github or "github.com" not in ident.github:
        ident.github = Prompt.ask("GitHub URL", default=ident.github or "")
    if not ident.location:
        ident.location = Prompt.ask("Current city and country (e.g. 'Pune, India')")

    return profile


def run(resume: Path | None = None, *, interactive: bool = True, use_llm: bool = True) -> Profile:
    path = choose_resume(resume)
    console.print(f"\nReading [cyan]{path.name}[/] …")

    profile, method = extract_profile(path, prefer_llm=use_llm)
    if method == "heuristic":
        console.print(
            "[yellow]LLM unavailable - used the deterministic parser.[/] "
            "Check the table below carefully; re-run after `claude /login` for a better parse."
        )
    summarise(profile, method)

    if interactive and not Confirm.ask("\nDoes this look right", default=True):
        console.print(
            f"Edit [cyan]{settings().profile_path}[/] by hand after saving - "
            "it is plain YAML and is the source of truth for every tailored resume."
        )

    profile = fill_gaps(profile, interactive=interactive)
    profile.save(settings().profile_path)
    console.print(f"\n[green]Saved[/] {settings().profile_path}")

    facts = profile.facts()
    console.print(
        f"[dim]Fact registry: {len(facts.entities)} entities, {len(facts.numbers)} numbers. "
        "Nothing outside this may appear on a tailored resume.[/]"
    )
    return profile
