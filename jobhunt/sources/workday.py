"""Workday connector.

Workday hosts the careers site for a large share of big technology employers -
NVIDIA, Salesforce, Adobe, Mastercard, PayPal, Red Hat among them - and exposes
the same JSON API its own careers page calls. No key, no scraping.

The one awkward part is that the list endpoint returns titles without
descriptions; the description needs a second request per job. NVIDIA alone
lists 2,000 roles, so descriptions are fetched **only** for postings whose
title already survives the level filter. That turns thousands of requests into
a few dozen.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from jobhunt.sources.base import Fetcher, RawJob, looks_remote, strip_html

log = logging.getLogger(__name__)

LIST_URL = "https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
DETAIL_URL = "https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"
PUBLIC_URL = "https://{tenant}.{host}.myworkdayjobs.com/en-US/{site}{path}"

PAGE_SIZE = 20
MAX_PAGES = 10          # per search term
MAX_DETAILS = 60        # per company, after the title prefilter

# Searched per company. Workday's searchText is a plain keyword match, so a
# handful of targeted terms beats pulling the whole board and filtering later.
DEFAULT_QUERIES = [
    "software engineer",
    "AI engineer",
    "machine learning engineer",
    "full stack engineer",
    "backend engineer",
]

# Cheap title prefilter, applied before spending a request on a description.
# Deliberately looser than the Gatekeeper - this only decides what is worth
# reading, and the real filtering happens downstream.
_TITLE_OK = re.compile(
    r"\b(engineer|developer|sde|swe|mts|member of technical staff|scientist)\b", re.I
)
_TITLE_NO = re.compile(
    r"\b(director|vp|principal|staff|architect|manager|intern|sales|account|"
    r"recruiter|marketing|finance|legal|counsel|hardware|asic|silicon|"
    r"mechanical|electrical|physical design|analog|verification)\b",
    re.I,
)

_POSTED_DAYS = re.compile(r"(\d+)\+?\s*days?\s*ago", re.I)


def _parse_posted(text: str | None) -> datetime | None:
    """Workday reports "Posted 5 Days Ago", not a date."""
    if not text:
        return None
    low = text.lower()
    now = datetime.now(UTC)
    if "today" in low:
        return now
    if "yesterday" in low:
        return now - timedelta(days=1)
    if m := _POSTED_DAYS.search(low):
        return now - timedelta(days=int(m.group(1)))
    return None


def _title_worth_reading(title: str) -> bool:
    return bool(_TITLE_OK.search(title)) and not _TITLE_NO.search(title)


def fetch_workday(
    fetcher: Fetcher,
    tenant: str,
    site: str,
    company: str,
    *,
    host: str = "wd5",
    queries: list[str] | None = None,
    max_details: int = MAX_DETAILS,
) -> list[RawJob]:
    list_url = LIST_URL.format(tenant=tenant, host=host, site=site)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    # Collect candidates first, deduplicating by path across search terms.
    candidates: dict[str, dict] = {}
    for query in queries or DEFAULT_QUERIES:
        for page in range(MAX_PAGES):
            payload = {
                "appliedFacets": {},
                "limit": PAGE_SIZE,
                "offset": page * PAGE_SIZE,
                "searchText": query,
            }
            data = fetcher.post_json(list_url, json=payload, headers=headers)
            if not data or not data.get("jobPostings"):
                break
            for posting in data["jobPostings"]:
                path = posting.get("externalPath")
                if path and path not in candidates:
                    candidates[path] = posting
            if (page + 1) * PAGE_SIZE >= min(data.get("total", 0), MAX_PAGES * PAGE_SIZE):
                break

    relevant = [
        (path, p) for path, p in candidates.items() if _title_worth_reading(p.get("title", ""))
    ][:max_details]

    log.info(
        "workday %s: %d candidates, %d worth reading", company, len(candidates), len(relevant)
    )

    jobs: list[RawJob] = []
    for path, posting in relevant:
        detail = fetcher.json(
            DETAIL_URL.format(tenant=tenant, host=host, site=site, path=path),
            headers={"Accept": "application/json"},
        )
        info = (detail or {}).get("jobPostingInfo") or {}
        description = strip_html(info.get("jobDescription") or "")
        location = info.get("location") or posting.get("locationsText") or ""
        title = info.get("title") or posting.get("title") or ""
        req_id = info.get("jobReqId") or (posting.get("bulletFields") or [path])[0]

        jobs.append(
            RawJob(
                source="workday",
                source_id=f"{tenant}:{req_id}",
                title=title.strip(),
                company=company,
                url=info.get("externalUrl")
                or PUBLIC_URL.format(tenant=tenant, host=host, site=site, path=path),
                location=location,
                description=description,
                remote=looks_remote(location, description[:400]),
                posted_at=_parse_posted(info.get("postedOn") or posting.get("postedOn")),
                ats="workday",
                ats_slug=tenant,
                extra={
                    "req_id": req_id,
                    "time_type": info.get("timeType"),
                    "country": (info.get("country") or {}).get("descriptor"),
                },
            )
        )
    return jobs
