"""SmartRecruiters career boards (Swiggy, Freshworks, Visa, ...).

A documented public API. The listing is cheap; descriptions cost one request
each, so only titles worth reading are fetched in full.

Darwinbox, which Zepto and many Indian employers use, is deliberately not
supported: its careers API sits behind Cloudflare's bot protection, and this
project does not work around bot checks. Those companies are covered by the
LinkedIn watch list instead.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from jobhunt.sources.base import Fetcher, RawJob, looks_remote, strip_html

log = logging.getLogger(__name__)

SR_LIST = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
SR_DETAIL = "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{id}"
SR_PUBLIC = "https://jobs.smartrecruiters.com/{slug}/{id}"
SR_MAX_DETAILS = 80


_TITLE_OK = re.compile(
    r"\b(engineer|engineering|developer|sde|swe|mts|member of technical staff|scientist)\b", re.I
)
_TITLE_NO = re.compile(
    r"\b(director|vp|principal|staff|architect|manager|head|intern|sales|recruiter|"
    r"marketing|finance|legal|hardware|mechanical|electrical|civil)\b",
    re.I,
)


def worth_reading(title: str) -> bool:
    return bool(_TITLE_OK.search(title)) and not _TITLE_NO.search(title)


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def fetch_smartrecruiters(fetcher: Fetcher, slug: str, company: str) -> list[RawJob]:
    postings: list[dict] = []
    offset = 0
    while True:
        page = fetcher.json(SR_LIST.format(slug=slug), params={"limit": 100, "offset": offset})
        if not isinstance(page, dict):
            break
        postings += page.get("content") or []
        offset += 100
        if offset >= int(page.get("totalFound") or 0):
            break

    jobs: list[RawJob] = []
    for p in postings:
        title = (p.get("name") or "").strip()
        if not worth_reading(title):
            continue
        # Global boards (ServiceNow, Grab, Arista) list hundreds of roles
        # abroad. Descriptions cost a request each, so only India and remote
        # roles are read - the listing already says where each one is.
        where = p.get("location") or {}
        if (where.get("country") or "").lower() != "in" and not where.get("remote"):
            continue
        if len(jobs) >= SR_MAX_DETAILS:
            log.info("%s (smartrecruiters): stopped at %d descriptions", company, SR_MAX_DETAILS)
            break
        detail = fetcher.json(SR_DETAIL.format(slug=slug, id=p.get("id"))) or {}
        sections = ((detail.get("jobAd") or {}).get("sections") or {})
        description = "\n\n".join(
            strip_html((sections.get(key) or {}).get("text") or "")
            for key in ("jobDescription", "qualifications", "additionalInformation")
        ).strip()
        loc = p.get("location") or {}
        country = "India" if (loc.get("country") or "").lower() == "in" else (loc.get("country") or "")
        city = (loc.get("city") or "").strip().title()
        location = ", ".join(x for x in (city, country.upper() if len(country) == 2 else country) if x)
        jobs.append(
            RawJob(
                source="smartrecruiters",
                source_id=str(p.get("id")),
                title=title,
                company=company,
                url=detail.get("postingUrl") or SR_PUBLIC.format(slug=slug, id=p.get("id")),
                location=location,
                description=description,
                remote=bool(loc.get("remote")) or looks_remote(location),
                posted_at=_iso(p.get("releasedDate")),
                ats="smartrecruiters",
                ats_slug=slug,
                extra={
                    "experience_level": (p.get("experienceLevel") or {}).get("label"),
                    "apply_url": detail.get("applyUrl"),
                },
            )
        )
    return jobs
