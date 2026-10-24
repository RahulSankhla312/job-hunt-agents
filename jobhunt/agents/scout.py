"""Scout - pull every source, deduplicate, persist.

Dedupe runs in two passes because the two kinds of duplicate are different
problems:

* **exact** - the same role surfaced by several sources, caught by the
  canonical hash (company + normalised title + normalised location);
* **near** - the same role posted twice by one company with slightly different
  wording ("Software Engineer, Backend" vs "Backend Software Engineer"), caught
  by fuzzy title matching *within a company* only.

When duplicates collapse, the surviving record is the most useful one, not the
first one seen - an Ashby row carrying a real salary band beats an aggregator
row that lost it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from rapidfuzz import fuzz
from sqlmodel import select

from jobhunt.sources.ats import fetch_all_boards
from jobhunt.sources.base import (
    Fetcher,
    RawJob,
    _canonical_title,
    _slug,
    distinguishing_tokens,
    title_level,
)
from jobhunt.sources.feeds import fetch_all_feeds
from jobhunt.store.db import session
from jobhunt.store.models import Job, JobStatus

log = logging.getLogger(__name__)

# Sources that apply directly to the employer's own ATS are worth more than
# aggregators: fresher, complete descriptions, and a usable apply URL.
SOURCE_RANK = {
    "ashby": 100,          # structured compensation
    "greenhouse": 90,
    "lever": 85,
    "weworkremotely": 60,
    "remotive": 55,
    "remoteok": 50,
    "arbeitnow": 45,
    "hn_hiring": 30,       # free-form text, no structure
}

NEAR_DUPLICATE_THRESHOLD = 95  # rapidfuzz token_sort_ratio, one of three gates


@dataclass
class FunnelStats:
    """Per-stage counts. The whole point of tuning is being able to see these."""

    raw: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    exact_duplicates: int = 0
    near_duplicates: int = 0
    already_known: int = 0
    new: int = 0

    def as_dict(self) -> dict:
        return {
            "raw": self.raw,
            "by_source": self.by_source,
            "exact_duplicates": self.exact_duplicates,
            "near_duplicates": self.near_duplicates,
            "already_known": self.already_known,
            "new": self.new,
        }


def _quality(job: RawJob) -> tuple[int, int, int]:
    """Sort key deciding which copy of a duplicate survives."""
    return (
        SOURCE_RANK.get(job.source, 0),
        1 if job.comp_min else 0,
        len(job.description),
    )


def collapse_exact(jobs: list[RawJob]) -> tuple[list[RawJob], int]:
    best: dict[str, RawJob] = {}
    for job in jobs:
        key = job.dedupe_hash()
        incumbent = best.get(key)
        if incumbent is None or _quality(job) > _quality(incumbent):
            best[key] = job
    return list(best.values()), len(jobs) - len(best)


def _is_near_duplicate(a: RawJob, b: RawJob) -> bool:
    """Two postings at the same company that are really the same role.

    Deliberately conservative. The costs are asymmetric: a false merge
    silently destroys a job opportunity, which is the one thing this system
    exists to prevent, while a false split costs one extra scoring call inside
    a batch. So all three conditions must hold:

    1. identical seniority marker - "Engineer II" and "Engineer III" are
       different jobs, and fuzzy matching rates them 95;
    2. identical set of distinguishing words - this is what separates
       "Software Engineer, Data Infrastructure" from "... Agent
       Infrastructure" (fuzzy: 93) and "AI Safety" from "API Safety" (98);
    3. a high overall fuzzy score, to catch the remaining word-order variants
       ("Software Engineer, Backend" vs "Backend Software Engineer").
    """
    if title_level(a.title) != title_level(b.title):
        return False
    if distinguishing_tokens(a.title) != distinguishing_tokens(b.title):
        return False
    return (
        fuzz.token_sort_ratio(_canonical_title(a.title), _canonical_title(b.title))
        >= NEAR_DUPLICATE_THRESHOLD
    )


def collapse_near(jobs: list[RawJob]) -> tuple[list[RawJob], int]:
    """Collapse re-posts of one role *within a company*.

    Scoped to one company deliberately: "Software Engineer" at Stripe and at
    Rubrik are title-identical and must never collapse into one row.
    """
    by_company: dict[str, list[RawJob]] = {}
    for job in jobs:
        by_company.setdefault(_slug(job.company), []).append(job)

    survivors: list[RawJob] = []
    dropped = 0
    for group in by_company.values():
        if len(group) == 1:
            survivors.extend(group)
            continue
        group.sort(key=_quality, reverse=True)
        kept: list[RawJob] = []
        for job in group:
            if any(_is_near_duplicate(job, existing) for existing in kept):
                dropped += 1
            else:
                kept.append(job)
        survivors.extend(kept)
    return survivors, dropped


def persist(jobs: list[RawJob], stats: FunnelStats, *, dry_run: bool = False) -> None:
    with session() as s:
        known_pairs = {
            (src, sid) for src, sid in s.exec(select(Job.source, Job.source_id)).all()
        }
        known_hashes = set(s.exec(select(Job.dedupe_hash)).all())

        for job in jobs:
            if (job.source, job.source_id) in known_pairs:
                stats.already_known += 1
                continue
            digest = job.dedupe_hash()
            if digest in known_hashes:
                stats.already_known += 1
                continue

            stats.new += 1
            known_hashes.add(digest)
            if dry_run:
                continue

            s.add(
                Job(
                    source=job.source,
                    source_id=job.source_id,
                    url=job.url,
                    ats=job.ats,
                    ats_slug=job.ats_slug,
                    company=job.company,
                    title=job.title,
                    location=job.location,
                    remote=job.remote,
                    description=job.description,
                    posted_at=job.posted_at,
                    dedupe_hash=digest,
                    comp_min=job.comp_min,
                    comp_max=job.comp_max,
                    comp_currency=job.comp_currency,
                    comp_is_estimated=False,
                    comp_reason="published by source" if job.comp_min else None,
                    status=JobStatus.NEW,
                )
            )
        if dry_run:
            s.rollback()


def run(*, dry_run: bool = False, boards: bool = True, feeds: bool = True) -> FunnelStats:
    stats = FunnelStats()
    collected: list[RawJob] = []

    with Fetcher() as fetcher:
        if boards:
            log.info("pulling ATS boards …")
            collected += fetch_all_boards(fetcher)
        if feeds:
            log.info("pulling free feeds …")
            collected += fetch_all_feeds(fetcher)

    stats.raw = len(collected)
    for job in collected:
        stats.by_source[job.source] = stats.by_source.get(job.source, 0) + 1

    collected, stats.exact_duplicates = collapse_exact(collected)
    collected, stats.near_duplicates = collapse_near(collected)
    persist(collected, stats, dry_run=dry_run)
    return stats
