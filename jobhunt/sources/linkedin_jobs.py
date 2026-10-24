"""LinkedIn job posts, read from LinkedIn's public (logged-out) job search.

Plenty of Indian roles are posted on LinkedIn before - or instead of - a
company's own careers page. This reads the same listing anyone sees without
signing in:

* ``/jobs-guest/jobs/api/seeMoreJobPostings/search`` - result cards, 10 a page
* ``/jobs-guest/jobs/api/jobPosting/<id>``           - one posting's full text

It never uses your signed-in LinkedIn session. Automated use of a logged-in
account is what gets accounts restricted, and your account is worth more than
any listing - so this stays logged out, slow (one request every few seconds)
and capped per run. LinkedIn still limits logged-out traffic; when it says
"slow down" the run keeps what it has and stops asking.

Applying is not automated for these: the "apply on company site" link is only
shown to signed-in users, and LinkedIn's own Easy Apply may not be automated.
The job page link is kept so you can apply in a click.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from html import unescape
from typing import Any

from jobhunt.config import targets
from jobhunt.sources.base import Fetcher, RawJob, strip_html

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
POSTING_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
VIEW_URL = "https://www.linkedin.com/jobs/view/{id}"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "location": "India",
    "keywords": [
        "software engineer",
        "software development engineer",
        "SDE 1",
        "backend engineer",
        "full stack engineer",
        "AI engineer",
        "machine learning engineer",
        "GenAI engineer",
    ],
    # LinkedIn experience levels: 1 internship, 2 entry, 3 associate,
    # 4 mid-senior. Entry and associate cover SDE-1 and early SDE-2.
    "experience_levels": "2,3",
    "posted_within_days": 7,
    "pages_per_query": 4,
    # Company-targeted searches, for companies whose own careers site cannot
    # be read directly. Their posts are kept even when LinkedIn files them
    # under a different seniority.
    "watch_companies": [],
    "pages_per_company": 2,
    # Hard ceiling on posting pages read per run, after title prefiltering.
    "max_postings": 150,
}

_CARD = re.compile(r"<li[^>]*>(.*?)</li>", re.S)
_ID = re.compile(r'urn:li:jobPosting:(\d+)')
_TITLE = re.compile(r'<h3 class="base-search-card__title">\s*(.*?)\s*</h3>', re.S)
_COMPANY = re.compile(r'<h4 class="base-search-card__subtitle">(.*?)</h4>', re.S)
_LOCATION = re.compile(r'<span class="job-search-card__location">\s*(.*?)\s*</span>', re.S)
_POSTED = re.compile(r'<time[^>]*datetime="([0-9-]+)"', re.S)
_DESCRIPTION = re.compile(r'show-more-less-html__markup[^>]*>(.*?)</div>', re.S)
_CRITERIA = re.compile(
    r'<h3 class="description__job-criteria-subheader">\s*(.*?)\s*</h3>\s*'
    r'<span[^>]*>\s*(.*?)\s*</span>',
    re.S,
)

# Looser than the Gatekeeper: this only decides what is worth a request.
_TITLE_OK = re.compile(
    r"\b(engineer|developer|sde|swe|mts|member of technical staff|scientist|programmer)\b",
    re.I,
)
_TITLE_NO = re.compile(
    r"\b(director|vp|principal|staff|architect|manager|lead|head|intern|internship|"
    r"sales|recruiter|marketing|finance|legal|hardware|mechanical|electrical|"
    r"civil|support engineer|test engineer|qa)\b",
    re.I,
)


# Words that may follow a company's name in its LinkedIn listing without making
# it a different company: "Flipkart Internet Private Limited", "Uber India
# Systems Pvt Ltd". Anything else ("Zepto Fans Club Consulting") is another firm.
_CORPORATE = frozenset(
    ["pvt", "private", "ltd", "limited", "llp", "inc", "india", "technologies", "technology", "tech", "labs", "internet", "services", "systems", "software", "solutions", "group", "co", "company", "corp", "corporation", "global", "digital", "ventures", "holdings"]
)


def same_company(listed: str, watched: str) -> bool:
    """Is this LinkedIn listing really from the company we asked for?"""
    listed_words = re.findall(r"[a-z0-9]+", listed.lower())
    watched_words = re.findall(r"[a-z0-9]+", watched.lower())
    if not watched_words or listed_words[: len(watched_words)] != watched_words:
        return False
    return all(w in _CORPORATE for w in listed_words[len(watched_words):])


def settings() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update((targets().get("sources", {}) or {}).get("linkedin", {}) or {})
    return cfg


def _text(markup: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", markup or "")).strip()


def _squash(text: str) -> str:
    return " ".join(text.split())


def parse_cards(html: str) -> list[dict[str, Any]]:
    """Result cards from one search page."""
    out = []
    for card in _CARD.findall(html):
        m = _ID.search(card)
        title = _TITLE.search(card)
        if not m or not title:
            continue
        company = _COMPANY.search(card)
        location = _LOCATION.search(card)
        posted = _POSTED.search(card)
        out.append(
            {
                "id": m.group(1),
                "title": _squash(_text(title.group(1))),
                "company": _squash(_text(company.group(1))) if company else "",
                "location": _squash(_text(location.group(1))) if location else "",
                "posted": posted.group(1) if posted else None,
            }
        )
    return out


def parse_posting(html: str) -> dict[str, Any]:
    """Description and criteria ("Seniority level" etc.) from a posting page."""
    desc = _DESCRIPTION.search(html)
    criteria = {_squash(_text(k)): _squash(_text(v)) for k, v in _CRITERIA.findall(html)}
    return {
        "description": strip_html(desc.group(1)) if desc else "",
        "criteria": criteria,
    }


def worth_reading(title: str) -> bool:
    return bool(_TITLE_OK.search(title)) and not _TITLE_NO.search(title)


def _search(fetcher: Fetcher, params: dict[str, Any], pages: int) -> tuple[list[dict], bool]:
    """Cards for one query. Returns (cards, throttled)."""
    cards: list[dict] = []
    for page in range(pages):
        resp = fetcher.get(SEARCH_URL, params={**params, "start": page * 10}, retries=1)
        if resp is None:
            return cards, True
        found = parse_cards(resp.text)
        cards.extend(found)
        if len(found) < 10:
            break
    return cards, False


def fetch_linkedin(fetcher: Fetcher) -> list[RawJob]:
    cfg = settings()
    if not cfg.get("enabled", True):
        return []

    days = int(cfg.get("posted_within_days") or 7)
    base = {"location": cfg.get("location") or "India", "f_TPR": f"r{days * 86400}"}
    # Companies you asked to watch go first, so their posts are never the
    # ones cut by the per-run budget.
    queries: list[tuple[dict[str, Any], int, str | None]] = []
    for company in cfg.get("watch_companies") or []:
        params = {**base, "keywords": f"{company} software engineer"}
        queries.append((params, int(cfg.get("pages_per_company") or 1), company))
    for keyword in cfg.get("keywords") or []:
        params = {**base, "keywords": keyword}
        if cfg.get("experience_levels"):
            params["f_E"] = cfg["experience_levels"]
        queries.append((params, int(cfg.get("pages_per_query") or 1), None))

    seen: dict[str, dict] = {}
    for params, pages, company in queries:
        cards, throttled = _search(fetcher, params, pages)
        for card in cards:
            if company and not same_company(card["company"], company):
                continue  # a keyword hit, not a post by the company we asked for
            if worth_reading(card["title"]):
                seen.setdefault(card["id"], card)
        if throttled:
            log.warning("linkedin: throttled - keeping %d postings found so far", len(seen))
            break

    jobs: list[RawJob] = []
    budget = int(cfg.get("max_postings") or 150)
    reading = True
    for card in list(seen.values())[:budget]:
        detail = {"description": "", "criteria": {}}
        if reading:
            resp = fetcher.get(POSTING_URL.format(id=card["id"]), retries=1)
            if resp is None:
                # LinkedIn has started refusing us. Keep every card found - a
                # title, company and link are still a lead - and stop asking.
                log.warning("linkedin: stopped reading postings after %d", len(jobs))
                reading = False
            else:
                detail = parse_posting(resp.text)
        posted = None
        if card["posted"]:
            try:
                posted = datetime.fromisoformat(card["posted"]).replace(tzinfo=UTC)
            except ValueError:
                posted = None
        jobs.append(
            RawJob(
                source="linkedin",
                source_id=card["id"],
                title=card["title"],
                company=card["company"],
                url=VIEW_URL.format(id=card["id"]),
                location=card["location"],
                description=detail["description"],
                remote="remote" in card["location"].lower(),
                posted_at=posted,
                ats="linkedin",
                extra={"criteria": detail["criteria"]},
            )
        )
    log.info("linkedin: %d postings from %d queries", len(jobs), len(queries))
    return jobs
