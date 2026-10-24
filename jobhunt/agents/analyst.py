"""Analyst - LLM fit scoring over jobs that cleared the Gatekeeper.

Two jobs at once:

* score each posting 1-5 across six dimensions, so a rejection is auditable
  rather than a bare number;
* resolve the ``UNKNOWN`` work-authorisation verdicts the Gatekeeper
  deliberately deferred - a US posting that never mentions sponsorship needs
  the whole JD read before it is thrown away.

Jobs are batched so one call covers several postings. The profile lives in the
system prompt, which is identical across every call in a run and therefore
cacheable; only the batch varies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlmodel import select

from jobhunt.config import company_tiers, settings, targets
from jobhunt.llm.adapter import MODEL_JUDGE, LLMError, complete_json
from jobhunt.resume.profile import Profile
from jobhunt.store.db import session
from jobhunt.store.models import Eligibility, Job, JobStatus

log = logging.getLogger(__name__)

# What the dashboard writes into status_reason when you reject a job yourself.
MANUAL_REJECT_PREFIX = "rejected by you"

BATCH_SIZE = 5
JD_CHARS = 3500  # requirements live near the top; the rest is boilerplate

DIMENSIONS = {
    "skill_overlap": 0.25,
    "level_realism": 0.25,
    "comp_confidence": 0.15,
    "eligibility": 0.15,
    "ai_relevance": 0.10,
    "company_quality": 0.10,
}


def build_system_prompt(profile: Profile) -> str:
    """Stable across a whole run, so it caches cleanly."""
    prefs = profile.preferences
    experience = "\n".join(
        f"- {e.title} at {e.company} ({e.start} - {e.end}), {e.location}\n"
        + "\n".join(f"    * {b}" for b in e.bullets)
        for e in profile.experience
    )
    projects = "\n".join(
        f"- {p.name} [{', '.join(p.tech)}]\n" + "\n".join(f"    * {b}" for b in p.bullets)
        for p in profile.projects
    )
    education = "\n".join(
        f"- {e.degree}, {e.institution} ({e.start}-{e.end}) {e.grade}" for e in profile.education
    )
    cfg = targets().get("compensation", {})

    return f"""You score software engineering job postings for one specific candidate.
Be sceptical and concrete. Your job is to protect the candidate's time, not to
be encouraging.

# CANDIDATE

{profile.identity.name}, {profile.years_text()} years of professional experience.
Based in {profile.identity.location or "India"}.
Work authorisation: {prefs.work_authorization or profile.answers.work_auth_statement or "not stated"}
Current CTC: {f"{prefs.current_ctc_lpa:g} LPA" if prefs.current_ctc_lpa else "not stated"}. Will not move for less than
{prefs.expected_min_ctc_lpa or cfg.get("india_min_ctc_lpa", 45)} LPA (India) or
${cfg.get("us_remote_min_usd", 120000):,} (US/remote).

## Education
{education}

## Experience
{experience}

## Projects
{projects}

## Skills
{", ".join(profile.all_skills())}

# SCORING

Score each posting 1-5 on each dimension. 3 is "acceptable", 5 is "excellent",
1 is "disqualifying". Use the whole range; do not cluster everything at 3-4.

- skill_overlap    How well the candidate's actual shipped work matches the
                   requirements. Reward concrete overlap, not keyword bingo.
- level_realism    Would this team realistically interview someone with
                   {profile.years_text()} years of experience? A title asking for
                   far more experience than that is a 1 even if the skills match.
- comp_confidence  Likelihood this clears the stated floor. Use any published
                   figure. If none, reason from company, location and level.
- eligibility      Can this candidate actually be hired? India-based or
                   worldwide-remote is 5. Explicit visa sponsorship is 4.
                   US-only with no sponsorship is 1, however good the fit.
- ai_relevance     Does it use the candidate's RAG / AI-agent / tool-calling
                   work? This is their strongest differentiator. A plain CRUD
                   role is a 2, not a 1 - it is simply not a differentiator.
- company_quality  Trajectory, engineering reputation, and what the role does
                   for the next move.

Also return `eligibility_verdict`, one of:
  india_eligible | sponsorship_offered | us_only_no_sponsorship | unknown
Read the posting properly before answering. Many US postings never mention
sponsorship; answer `unknown` rather than assuming either way.

`summary` must be one sentence naming the single biggest reason to apply or
not apply. No hedging, no restating the title.
"""


def build_batch_prompt(jobs: list[Job]) -> str:
    blocks = []
    for job in jobs:
        comp = "not published"
        if job.comp_min or job.comp_max:
            cur = job.comp_currency or "USD"
            comp = f"{cur} {job.comp_min or 0:,.0f} - {job.comp_max or 0:,.0f} (published)"
        elif job.comp_reason:
            comp = f"estimated: {job.comp_reason}"

        blocks.append(
            f"""<posting id="{job.id}">
company: {job.company}
title: {job.title}
location: {job.location or "not stated"} (remote={job.remote})
compensation: {comp}
gatekeeper_level_band: {job.level_band.value} ({job.level_reason})
gatekeeper_eligibility: {job.eligibility.value} ({job.eligibility_reason})

description:
{(job.description or "")[:JD_CHARS]}
</posting>"""
        )

    schema = """Return a JSON array with one object per posting:
[{"id": <int>, "scores": {"skill_overlap": <1-5>, "level_realism": <1-5>,
  "comp_confidence": <1-5>, "eligibility": <1-5>, "ai_relevance": <1-5>,
  "company_quality": <1-5>}, "eligibility_verdict": "<verdict>",
  "summary": "<one sentence>"}]"""

    return f"Score these {len(jobs)} postings.\n\n" + "\n\n".join(blocks) + f"\n\n{schema}"


def weighted_score(scores: dict[str, float]) -> float:
    """Weighted mean, with two hard overrides.

    A dimension can be disqualifying on its own: a role the candidate cannot
    legally take, or cannot plausibly be interviewed for, is not a 3.5
    opportunity no matter how good the rest looks.
    """
    total = sum(DIMENSIONS[d] * float(scores.get(d, 3)) for d in DIMENSIONS)
    if float(scores.get("eligibility", 3)) <= 1:
        return min(total, 1.5)
    if float(scores.get("level_realism", 3)) <= 1:
        return min(total, 2.5)
    return round(total, 2)


@dataclass
class ScoreStats:
    scored: int = 0
    failed: int = 0
    llm_calls: int = 0
    above_threshold: int = 0
    archived: int = 0
    reclassified: int = 0
    distribution: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "scored": self.scored,
            "failed": self.failed,
            "llm_calls": self.llm_calls,
            "above_threshold": self.above_threshold,
            "archived": self.archived,
            "reclassified": self.reclassified,
            "distribution": self.distribution,
        }


def _apply(job: Job, result: dict, stats: ScoreStats, threshold: float) -> None:
    scores = result.get("scores") or {}
    score = weighted_score(scores)

    job.score = score
    job.score_detail = {
        "dimensions": scores,
        "weighted": score,
        "model_summary": result.get("summary", ""),
    }
    job.score_summary = result.get("summary", "")

    verdict_raw = (result.get("eligibility_verdict") or "").strip()
    try:
        verdict = Eligibility(verdict_raw) if verdict_raw else None
    except ValueError:
        log.debug("job %s: unrecognised eligibility verdict %r", job.id, verdict_raw)
        verdict = None

    if verdict is not None and verdict is not job.eligibility:
        # The Analyst may resolve an UNKNOWN either way, and may *downgrade* a
        # positive verdict - it reads the whole JD, where the Gatekeeper only
        # sees the location string. A real case: a posting headed "Anywhere in
        # the World" whose body restricts hiring to five European countries.
        #
        # It may not upgrade an explicit refusal. That verdict comes from the
        # posting stating outright that it will not sponsor, which no amount
        # of inference should overturn.
        downgrade = verdict is Eligibility.US_ONLY_NO_SPONSORSHIP
        resolving_unknown = job.eligibility is Eligibility.UNKNOWN
        hard_refusal = job.eligibility is Eligibility.US_ONLY_NO_SPONSORSHIP

        if (downgrade or resolving_unknown) and not hard_refusal:
            job.eligibility = verdict
            job.eligibility_reason = f"Analyst read the full JD: {verdict.value}"
            stats.reclassified += 1

    if job.eligibility is Eligibility.US_ONLY_NO_SPONSORSHIP:
        job.status = JobStatus.REJECTED_GATE
        job.status_reason = "Analyst: US-only, no sponsorship"
        stats.archived += 1
        return

    bucket = f"{int(score)}.0-{int(score)}.9"
    stats.distribution[bucket] = stats.distribution.get(bucket, 0) + 1

    if score >= threshold:
        job.status = JobStatus.SCORED
        job.status_reason = f"scored {score}"
        stats.above_threshold += 1
    else:
        job.status = JobStatus.ARCHIVED
        job.status_reason = f"scored {score} (below {threshold})"
        stats.archived += 1


def pick_for_scoring(
    s: Any, *, limit: int | None = None, eligibility: list[Eligibility] | None = None
) -> list[int]:
    """Which gated jobs to score next, best bets first.

    Roles you can actually take come first (enum values sort india_eligible <
    sponsorship_offered < unknown), so a run cut short still produced usable
    results. Within that, companies you track by tier come first: with
    LinkedIn in the mix, "newest first" alone spent the day's scoring on
    whichever unknown agency posted last.
    """
    query = select(Job.id, Job.company, Job.eligibility, Job.posted_at).where(
        Job.status == JobStatus.GATED
    )
    if eligibility:
        query = query.where(Job.eligibility.in_(eligibility))
    rows = list(s.exec(query).all())
    tiers = {c["name"].lower(): c.get("tier", 3) for c in company_tiers().get("companies", [])}
    order = {e: i for i, e in enumerate(Eligibility)}
    epoch = datetime.min.replace(tzinfo=UTC)

    def posted(row) -> datetime:
        when = row[3]
        if when is None:
            return epoch
        return when if when.tzinfo else when.replace(tzinfo=UTC)

    rows.sort(key=posted, reverse=True)  # newest first within each group
    rows.sort(key=lambda r: (order.get(r[2], 9), tiers.get((r[1] or "").lower(), 9)))
    ids = [r[0] for r in rows]
    return ids[:limit] if limit else ids


def run(
    *,
    limit: int | None = None,
    batch_size: int = BATCH_SIZE,
    eligibility: list[Eligibility] | None = None,
) -> ScoreStats:
    cfg = targets().get("scoring", {})
    threshold = float(cfg.get("apply_threshold", 4.0))
    stats = ScoreStats()

    profile = Profile.load(settings().profile_path)
    system = build_system_prompt(profile)

    with session() as s:
        job_ids = pick_for_scoring(s, limit=limit, eligibility=eligibility)

    # One session and one transaction per batch. Scoring a few hundred jobs
    # takes many minutes; holding a single transaction across all of it means
    # a crash or a Ctrl-C throws away every result, and nothing is visible
    # until the very end.
    for i in range(0, len(job_ids), batch_size):
        chunk = job_ids[i : i + batch_size]
        with session() as s:
            batch = list(s.exec(select(Job).where(Job.id.in_(chunk))).all())
            if not batch:
                continue
            try:
                results = complete_json(
                    build_batch_prompt(batch),
                    system=system,
                    model=MODEL_JUDGE,
                    max_tokens=4000,
                )
                stats.llm_calls += 1
            except LLMError as exc:
                log.warning("batch %d failed: %s", i // batch_size, exc)
                stats.failed += len(batch)
                continue

            if isinstance(results, dict):
                results = results.get("results") or [results]
            by_id = {int(r["id"]): r for r in results if isinstance(r, dict) and "id" in r}

            for job in batch:
                result = by_id.get(job.id)
                if result is None:
                    stats.failed += 1
                    continue
                _apply(job, result, stats, threshold)
                stats.scored += 1

        log.info("scored %d/%d", min(i + batch_size, len(job_ids)), len(job_ids))

    return stats


def top(n: int = 20) -> list[Job]:
    with session() as s:
        return s.exec(
            select(Job)
            .where(Job.status == JobStatus.SCORED)
            .order_by(Job.score.desc())
            .limit(n)
        ).all()


def reroute() -> dict[str, int]:
    """Re-apply the threshold to jobs that already carry a score.

    Tuning ``apply_threshold`` must not mean paying for every LLM call again -
    the score is a property of the job, the threshold is only a routing
    decision over it.
    """
    threshold = float(targets().get("scoring", {}).get("apply_threshold", 3.75))
    moved = {"to_scored": 0, "to_archived": 0, "unchanged": 0}

    with session() as s:
        jobs = s.exec(
            select(Job).where(
                Job.score.is_not(None),
                Job.status.in_((JobStatus.SCORED, JobStatus.ARCHIVED)),
            )
        ).all()
        for job in jobs:
            # A job you rejected by hand stays rejected, whatever the bar does:
            # refiltering runs automatically when the settings change, and it
            # must never resurrect something you already said no to.
            if (job.status_reason or "").startswith(MANUAL_REJECT_PREFIX):
                moved["unchanged"] += 1
                continue
            wanted = JobStatus.SCORED if job.score >= threshold else JobStatus.ARCHIVED
            if job.status is wanted:
                moved["unchanged"] += 1
                continue
            job.status = wanted
            job.status_reason = (
                f"scored {job.score}"
                if wanted is JobStatus.SCORED
                else f"scored {job.score} (below {threshold})"
            )
            moved["to_scored" if wanted is JobStatus.SCORED else "to_archived"] += 1
    return moved
