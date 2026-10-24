"""Public ATS job-board connectors: Greenhouse, Ashby, Lever.

These three expose unauthenticated JSON endpoints - the same ones the company's
own careers page calls - so they are the highest-signal, lowest-risk source in
the system. No key, no scraping, no ToS grey area.

Ashby is the most valuable of the three because it returns *structured
compensation*: on OpenAI's board, 665 of 795 postings carry a real salary band.
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, datetime

from jobhunt.config import company_tiers
from jobhunt.sources.base import Fetcher, RawJob, looks_remote, strip_html

log = logging.getLogger(__name__)

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
LEVER_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Greenhouse
# --------------------------------------------------------------------------


def fetch_greenhouse(fetcher: Fetcher, slug: str, company: str) -> list[RawJob]:
    payload = fetcher.json(GREENHOUSE_URL.format(slug=slug))
    if not payload or "jobs" not in payload:
        return []

    jobs: list[RawJob] = []
    for j in payload["jobs"]:
        # Greenhouse double-escapes its content field ("&lt;p&gt;"), so it must
        # be unescaped before the HTML stripper can see any tags at all.
        description = strip_html(html.unescape(j.get("content") or ""))
        location = (j.get("location") or {}).get("name", "") or ""
        jobs.append(
            RawJob(
                source="greenhouse",
                source_id=str(j.get("id")),
                title=(j.get("title") or "").strip(),
                company=company,
                url=j.get("absolute_url") or "",
                location=location,
                description=description,
                remote=looks_remote(location),
                posted_at=_parse_iso(j.get("first_published") or j.get("updated_at")),
                ats="greenhouse",
                ats_slug=slug,
                extra={"departments": [d.get("name") for d in j.get("departments") or []]},
            )
        )
    return jobs


# --------------------------------------------------------------------------
# Ashby
# --------------------------------------------------------------------------


def _ashby_compensation(job: dict) -> tuple[float | None, float | None, str | None]:
    """Pull the annual salary band out of Ashby's compensation tiers.

    Ashby reports several component types per tier (Salary, EquityPercentage,
    EquityCashValue, Bonus). Only the Salary component is comparable against a
    base-pay floor, so the others are deliberately ignored rather than summed.
    """
    comp = job.get("compensation") or {}
    for tier in comp.get("compensationTiers") or []:
        for component in tier.get("components") or []:
            if component.get("compensationType") != "Salary":
                continue
            if component.get("interval") not in ("1 YEAR", "YEAR", None):
                continue
            lo, hi = component.get("minValue"), component.get("maxValue")
            if lo or hi:
                return lo, hi, component.get("currencyCode")
    return None, None, None


def fetch_ashby(fetcher: Fetcher, slug: str, company: str) -> list[RawJob]:
    payload = fetcher.json(ASHBY_URL.format(slug=slug))
    if not payload or "jobs" not in payload:
        return []

    jobs: list[RawJob] = []
    for j in payload["jobs"]:
        if j.get("isListed") is False:
            continue
        description = strip_html(j.get("descriptionHtml") or "")
        locations = [j.get("location") or ""]
        locations += [
            (s.get("location") or "") for s in (j.get("secondaryLocations") or [])
        ]
        location = ", ".join(x for x in dict.fromkeys(locations) if x)
        lo, hi, currency = _ashby_compensation(j)

        jobs.append(
            RawJob(
                source="ashby",
                source_id=str(j.get("id")),
                title=(j.get("title") or "").strip(),
                company=company,
                url=j.get("jobUrl") or j.get("applyUrl") or "",
                location=location,
                description=description,
                # Ashby states this explicitly, so trust it. OR-ing in a text
                # heuristic marked San Francisco roles remote purely because
                # their description mentioned remote work somewhere.
                remote=bool(j.get("isRemote")) or j.get("workplaceType") == "Remote",
                posted_at=_parse_iso(j.get("publishedAt")),
                ats="ashby",
                ats_slug=slug,
                comp_min=lo,
                comp_max=hi,
                comp_currency=currency,
                extra={
                    "department": j.get("department"),
                    "team": j.get("team"),
                    "employment_type": j.get("employmentType"),
                    "workplace_type": j.get("workplaceType"),
                    "apply_url": j.get("applyUrl"),
                },
            )
        )
    return jobs


# --------------------------------------------------------------------------
# Lever
# --------------------------------------------------------------------------


def fetch_lever(fetcher: Fetcher, slug: str, company: str) -> list[RawJob]:
    payload = fetcher.json(LEVER_URL.format(slug=slug))
    if not isinstance(payload, list):
        return []

    jobs: list[RawJob] = []
    for j in payload:
        categories = j.get("categories") or {}
        location = categories.get("location") or ""
        if extra_locs := categories.get("allLocations"):
            location = ", ".join(dict.fromkeys([location, *extra_locs]))
        description = j.get("descriptionPlain") or strip_html(j.get("description") or "")
        posted = None
        if created := j.get("createdAt"):
            try:
                posted = datetime.fromtimestamp(created / 1000, tz=UTC)
            except (TypeError, ValueError, OSError):
                posted = None

        jobs.append(
            RawJob(
                source="lever",
                source_id=str(j.get("id")),
                # Lever titles are frequently lowercase ("area collections
                # manager bangalore"); title-casing keeps them readable and
                # makes level regexes behave predictably.
                title=(j.get("text") or "").strip(),
                company=company,
                url=j.get("hostedUrl") or j.get("applyUrl") or "",
                location=location,
                description=description,
                # Lever also states workplace type; same reasoning as Ashby.
                remote=(j.get("workplaceType") or "").lower() == "remote"
                or looks_remote(location),
                posted_at=posted,
                ats="lever",
                ats_slug=slug,
                extra={
                    "commitment": categories.get("commitment"),
                    "department": categories.get("department"),
                    "team": categories.get("team"),
                    "country": j.get("country"),
                    "apply_url": j.get("applyUrl"),
                },
            )
        )
    return jobs


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def _fetch_workday(fetcher: Fetcher, slug: str, company: str, entry: dict) -> list[RawJob]:
    from jobhunt.sources.workday import fetch_workday

    return fetch_workday(
        fetcher, slug, entry["wd_site"], company, host=entry.get("wd_host", "wd5")
    )


def _fetch_amazon(fetcher: Fetcher, _slug: str, _company: str, _entry: dict) -> list[RawJob]:
    from jobhunt.sources.amazon import fetch_amazon

    return fetch_amazon(fetcher)


def _fetch_smartrecruiters(fetcher: Fetcher, slug: str, company: str, _e: dict) -> list[RawJob]:
    from jobhunt.sources.smartrecruiters import fetch_smartrecruiters

    return fetch_smartrecruiters(fetcher, slug, company)


# Greenhouse/Ashby/Lever share a signature; Workday needs its site and host,
# and Amazon takes neither, so those two are adapted rather than special-cased
# at the call site.
FETCHERS = {
    "greenhouse": lambda f, slug, company, _e: fetch_greenhouse(f, slug, company),
    "ashby": lambda f, slug, company, _e: fetch_ashby(f, slug, company),
    "lever": lambda f, slug, company, _e: fetch_lever(f, slug, company),
    "workday": _fetch_workday,
    "amazon": _fetch_amazon,
    "smartrecruiters": _fetch_smartrecruiters,
}


def fetch_all_boards(fetcher: Fetcher, companies: list[dict] | None = None) -> list[RawJob]:
    """Pull every configured company board.

    One dead board never aborts the run - a company silently migrating ATS is
    routine, and :func:`discover_board` exists to re-home it.
    """
    companies = companies if companies is not None else company_tiers().get("companies", [])
    out: list[RawJob] = []
    for entry in companies:
        fn = FETCHERS.get(entry.get("ats", ""))
        if fn is None:
            continue
        try:
            jobs = fn(fetcher, entry["slug"], entry["name"], entry)
        except Exception as exc:  # noqa: BLE001 - a bad board must not kill the run
            log.warning("%s (%s): %s", entry["name"], entry.get("ats"), exc)
            continue
        if not jobs:
            log.info("%s (%s/%s): no jobs - board may have moved",
                     entry["name"], entry.get("ats"), entry.get("slug"))
        out.extend(jobs)
    return out


def discover_board(fetcher: Fetcher, slug: str) -> str | None:
    """Probe all three ATS for a slug and return whichever answers."""
    for ats, url in (
        ("greenhouse", GREENHOUSE_URL.format(slug=slug)),
        ("ashby", ASHBY_URL.format(slug=slug)),
        ("lever", LEVER_URL.format(slug=slug)),
    ):
        if fetcher.get(url) is not None:
            return ats
    return None
