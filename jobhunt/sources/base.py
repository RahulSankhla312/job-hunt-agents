"""Shared plumbing for every job source.

The single throttled fetcher here is not a nicety. Sourcing hits ~60 endpoints
per run; without per-host rate limiting and backoff the run gets the machine's
IP blocked, which costs far more than the listings are worth.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import logging
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Seconds between requests to the same host.
DEFAULT_DELAY = 1.0
HOST_DELAYS = {
    "boards-api.greenhouse.io": 0.4,   # public API, generous
    "api.ashbyhq.com": 0.5,
    "api.lever.co": 0.6,
    "remotive.com": 1.5,
    "remoteok.com": 2.0,               # asks for courtesy in its own API terms
    "www.linkedin.com": 2.5,           # logged-out search; limits bursts hard
    "wellfound.com": 2.0,
    "weworkremotely.com": 2.0,
    "www.amazon.jobs": 1.0,
}

# Board discovery probes many distinct Workday tenant hosts, almost all of
# which 404. Each tenant is its own host, so a shorter delay does not
# concentrate load on anyone.
WORKDAY_HOST_DELAY = 0.25


@dataclass
class RawJob:
    """A posting as the source gave it to us, before normalisation."""

    source: str
    source_id: str
    title: str
    company: str
    url: str
    location: str = ""
    description: str = ""
    remote: bool = False
    posted_at: datetime | None = None
    ats: str | None = None
    ats_slug: str | None = None
    # Published compensation, when the source actually provides it.
    comp_min: float | None = None
    comp_max: float | None = None
    comp_currency: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def dedupe_hash(self) -> str:
        """Stable identity across sources.

        Keyed on company + normalised title + normalised location, so the same
        role surfaced by Greenhouse and by RemoteOK collapses into one row.
        """
        key = "|".join(
            (
                _slug(self.company),
                _slug(_canonical_title(self.title)),
                _slug(_canonical_location(self.location)),
            )
        )
        return hashlib.sha256(key.encode()).hexdigest()[:32]


class Source(Protocol):
    name: str

    def fetch(self) -> list[RawJob]: ...


# --------------------------------------------------------------------------
# Throttled HTTP
# --------------------------------------------------------------------------


class Fetcher:
    """One shared client with per-host pacing and bounded retries."""

    def __init__(self, timeout: float = 20.0) -> None:
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/*",
                # Exclude zstd. amazon.jobs serves it, and httpx's zstandard
                # decoder raises "cannot use a decompressobj multiple times" on
                # those responses - which surfaced as an empty source rather
                # than an error.
                "Accept-Encoding": "gzip, deflate",
            },
        )
        self._last: dict[str, float] = defaultdict(float)
        self._lock = threading.Lock()

    def _wait(self, host: str) -> None:
        if host.endswith(".myworkdayjobs.com"):
            delay = WORKDAY_HOST_DELAY
        else:
            delay = HOST_DELAYS.get(host, DEFAULT_DELAY)
        with self._lock:
            elapsed = time.monotonic() - self._last[host]
            if elapsed < delay:
                time.sleep(delay - elapsed)
            self._last[host] = time.monotonic()

    def get(self, url: str, *, retries: int = 2, **kwargs: Any) -> httpx.Response | None:
        """GET with pacing and backoff. Returns None rather than raising.

        A single dead board must never abort a whole sourcing run.
        """
        host = urlparse(url).netloc
        for attempt in range(retries + 1):
            self._wait(host)
            try:
                resp = self._client.get(url, **kwargs)
            except httpx.HTTPError as exc:
                if attempt == retries:
                    # Last attempt: surface it. A transport or decode failure
                    # silently returns an empty source otherwise.
                    log.warning("%s failed after %d attempts: %s", url, attempt + 1, exc)
                    return None
                log.debug("%s: %s", url, exc)
                time.sleep(2**attempt)
                continue

            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 503):
                wait = float(resp.headers.get("Retry-After", 2**attempt * 2))
                log.warning("%s throttled us (%s); backing off %.0fs", host, resp.status_code, wait)
                time.sleep(min(wait, 30))
                continue
            # 404 on a board slug is normal - the company moved ATS.
            log.debug("%s -> HTTP %s", url, resp.status_code)
            return None
        return None

    def post(self, url: str, *, retries: int = 2, **kwargs: Any) -> httpx.Response | None:
        """POST with the same pacing and backoff as :meth:`get`.

        Workday's job search is a POST endpoint, so the throttling has to apply
        here too - it is the same host budget.
        """
        host = urlparse(url).netloc
        for attempt in range(retries + 1):
            self._wait(host)
            try:
                resp = self._client.post(url, **kwargs)
            except httpx.HTTPError as exc:
                log.debug("%s: %s", url, exc)
                if attempt == retries:
                    return None
                time.sleep(2**attempt)
                continue

            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 503):
                wait = float(resp.headers.get("Retry-After", 2**attempt * 2))
                log.warning("%s throttled us (%s); backing off %.0fs", host, resp.status_code, wait)
                time.sleep(min(wait, 30))
                continue
            log.debug("%s -> HTTP %s", url, resp.status_code)
            return None
        return None

    def json(self, url: str, **kwargs: Any) -> Any | None:
        resp = self.get(url, **kwargs)
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError:
            log.debug("%s returned non-JSON", url)
            return None

    def post_json(self, url: str, **kwargs: Any) -> Any | None:
        resp = self.post(url, **kwargs)
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError:
            log.debug("%s returned non-JSON", url)
            return None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_HTML_TAG = re.compile(r"<[^>]+>")

# Decorations that differ between boards for the same role and would otherwise
# defeat deduplication.
_TITLE_NOISE = re.compile(
    r"\b(?:remote|hybrid|onsite|on-site|full[- ]?time|part[- ]?time|contract|"
    r"urgent|hiring|new|w/?m/?d|m/?f/?d|\(f/m/x\)|h/f)\b",
    re.IGNORECASE,
)
_TITLE_BRACKETS = re.compile(r"[\(\[\{][^)\]\}]*[\)\]\}]")

# Level markers must survive normalisation and be compared exactly: merging
# "Software Engineer II" into "Software Engineer III" would defeat the entire
# level filter this system is built around.
_LEVEL_TOKENS = re.compile(
    r"\b(?:i{1,3}|iv|[1-4]|junior|jr|senior|sr|staff|principal|lead|entry|associate|"
    r"intern|new\s?grad|l[3-7])\b",
    re.IGNORECASE,
)

# Words too generic to distinguish two roles at the same company.
_GENERIC_TOKENS = frozenset(
    {"software", "engineer", "engineering", "developer", "development", "and", "of",
     "the", "for", "to", "at", "in", "a", "an", "team", "role", "job", "opening"}
)


def _slug(text: str) -> str:
    return _NON_ALNUM.sub("-", (text or "").strip().lower()).strip("-")


def _canonical_title(title: str) -> str:
    """Strip board-specific decoration so the same role hashes identically.

    Parentheticals are kept. They routinely carry the distinguishing detail -
    "Backend Software Engineer (Evals)" and "Software Engineer, Backend
    (Cooperative AI)" are different jobs, and stripping the bracket made them
    hash identically.
    """
    t = _TITLE_NOISE.sub(" ", title or "")
    t = re.sub(r"[-–—|,/()\[\]{}]+", " ", t)
    # Roman/arabic level equivalence: "Engineer II" == "Engineer 2".
    t = re.sub(r"\biii\b", "3", t, flags=re.I)
    t = re.sub(r"\bii\b", "2", t, flags=re.I)
    t = re.sub(r"\bi\b", "1", t, flags=re.I)
    return _WS.sub(" ", t).strip().lower()


def title_level(title: str) -> str:
    """The seniority marker in a title, or "" when unmarked."""
    found = _LEVEL_TOKENS.findall(_canonical_title(title))
    return found[0].lower() if found else ""


def distinguishing_tokens(title: str) -> frozenset[str]:
    """Content words that actually separate one role from another."""
    return frozenset(
        tok
        for tok in _canonical_title(title).split()
        if tok not in _GENERIC_TOKENS and len(tok) > 1
    )


def _canonical_location(location: str) -> str:
    """Collapse the many spellings of 'remote' and of Bangalore."""
    loc = (location or "").strip().lower()
    if not loc:
        return ""
    if re.search(r"\bremote\b|\banywhere\b|work from home", loc):
        if re.search(r"\bindia\b", loc):
            return "remote-india"
        if re.search(r"\b(us|usa|united states)\b", loc):
            return "remote-us"
        return "remote"
    loc = re.sub(r"\bbengaluru\b", "bangalore", loc)
    loc = re.sub(r"\bgurugram\b", "gurgaon", loc)
    # Keep only the first component: "Bangalore, Karnataka, India" -> "bangalore".
    return _WS.sub(" ", loc.split(",")[0]).strip()


def strip_html(markup: str) -> str:
    """Plain text from an ATS description blob.

    Uses ``html.unescape`` rather than a hand-rolled entity table - HN in
    particular emits numeric entities (``&#x2F;``) that a named-entity list
    silently leaves in the output.
    """
    if not markup:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", markup, flags=re.DOTALL | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h[1-6]>", "\n", text, flags=re.I)
    text = _HTML_TAG.sub(" ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def looks_remote(location: str, description: str = "") -> bool:
    blob = f"{location} {description[:500]}".lower()
    return bool(re.search(r"\bremote\b|\bwork from home\b|\bdistributed\b|\banywhere\b", blob))
