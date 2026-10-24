"""Amazon / AWS connector.

``amazon.jobs`` exposes the same search JSON its own site calls, and unlike
Workday it returns the full description, basic qualifications and preferred
qualifications in the list response - so one request covers a whole page.

Amazon is worth a dedicated connector rather than a config line: it is the
largest single SDE employer in Bengaluru, and its "SDE I / SDE II" ladder maps
directly onto the level bands this search targets.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from jobhunt.sources.base import Fetcher, RawJob, looks_remote, strip_html

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.amazon.jobs/en/search.json"
JOB_URL = "https://www.amazon.jobs{path}"

PAGE_SIZE = 100
MAX_PAGES = 5

# Amazon's own category taxonomy; searching these beats free-text guessing.
DEFAULT_QUERIES = [
    "software development engineer",
    "front end engineer",
    "applied scientist",
    "machine learning engineer",
]

# ISO-3 country codes. `country` is the parameter that actually filters:
# `loc_query=India` is a free-text hint that still returned 36 US roles out of
# 50, while `country=IND` returned 50 of 50.
DEFAULT_COUNTRIES = ["IND"]

_DATE = re.compile(r"^([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})$")
_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    )
}


def _parse_date(text: str | None) -> datetime | None:
    """Amazon formats dates as "September 12, 2026"."""
    if not text:
        return None
    if m := _DATE.match(text.strip()):
        month, day, year = m.groups()
        if month in _MONTHS:
            try:
                return datetime(int(year), _MONTHS[month], int(day), tzinfo=UTC)
            except ValueError:
                return None
    return None


def _compose_description(job: dict) -> str:
    """Amazon splits the posting across three fields.

    The qualifications carry the years-of-experience bar the level filter
    depends on, so dropping them would make every Amazon role look unbounded.
    """
    parts = [
        job.get("description") or "",
        ("BASIC QUALIFICATIONS\n" + job["basic_qualifications"])
        if job.get("basic_qualifications")
        else "",
        ("PREFERRED QUALIFICATIONS\n" + job["preferred_qualifications"])
        if job.get("preferred_qualifications")
        else "",
    ]
    return strip_html("\n\n".join(p for p in parts if p))


def fetch_amazon(
    fetcher: Fetcher,
    *,
    queries: list[str] | None = None,
    countries: list[str] | None = None,
) -> list[RawJob]:
    seen: dict[str, RawJob] = {}

    for query in queries or DEFAULT_QUERIES:
        for country in countries if countries is not None else DEFAULT_COUNTRIES:
            for page in range(MAX_PAGES):
                params = {
                    "base_query": query,
                    "result_limit": PAGE_SIZE,
                    "offset": page * PAGE_SIZE,
                    "sort": "recent",
                }
                if country:
                    params["country"] = country

                data = fetcher.json(SEARCH_URL, params=params)
                if not data or not data.get("jobs"):
                    break

                for job in data["jobs"]:
                    job_id = str(job.get("id_icims") or job.get("id") or "")
                    if not job_id or job_id in seen:
                        continue
                    # Amazon flags interns and managers explicitly; the level
                    # filter would reject them anyway, so skip the work.
                    if str(job.get("is_intern")).lower() == "true":
                        continue

                    description = _compose_description(job)
                    where = job.get("normalized_location") or job.get("location") or ""
                    seen[job_id] = RawJob(
                        source="amazon",
                        source_id=job_id,
                        title=(job.get("title") or "").strip(),
                        company="Amazon",
                        url=JOB_URL.format(path=job.get("job_path") or ""),
                        location=where,
                        description=description,
                        remote=looks_remote(where, description[:400]),
                        posted_at=_parse_date(job.get("posted_date")),
                        ats="amazon",
                        ats_slug="amazon",
                        extra={
                            "team": job.get("team"),
                            "business_category": job.get("business_category"),
                            "job_family": job.get("job_family"),
                            "company_entity": job.get("company_name"),
                        },
                    )

                if len(data["jobs"]) < PAGE_SIZE:
                    break

    log.info("amazon: %d jobs", len(seen))
    return list(seen.values())
