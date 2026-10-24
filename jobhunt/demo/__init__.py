"""A try-before-you-set-up demo: a made-up candidate and sample jobs.

``python run.py demo`` points the app at its own data folder, writes the
profile of a fictional candidate (Aarav Mehta) and a few jobs at fictional
companies in every stage - waiting for approval, ready, applied, archived,
filtered out - and opens the dashboard. Nothing in your real data folder is
read or touched, and nothing is applied to: the demo starts in practice mode
and every company and posting here is invented.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PROFILE = Path(__file__).with_name("profile.yaml")


def _ago(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _scores(skill: int, level: int, comp: int, elig: int, ai: int, company: int) -> dict[str, int]:
    return {
        "skill_overlap": skill,
        "level_realism": level,
        "comp_confidence": comp,
        "eligibility": elig,
        "ai_relevance": ai,
        "company_quality": company,
    }


# Every company and posting below is invented.
JOBS: list[dict[str, Any]] = [
    {
        "company": "Globex AI", "title": "Software Engineer II, Agents Platform",
        "location": "Bengaluru, India", "ats": "ashby", "status": "queued_approval",
        "scores": _scores(5, 4, 4, 5, 5, 4), "comp": (3_600_000, 5_200_000, "INR", False),
        "summary": "Python + agent tooling match your RAG and tool-calling work; level fits 1-3 years.",
    },
    {
        "company": "Acme Payments", "title": "Backend Engineer (Java, Spring Boot)",
        "location": "Bengaluru, India", "ats": "greenhouse", "status": "queued_approval",
        "scores": _scores(5, 4, 4, 5, 2, 4), "comp": (3_000_000, 4_200_000, "INR", True),
        "summary": "Spring Boot microservices and CI/CD ownership line up with your current role.",
    },
    {
        "company": "Initech Cloud", "title": "Full Stack Engineer, Developer Experience",
        "location": "Remote, India", "ats": "lever", "status": "queued_approval",
        "scores": _scores(4, 4, 3, 5, 3, 4), "comp": None,
        "summary": "React + TypeScript frontend with Playwright test ownership; remote within India.",
    },
    {
        "company": "Hooli", "title": "Software Engineer, Search Infrastructure",
        "location": "Hyderabad, India", "ats": "greenhouse", "status": "ready",
        "scores": _scores(4, 4, 4, 5, 3, 5), "comp": (3_800_000, 5_500_000, "INR", True),
        "summary": "Strong company, distributed backend; asks for 2+ years which you meet.",
    },
    {
        "company": "Pied Piper", "title": "AI Engineer, Retrieval",
        "location": "Bengaluru, India", "ats": "ashby", "status": "ready",
        "scores": _scores(5, 3, 3, 5, 5, 3), "comp": (2_800_000, 4_000_000, "INR", False),
        "summary": "Hybrid search, reranking and evals - close to your agent platform project.",
    },
    {
        "company": "Umbrella Health", "title": "SDE 1, Platform",
        "location": "Pune, India", "ats": "lever", "status": "scored",
        "scores": _scores(4, 5, 3, 5, 2, 3), "comp": None,
        "summary": "Good level fit; healthcare data platform, less AI work than you want.",
    },
    {
        "company": "Stark Logistics", "title": "Software Engineer, Routing",
        "location": "Gurugram, India", "ats": "smartrecruiters", "status": "scored",
        "scores": _scores(4, 4, 3, 5, 2, 3), "comp": None,
        "summary": "Java backend and graph algorithms; pay not published, estimated from tier.",
    },
    {
        "company": "Wayne Fintech", "title": "Machine Learning Engineer, Fraud",
        "location": "Mumbai, India", "ats": "greenhouse", "status": "scored",
        "scores": _scores(3, 3, 4, 5, 4, 4), "comp": (3_200_000, 4_600_000, "INR", True),
        "summary": "Production ML; you have less classical-ML depth than the posting asks for.",
    },
    {
        "company": "Massive Dynamic", "title": "Frontend Engineer II",
        "location": "Bengaluru, India", "ats": "ashby", "status": "applied",
        "scores": _scores(4, 4, 4, 5, 2, 4), "comp": (3_000_000, 4_000_000, "INR", False),
        "summary": "React and TypeScript heavy; good fit for your frontend integration work.",
        "application": ("interview", 9),
    },
    {
        "company": "Soylent Systems", "title": "Software Engineer, Integrations",
        "location": "Remote, India", "ats": "greenhouse", "status": "applied",
        "scores": _scores(5, 4, 3, 5, 3, 3), "comp": None,
        "summary": "Third-party integrations and webhooks - mirrors your current work closely.",
        "application": ("submitted", 3),
    },
    {
        "company": "Cyberdyne", "title": "Senior Staff Engineer, Distributed Systems",
        "location": "Bengaluru, India", "ats": "greenhouse", "status": "rejected_gate",
        "reason": "level: title says Senior Staff - above your target level",
    },
    {
        "company": "Tyrell Robotics", "title": "Software Engineer",
        "location": "San Francisco, CA", "ats": "lever", "status": "rejected_gate",
        "reason": "eligibility: US-only role with no visa sponsorship",
    },
    {
        "company": "Vandelay Retail", "title": "Software Engineer, Store Systems",
        "location": "Chennai, India", "ats": "lever", "status": "archived",
        "scores": _scores(3, 4, 2, 5, 1, 2), "comp": (1_200_000, 1_800_000, "INR", False),
        "summary": "Below your pay floor and little overlap with your AI interests.",
    },
]


def seed(data_dir: Path) -> Path:
    """Create (or reset) a demo data folder and return it."""
    import os

    from jobhunt.store import db

    # An open engine holds the old database file; Windows will not delete it.
    if db.get_engine.cache_info().currsize:
        db.get_engine().dispose()
        db.get_engine.cache_clear()

    data_dir = data_dir.resolve()
    if data_dir.exists():
        shutil.rmtree(data_dir)
    (data_dir / "profile").mkdir(parents=True)
    shutil.copyfile(PROFILE, data_dir / "profile" / "profile.yaml")

    os.environ["JOBHUNT_DATA_DIR"] = str(data_dir)
    from jobhunt.config import settings

    settings.cache_clear()
    db.init_db()

    from jobhunt.agents.analyst import weighted_score
    from jobhunt.store.models import (
        Application,
        Eligibility,
        Job,
        JobStatus,
        LevelBand,
        RunLog,
    )

    with db.session() as s:
        for i, spec in enumerate(JOBS):
            slug = spec["company"].lower().replace(" ", "")
            job = Job(
                source=spec["ats"],
                source_id=f"demo-{i}",
                url=f"https://example.com/careers/{slug}/{i}",
                ats=spec["ats"],
                ats_slug=slug,
                company=spec["company"],
                title=spec["title"],
                location=spec["location"],
                remote=spec["location"].startswith("Remote"),
                description=(
                    f"Sample posting for the demo. {spec['company']} is a made-up company.\n\n"
                    "You will build and run backend services, work with product and design, "
                    "and own features end to end. 1-3 years of experience with Python, Java or "
                    "TypeScript; experience with LLM-based features is a plus."
                ),
                posted_at=_ago(1 + i % 6),
                first_seen=_ago(1 + i % 6),
                dedupe_hash=f"demo{i:04d}",
                eligibility=(
                    Eligibility.US_ONLY_NO_SPONSORSHIP
                    if "CA" in spec["location"]
                    else Eligibility.INDIA_ELIGIBLE
                ),
                eligibility_reason="India location" if "India" in spec["location"] else "US only",
                level_band=LevelBand.AI_TRACK if "AI" in spec["title"] or "Machine" in spec["title"]
                else LevelBand.DIRECT,
                level_reason="title matches your target roles",
                status=JobStatus(spec["status"]),
                status_reason=spec.get("reason"),
            )
            if comp := spec.get("comp"):
                job.comp_min, job.comp_max, job.comp_currency, job.comp_is_estimated = comp
                job.comp_reason = (
                    "estimated from company tier" if job.comp_is_estimated else "published range"
                )
            elif spec["status"] != "rejected_gate":
                job.comp_reason = "not published - top-tier company, passes the floor"
            if scores := spec.get("scores"):
                job.score = weighted_score(scores)
                job.score_detail = {
                    "dimensions": scores, "weighted": job.score, "model_summary": spec["summary"],
                }
                job.score_summary = spec["summary"]
            s.add(job)
            s.flush()
            if app := spec.get("application"):
                state, days = app
                s.add(Application(
                    job_id=job.id, method="ats_manual", status=state,
                    applied_at=_ago(days), updated_at=_ago(max(days - 2, 0)),
                    notes="Demo application",
                ))

        s.add(RunLog(
            run_type="morning", started_at=_ago(0.2), finished_at=_ago(0.19), ok=True,
            stats={"scout": {"fetched": 412, "new": 37}, "gatekeeper": {"passed": 14},
                   "analyst": {"scored": 14, "above_threshold": 6}},
        ))
    return data_dir
