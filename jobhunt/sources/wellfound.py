"""Startup jobs from Wellfound (formerly AngelList Talent).

Indian startups list here, often with the salary range - which is exactly
what company careers pages leave out. Each public role/location page embeds
its listings as JSON in the page (``__NEXT_DATA__``), so no login is involved
and one request yields a page of jobs with their full descriptions.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from jobhunt.config import targets
from jobhunt.sources.base import Fetcher, RawJob

log = logging.getLogger(__name__)

PAGE_URL = "https://wellfound.com/role/l/{role}/{location}"
JOB_URL = "https://wellfound.com/jobs/{id}-{slug}"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "location": "india",
    "roles": [
        "software-engineer",
        "backend-engineer",
        "full-stack-engineer",
        "machine-learning-engineer",
        "artificial-intelligence-engineer",
    ],
    "pages_per_role": 3,
}

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
# "₹15L – ₹25L", "₹8,00,000 – ₹12,00,000", "$120k – $160k"
_LAKH = re.compile(r"₹\s*([\d.]+)\s*L", re.I)
_RUPEES = re.compile(r"₹\s*([\d,]+)")
_DOLLARS = re.compile(r"\$\s*([\d.]+)\s*(k)?", re.I)


def settings() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update((targets().get("sources", {}) or {}).get("wellfound", {}) or {})
    return cfg


def parse_compensation(text: str) -> tuple[float | None, float | None, str | None]:
    """(min, max, currency) from Wellfound's compensation string."""
    if not text:
        return None, None, None
    if lakhs := _LAKH.findall(text):
        values = [float(v) * 100_000 for v in lakhs]
        return min(values), max(values), "INR"
    if rupees := _RUPEES.findall(text):
        values = [float(v.replace(",", "")) for v in rupees if v.replace(",", "")]
        values = [v for v in values if v > 0]
        if values:
            return min(values), max(values), "INR"
    if dollars := _DOLLARS.findall(text):
        values = [float(v) * (1000 if k else 1) for v, k in dollars]
        return min(values), max(values), "USD"
    return None, None, None


def parse_page(html: str) -> list[RawJob]:
    """Every listing on one Wellfound role/location page."""
    m = _NEXT_DATA.search(html)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
        apollo = data["props"]["pageProps"]["apolloState"]["data"]
    except (ValueError, KeyError, TypeError):
        return []

    company_of: dict[str, str] = {}
    for node in apollo.values():
        if node.get("__typename") == "StartupResult":
            for ref in node.get("highlightedJobListings") or []:
                if isinstance(ref, dict) and ref.get("__ref"):
                    company_of[ref["__ref"]] = node.get("name") or ""

    jobs = []
    for key, node in apollo.items():
        if node.get("__typename") != "JobListingSearchResult":
            continue
        if (node.get("jobType") or "full-time") not in ("full-time", "contract-to-hire"):
            continue
        low, high, currency = parse_compensation(node.get("compensation") or "")
        posted = node.get("liveStartAt")
        locations = node.get("locationNames") or []
        remote_ok = node.get("acceptedRemoteLocationNames") or []
        location = ", ".join(locations) or ("Remote - " + ", ".join(remote_ok) if remote_ok else "")
        years = node.get("yearsExperienceMin")
        description = node.get("description") or ""
        if years is not None:
            description = f"Experience: {years}+ years.\n\n{description}"
        jobs.append(
            RawJob(
                source="wellfound",
                source_id=str(node.get("id")),
                title=(node.get("title") or "").strip(),
                company=company_of.get(key, ""),
                url=JOB_URL.format(id=node.get("id"), slug=node.get("slug") or "job"),
                location=location,
                description=description,
                remote=bool(node.get("remote")),
                posted_at=datetime.fromtimestamp(posted, tz=UTC) if posted else None,
                ats="wellfound",
                comp_min=low,
                comp_max=high,
                comp_currency=currency,
                extra={"compensation": node.get("compensation") or ""},
            )
        )
    return [j for j in jobs if j.company and j.title]


def fetch_wellfound(fetcher: Fetcher) -> list[RawJob]:
    cfg = settings()
    if not cfg.get("enabled", True):
        return []
    out: dict[str, RawJob] = {}
    for role in cfg.get("roles") or []:
        for page in range(1, int(cfg.get("pages_per_role") or 1) + 1):
            url = PAGE_URL.format(role=role, location=cfg.get("location") or "india")
            resp = fetcher.get(url, params={"page": page} if page > 1 else None, retries=1)
            if resp is None:
                break
            found = parse_page(resp.text)
            for job in found:
                out.setdefault(job.source_id, job)
            if not found:
                break
    log.info("wellfound: %d listings", len(out))
    return list(out.values())
