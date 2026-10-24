"""Board discovery.

Given a company name, work out which ATS hosts its careers page and what slug
it uses. Hand-probing does not scale past a handful of companies, and slugs are
rarely the obvious spelling - "Scale AI" is ``scaleai``, "Remote.com" is
``remotecom``, "Red Hat" is ``redhat``.

Workday is the awkward one: it needs both a tenant and a site name, and the
site name is arbitrary per customer (``External_Career_Site``, ``jobs``,
``NVIDIAExternalCareerSite``). Both are tried against a list of observed
conventions across several hosts.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from jobhunt.sources.ats import ASHBY_URL, GREENHOUSE_URL, LEVER_URL
from jobhunt.sources.base import Fetcher

log = logging.getLogger(__name__)

WORKDAY_URL = "https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
SMARTRECRUITERS_URL = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
# Ordered by observed frequency. The full cross-product is 5 hosts x 13 sites
# per slug, which at ~120 companies is tens of thousands of requests; this
# trimmed set covers the tenants actually seen in practice.
WORKDAY_HOSTS = ["wd1", "wd5", "wd3", "wd12"]
# Site names are matched case-insensitively by Workday, so only the shape
# matters. "external_experienced" is Adobe's and was missed by the first pass.
WORKDAY_SITES = [
    "External", "Careers", "External_Career_Site", "jobs", "Jobs",
    "ExternalCareerSite", "{Tenant}ExternalCareerSite", "external_experienced",
    "CorporateCareers", "{Tenant}", "External_Careers", "careers_site",
]

# Noise in company names that never appears in a board slug.
_SUFFIX = re.compile(
    r"\b(inc|llc|ltd|limited|corp|corporation|technologies|technology|labs|"
    r"software|systems|solutions|india|group|global|pvt|private|co)\b\.?",
    re.IGNORECASE,
)


@dataclass
class Board:
    company: str
    ats: str
    slug: str
    total: int = 0
    wd_site: str | None = None
    wd_host: str | None = None

    def as_config(self, tier: int = 2) -> dict:
        entry = {"name": self.company, "tier": tier, "ats": self.ats, "slug": self.slug}
        if self.ats == "workday":
            entry["wd_site"] = self.wd_site
            entry["wd_host"] = self.wd_host
        return entry


def slug_candidates(name: str) -> list[str]:
    """Plausible board slugs for a company name, most likely first."""
    cleaned = _SUFFIX.sub(" ", name)
    cleaned = re.sub(r"[^A-Za-z0-9 ]+", " ", cleaned).strip()
    words = [w for w in cleaned.split() if w]
    if not words:
        return []

    joined = "".join(words).lower()
    hyphen = "-".join(w.lower() for w in words)
    first = words[0].lower()

    out = [joined, hyphen, first]
    if len(words) > 1:
        # "Scale AI" also appears as scaleai (covered) and scale-ai (covered);
        # some boards drop the qualifier entirely.
        out.append("".join(w.lower() for w in words[:-1]))
    # Deduplicate, keep order, drop anything too short to be a real slug.
    seen: list[str] = []
    for s in out:
        if len(s) >= 3 and s not in seen:
            seen.append(s)
    return seen


_TEST_BOARD = re.compile(r"\btest\b|\bdemo\b|\bsandbox\b|\bexample\b", re.IGNORECASE)


def board_belongs_to(company: str, slug: str, evidence: str) -> bool:
    """Does this board actually belong to the company we searched for?

    A short slug derived from the first word of a name collides constantly:
    "Air Asia" found a board owned by a startup called "Air", "CASA RETAIL AI"
    found one owned by "Casa", and "LinkedIn" found *LI Test Company* - 53
    postings that would have entered the pipeline as real LinkedIn jobs.

    The check is deliberately lenient about wording but strict about test
    boards, because a plausible-looking wrong board is the expensive failure.
    """
    if _TEST_BOARD.search(evidence):
        return False

    words = [w.lower() for w in re.split(r"[^A-Za-z0-9]+", company) if len(w) > 2]
    significant = [w for w in words if w not in {"india", "private", "limited", "ltd", "inc"}]
    if not significant:
        return True

    haystack = evidence.lower()
    low = slug.lower()

    # The slug spelling the whole name is proof enough.
    if low in ("".join(significant), "-".join(significant)):
        return True

    # A slug that is only the *first* word of a multi-word name is the
    # dangerous case - "air" from "Air Asia", "casa" from "CASA RETAIL AI".
    # It always appears in its own board's URLs, so its presence proves
    # nothing; some other part of the name has to show up too.
    if len(significant) >= 2 and low == significant[0]:
        return any(w in haystack for w in significant[1:])

    return any(w in haystack for w in significant)


def _probe_simple(fetcher: Fetcher, slug: str, company: str) -> tuple[str, int] | None:
    for ats, url in (
        ("greenhouse", GREENHOUSE_URL.format(slug=slug)),
        ("ashby", ASHBY_URL.format(slug=slug)),
        ("lever", LEVER_URL.format(slug=slug)),
    ):
        data = fetcher.json(url)
        if data is None:
            continue

        if ats == "lever":
            jobs = data if isinstance(data, list) else []
            evidence = " ".join(
                str(j.get("hostedUrl", "")) + " " + str(j.get("text", "")) for j in jobs[:3]
            )
        else:
            jobs = data.get("jobs") if isinstance(data, dict) else None
            jobs = jobs or []
            evidence = " ".join(
                f"{j.get('company_name', '')} {j.get('absolute_url', '')} {j.get('jobUrl', '')}"
                for j in jobs[:3]
            )

        if not jobs:
            continue
        if not board_belongs_to(company, slug, f"{slug} {evidence}"):
            log.info("%s: %s/%s looks like a different company - skipping", company, ats, slug)
            continue
        return ats, len(jobs)

    # SmartRecruiters answers 200 for any slug, so only a non-empty board with
    # a matching company name counts.
    data = fetcher.json(SMARTRECRUITERS_URL.format(slug=slug), params={"limit": 3})
    if isinstance(data, dict) and int(data.get("totalFound") or 0) > 0:
        names = " ".join(
            str((p.get("company") or {}).get("name", "")) for p in data.get("content") or []
        )
        if board_belongs_to(company, slug, f"{slug} {names}"):
            return "smartrecruiters", int(data["totalFound"])
    return None


def _probe_workday(fetcher: Fetcher, slug: str) -> tuple[str, str, int] | None:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    payload = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
    for host in WORKDAY_HOSTS:
        for template in WORKDAY_SITES:
            site = template.format(tenant=slug, Tenant=slug.capitalize())
            data = fetcher.post_json(
                WORKDAY_URL.format(tenant=slug, host=host, site=site),
                json=payload,
                headers=headers,
            )
            if isinstance(data, dict) and data.get("total"):
                return host, site, int(data["total"])
    return None


def discover(
    fetcher: Fetcher, company: str, *, include_workday: bool = True
) -> Board | None:
    """Find the board for one company, or None."""
    for slug in slug_candidates(company):
        if hit := _probe_simple(fetcher, slug, company):
            ats, total = hit
            log.info("%s -> %s/%s (%d jobs)", company, ats, slug, total)
            return Board(company=company, ats=ats, slug=slug, total=total)

    if not include_workday:
        return None

    for slug in slug_candidates(company)[:2]:  # Workday probing is expensive
        if hit := _probe_workday(fetcher, slug):
            host, site, total = hit
            log.info("%s -> workday/%s/%s on %s (%d jobs)", company, slug, site, host, total)
            return Board(
                company=company, ats="workday", slug=slug, total=total,
                wd_site=site, wd_host=host,
            )
    return None
