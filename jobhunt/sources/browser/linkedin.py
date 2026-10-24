"""LinkedIn: find people at a company who could refer you.

Read-only. This navigates your own logged-in session the way you would, reads
what is on screen, and stops. It never sends a message, never clicks connect,
and never submits anything - the Connector agent drafts, you send.

That split is deliberate. LinkedIn's User Agreement prohibits automated
activity, and your account is the most valuable asset in a job search; a
restricted account costs far more than any referral is worth. Every hard limit
below comes from `defaults/targets.yaml` and is enforced against a daily counter,
not just hoped for.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote

from jobhunt.config import targets
from jobhunt.sources.browser.session import BrowserSession, SessionError
from jobhunt.store.db import consume_quota, remaining_quota

log = logging.getLogger(__name__)

PEOPLE_SEARCH = (
    "https://www.linkedin.com/search/results/people/"
    "?keywords={query}&origin=GLOBAL_SEARCH_HEADER"
)

# "1st", "2nd", "3rd+" as LinkedIn renders degree badges.
_DEGREE = re.compile(r"\b(1st|2nd|3rd)\b")
_PROFILE_HREF = re.compile(r"/in/([A-Za-z0-9\-_%]+)")

# Titles worth asking for a referral: engineers who would know the team, plus
# the recruiters who own the requisition.
REFERRAL_TITLES = (
    "software engineer", "sde", "engineer", "developer", "engineering manager",
    "tech lead", "staff engineer", "recruiter", "talent", "hiring",
)


@dataclass
class Person:
    name: str
    headline: str = ""
    profile_url: str = ""
    degree: int | None = None       # 1, 2 or 3
    company: str = ""
    shared_context: list[str] = field(default_factory=list)

    def referral_score(self) -> float:
        """How likely this person is to actually help.

        Degree dominates: a first-degree connection who already knows you is
        worth more than a better-titled stranger.
        """
        score = {1: 5.0, 2: 3.0, 3: 1.0}.get(self.degree or 3, 1.0)
        headline = self.headline.lower()
        if any(t in headline for t in ("recruiter", "talent", "hiring")):
            score += 1.5      # owns the requisition
        elif any(t in headline for t in ("engineer", "developer", "sde")):
            score += 1.0      # can submit an employee referral
        score += 0.75 * len(self.shared_context)
        return round(score, 2)


def shared_context(headline: str, profile_text: str, anchors: list[str]) -> list[str]:
    """Anchors you genuinely share with this person.

    A referral message that names a real shared thing - the same university,
    the same employer - lands; a generic one does not.
    """
    blob = f"{headline} {profile_text}".lower()
    return [a for a in anchors if a.lower() in blob]


def _parse_degree(text: str) -> int | None:
    if m := _DEGREE.search(text):
        return {"1st": 1, "2nd": 2, "3rd": 3}[m.group(1)]
    return None


def search_people(
    session: BrowserSession,
    company: str,
    *,
    anchors: list[str] | None = None,
    limit: int = 8,
) -> list[Person]:
    """Find plausible referrers at one company.

    Returns [] rather than raising when the quota is spent or the session has
    expired, so a sweep over many jobs degrades instead of aborting.
    """
    if remaining_quota("linkedin_search") <= 0:
        log.warning("linkedin search quota spent for today")
        return []
    if not consume_quota("linkedin_search"):
        return []

    query = quote(f"{company} software engineer")
    page = session.page()
    try:
        page.goto(PEOPLE_SEARCH.format(query=query), wait_until="domcontentloaded", timeout=45_000)
    except Exception as exc:  # noqa: BLE001
        raise SessionError(f"LinkedIn search failed: {exc}") from exc

    if "authwall" in page.url or "/login" in page.url:
        raise SessionError(
            "LinkedIn is not signed in - use Sign in to LinkedIn on the Connections page"
        )

    session.pause()
    try:
        page.wait_for_selector("div.search-results-container, main", timeout=20_000)
    except Exception:  # noqa: BLE001 - an empty result page is not an error
        log.info("no results container for %s", company)
        return []

    people: list[Person] = []
    seen: set[str] = set()
    view_budget = remaining_quota("linkedin_view")

    for card in page.query_selector_all("li, div.reusable-search__result-container")[:60]:
        if len(people) >= limit or view_budget <= 0:
            break
        try:
            text = (card.inner_text() or "").strip()
        except Exception:  # noqa: BLE001 - stale node during scroll
            continue
        if len(text) < 20:
            continue

        link = card.query_selector("a[href*='/in/']")
        href = link.get_attribute("href") if link else None
        if not href:
            continue
        m = _PROFILE_HREF.search(href)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))

        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        name = next((ln for ln in lines if ln and not _DEGREE.search(ln)), "")
        headline = next((ln for ln in lines[1:] if len(ln) > 12), "")

        person = Person(
            name=name[:80],
            headline=headline[:160],
            profile_url=f"https://www.linkedin.com/in/{m.group(1)}",
            degree=_parse_degree(text),
            company=company,
            shared_context=shared_context(headline, text, anchors or []),
        )
        if person.name:
            people.append(person)
            consume_quota("linkedin_view")
            view_budget -= 1

    session.pause()
    people.sort(key=lambda p: p.referral_score(), reverse=True)
    log.info("linkedin %s: %d candidate referrers", company, len(people))
    return people


def daily_budget() -> dict[str, int]:
    limits = targets().get("limits", {})
    return {
        "searches_left": remaining_quota("linkedin_search"),
        "views_left": remaining_quota("linkedin_view"),
        "search_cap": limits.get("linkedin_searches_per_run", 15),
        "view_cap": limits.get("linkedin_profile_views_per_run", 40),
    }
