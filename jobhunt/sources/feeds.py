"""Free public job feeds.

These are the remote-first boards. Their real value is not volume but the
eligibility signal: Remotive's ``candidate_required_location`` and We Work
Remotely's ``region`` state outright whether a role is open worldwide, which is
exactly the question the Gatekeeper has to answer for an India-based applicant.
"""

from __future__ import annotations

import ast
import logging
import re
from datetime import UTC, datetime

from jobhunt.sources.base import Fetcher, RawJob, strip_html

log = logging.getLogger(__name__)

REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
REMOTEOK_URL = "https://remoteok.com/api"
ARBEITNOW_URL = "https://www.arbeitnow.com/api/job-board-api"
WWR_FEEDS = [
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
]
# search_by_date, not search: relevance ranking returns the 2020 thread, which
# is both stale and enormous. Sorting by date is the only way to get the
# current month's post.
HN_URL = (
    "https://hn.algolia.com/api/v1/search_by_date"
    "?tags=story,author_whoishiring&hitsPerPage=5"
)
HN_ITEMS = "https://hn.algolia.com/api/v1/items/{id}"


def _parse_taglist(raw: object) -> list[str]:
    """These feeds serialise tag lists as a Python-literal string."""
    if isinstance(raw, list):
        return [str(t) for t in raw]
    if isinstance(raw, str) and raw.startswith("["):
        try:
            return [str(t) for t in ast.literal_eval(raw)]
        except (ValueError, SyntaxError):
            return []
    return []


def _epoch(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(str(value)), tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


# --------------------------------------------------------------------------


def fetch_remotive(fetcher: Fetcher, limit: int = 400) -> list[RawJob]:
    payload = fetcher.json(f"{REMOTIVE_URL}?limit={limit}")
    if not payload or "jobs" not in payload:
        return []

    jobs: list[RawJob] = []
    for j in payload["jobs"]:
        # The single most useful field in any free feed: it says outright
        # whether someone in India may apply.
        eligible_in = j.get("candidate_required_location") or ""
        posted = None
        if raw := j.get("publication_date"):
            try:
                posted = datetime.fromisoformat(raw).replace(tzinfo=UTC)
            except ValueError:
                posted = None
        jobs.append(
            RawJob(
                source="remotive",
                source_id=str(j.get("id")),
                title=(j.get("title") or "").strip(),
                company=(j.get("company_name") or "").strip(),
                url=j.get("url") or "",
                location=eligible_in,
                description=strip_html(j.get("description") or ""),
                remote=True,
                posted_at=posted,
                extra={
                    "candidate_required_location": eligible_in,
                    "salary_text": j.get("salary") or "",
                    "category": j.get("category"),
                    "tags": _parse_taglist(j.get("tags")),
                },
            )
        )
    return jobs


def fetch_remoteok(fetcher: Fetcher) -> list[RawJob]:
    payload = fetcher.json(REMOTEOK_URL)
    if not isinstance(payload, list):
        return []

    jobs: list[RawJob] = []
    for j in payload:
        # The first element is a legal/ToS notice, not a posting.
        if not isinstance(j, dict) or "position" not in j:
            continue

        def _num(key: str, j: dict = j) -> float | None:
            try:
                v = float(j.get(key) or 0)
            except (TypeError, ValueError):
                return None
            return v or None  # RemoteOK uses "0" to mean "not disclosed"

        jobs.append(
            RawJob(
                source="remoteok",
                source_id=str(j.get("id")),
                title=(j.get("position") or "").strip(),
                company=(j.get("company") or "").strip(),
                url=j.get("url") or j.get("apply_url") or "",
                location=(j.get("location") or "").strip(),
                description=strip_html(j.get("description") or ""),
                remote=True,
                posted_at=_epoch(j.get("epoch")),
                comp_min=_num("salary_min"),
                comp_max=_num("salary_max"),
                comp_currency="USD" if _num("salary_min") else None,
                extra={"tags": _parse_taglist(j.get("tags"))},
            )
        )
    return jobs


def fetch_arbeitnow(fetcher: Fetcher) -> list[RawJob]:
    payload = fetcher.json(ARBEITNOW_URL)
    if not payload or "data" not in payload:
        return []

    jobs: list[RawJob] = []
    for j in payload["data"]:
        # `remote` arrives as the string "True"/"False", not a bool.
        remote = str(j.get("remote")).strip().lower() in ("true", "1")
        jobs.append(
            RawJob(
                source="arbeitnow",
                source_id=str(j.get("slug")),
                title=(j.get("title") or "").strip(),
                company=(j.get("company_name") or "").strip(),
                url=j.get("url") or "",
                location=(j.get("location") or "").strip(),
                description=strip_html(j.get("description") or ""),
                remote=remote,
                posted_at=_epoch(j.get("created_at")),
                extra={"tags": _parse_taglist(j.get("tags"))},
            )
        )
    return jobs


_WWR_TITLE = re.compile(r"^\s*(?P<company>[^:]+?)\s*:\s*(?P<role>.+)$")


def fetch_weworkremotely(fetcher: Fetcher) -> list[RawJob]:
    import feedparser

    jobs: list[RawJob] = []
    for url in WWR_FEEDS:
        resp = fetcher.get(url)
        if resp is None:
            continue
        feed = feedparser.parse(resp.text)
        for e in feed.entries:
            raw_title = e.get("title", "")
            # WWR titles are "Company: Role".
            if m := _WWR_TITLE.match(raw_title):
                company, title = m.group("company").strip(), m.group("role").strip()
            else:
                company, title = "", raw_title.strip()

            posted = None
            if parsed := e.get("published_parsed"):
                try:
                    posted = datetime(*parsed[:6], tzinfo=UTC)
                except (TypeError, ValueError):
                    posted = None

            region = e.get("region", "") or ""
            jobs.append(
                RawJob(
                    source="weworkremotely",
                    source_id=e.get("id") or e.get("link", ""),
                    title=title,
                    company=company,
                    url=e.get("link", ""),
                    location=region,
                    description=strip_html(e.get("summary", "")),
                    remote=True,
                    posted_at=posted,
                    extra={"region": region},
                )
            )
    return jobs


_HN_COMPANY = re.compile(r"^\s*([^|\n]{2,60}?)\s*\|")


def fetch_hn_hiring(fetcher: Fetcher, max_comments: int = 400) -> list[RawJob]:
    """The monthly 'Ask HN: Who is hiring?' thread.

    Top-level comments are free-form, but the community convention
    ``Company | Role | Location | Remote`` holds often enough to be worth
    parsing. Anything that does not match is skipped rather than guessed at.
    """
    search = fetcher.json(HN_URL)
    if not search or not search.get("hits"):
        return []

    story_id = None
    for hit in search["hits"]:
        if "who is hiring" in (hit.get("title") or "").lower():
            story_id = hit.get("objectID")
            break
    if story_id is None:
        return []

    thread = fetcher.json(HN_ITEMS.format(id=story_id))
    if not thread:
        return []

    jobs: list[RawJob] = []
    for child in (thread.get("children") or [])[:max_comments]:
        text = strip_html(child.get("text") or "")
        if len(text) < 40:
            continue
        first_line = text.splitlines()[0]
        m = _HN_COMPANY.match(first_line)
        if not m:
            continue
        company = m.group(1).strip()
        jobs.append(
            RawJob(
                source="hn_hiring",
                source_id=str(child.get("id")),
                title=first_line[:180],
                company=company,
                url=f"https://news.ycombinator.com/item?id={child.get('id')}",
                location=first_line,
                description=text,
                remote="remote" in first_line.lower(),
                posted_at=None,
                extra={"thread": story_id},
            )
        )
    return jobs


def _linkedin(fetcher: Fetcher) -> list[RawJob]:
    from jobhunt.sources.linkedin_jobs import fetch_linkedin

    return fetch_linkedin(fetcher)


def _wellfound(fetcher: Fetcher) -> list[RawJob]:
    from jobhunt.sources.wellfound import fetch_wellfound

    return fetch_wellfound(fetcher)


FEED_FETCHERS = {
    "remotive": fetch_remotive,
    "remoteok": fetch_remoteok,
    "arbeitnow": fetch_arbeitnow,
    "weworkremotely": fetch_weworkremotely,
    "hn_hiring": fetch_hn_hiring,
    "linkedin": _linkedin,
    "wellfound": _wellfound,
}


def fetch_all_feeds(fetcher: Fetcher) -> list[RawJob]:
    out: list[RawJob] = []
    for name, fn in FEED_FETCHERS.items():
        try:
            jobs = fn(fetcher)
        except Exception as exc:  # noqa: BLE001 - one bad feed must not kill the run
            log.warning("feed %s failed: %s", name, exc)
            continue
        log.info("feed %s: %d jobs", name, len(jobs))
        out.extend(jobs)
    return out
