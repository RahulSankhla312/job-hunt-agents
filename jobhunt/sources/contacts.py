"""Recruiter and hiring-manager discovery, free-tier only.

Fans out across sources and cross-references them, because no single free
source is reliable enough on its own:

1. **Google Programmable Search** - 100 queries/day free and officially
   sanctioned. This is the durable version of "scrape Google": raw SERP
   scraping gets the IP blocked within hours and breaks their terms.
2. **Company surfaces** - /team, /about, engineering-blog bylines. Often names
   the actual hiring manager, which beats a generic recruiter.
3. **GitHub** - engineers frequently expose a public email on their profile.
   High quality for AI and infrastructure roles specifically.

Every contact carries a confidence score. Only high-confidence, verified
addresses are emailed; the rest are queued as LinkedIn messages instead, so a
good lead is never wasted on a bounce.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from jobhunt.config import settings
from jobhunt.sources.base import Fetcher, strip_html

log = logging.getLogger(__name__)

CSE_URL = "https://www.googleapis.com/customsearch/v1"
DDG_URL = "https://html.duckduckgo.com/html/"
GITHUB_SEARCH = "https://api.github.com/search/users"
GITHUB_USER = "https://api.github.com/users/{login}"

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_LINKEDIN_IN = re.compile(r"linkedin\.com/in/([A-Za-z0-9\-_%]+)")

# Addresses that are never a person.
_ROLE_ADDRESS = re.compile(
    r"^(?:info|support|help|sales|contact|hello|hi|admin|noreply|no-reply|"
    r"donotreply|webmaster|abuse|privacy|legal|press|media|marketing|billing|"
    r"security|jobs|careers|hr)@",
    re.IGNORECASE,
)

TEAM_PATHS = ("/about", "/team", "/company/team", "/about-us", "/leadership", "/people")

PATTERNS = {
    "first.last": lambda f, l: f"{f}.{l}",
    "firstlast": lambda f, l: f"{f}{l}",
    "first": lambda f, l: f,
    "flast": lambda f, l: f"{f[:1]}{l}",
    "first_last": lambda f, l: f"{f}_{l}",
    "lastf": lambda f, l: f"{l}{f[:1]}",
}


@dataclass
class Contact:
    name: str
    company: str
    title: str = ""
    email: str | None = None
    linkedin_url: str | None = None
    source: str = ""
    pattern: str | None = None
    verified: bool = False
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)

    def score(self) -> float:
        """Confidence that this address reaches this person."""
        score = 0.0
        if self.email and self.verified:
            score += 0.55
        elif self.email:
            score += 0.25          # derived from a pattern, unconfirmed
        if self.pattern:
            score += 0.15          # the company's convention is known
        if self.linkedin_url:
            score += 0.15
        if any(t in self.title.lower() for t in ("recruit", "talent", "hiring", "people")):
            score += 0.15
        if self.source in ("company_site", "github"):
            score += 0.10          # observed, not inferred
        return round(min(score, 1.0), 2)


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


def google_cse(fetcher: Fetcher, query: str, *, limit: int = 10) -> list[dict]:
    """Official Programmable Search. Returns [] when no key is configured."""
    cfg = settings()
    if not (cfg.google_cse_api_key and cfg.google_cse_cx):
        return []
    data = fetcher.json(
        CSE_URL,
        params={
            "key": cfg.google_cse_api_key,
            "cx": cfg.google_cse_cx,
            "q": query,
            "num": min(limit, 10),
        },
    )
    return (data or {}).get("items", []) or []


def duckduckgo(fetcher: Fetcher, query: str, *, limit: int = 10) -> list[dict]:
    """Fallback for when the CSE daily quota is spent."""
    resp = fetcher.get(DDG_URL, params={"q": query})
    if resp is None:
        return []
    items: list[dict] = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', resp.text):
        items.append({"link": m.group(1), "title": strip_html(m.group(2))})
        if len(items) >= limit:
            break
    return items


def find_people(fetcher: Fetcher, company: str, *, roles: tuple[str, ...] = ()) -> list[Contact]:
    """Search for recruiters and engineering leads at a company."""
    roles = roles or ("technical recruiter", "engineering manager", "talent acquisition")
    found: dict[str, Contact] = {}

    for role in roles:
        query = f'site:linkedin.com/in "{role}" "{company}"'
        results = google_cse(fetcher, query) or duckduckgo(fetcher, query)
        for item in results:
            link = item.get("link", "")
            m = _LINKEDIN_IN.search(link)
            if not m:
                continue
            title_text = item.get("title", "")
            # LinkedIn titles render as "Name - Role - Company | LinkedIn".
            name = title_text.split(" - ")[0].split(" | ")[0].strip()
            if not name or m.group(1) in found:
                continue
            found[m.group(1)] = Contact(
                name=name[:80],
                company=company,
                title=title_text[:160],
                linkedin_url=f"https://www.linkedin.com/in/{m.group(1)}",
                source="cse" if settings().google_cse_api_key else "duckduckgo",
                evidence=[link],
            )
    return list(found.values())


# --------------------------------------------------------------------------
# Email pattern inference
# --------------------------------------------------------------------------


def harvest_emails(fetcher: Fetcher, domain: str) -> list[str]:
    """Real addresses observed on the company's own pages."""
    seen: list[str] = []
    for path in TEAM_PATHS:
        resp = fetcher.get(f"https://{domain}{path}")
        if resp is None:
            continue
        for address in _EMAIL.findall(resp.text):
            low = address.lower()
            if low.endswith(f"@{domain}") and not _ROLE_ADDRESS.match(low):
                if low not in seen:
                    seen.append(low)
    return seen


def infer_pattern(emails: list[str]) -> str | None:
    """Work out the company's address convention from observed examples."""
    votes: Counter[str] = Counter()
    for address in emails:
        local = address.split("@")[0].lower()
        if "." in local:
            votes["first.last"] += 1
        elif "_" in local:
            votes["first_last"] += 1
        elif len(local) <= 8:
            votes["first"] += 1
        else:
            votes["firstlast"] += 1
    return votes.most_common(1)[0][0] if votes else None


def derive_email(name: str, domain: str, pattern: str) -> str | None:
    parts = [re.sub(r"[^a-z]", "", p.lower()) for p in name.split() if p.strip()]
    parts = [p for p in parts if p]
    if len(parts) < 2 or pattern not in PATTERNS:
        return None
    return f"{PATTERNS[pattern](parts[0], parts[-1])}@{domain}"


def verify_email(address: str, *, timeout: float = 8.0) -> bool:
    """SMTP RCPT-TO check - does this mailbox exist?

    Discards bounces before they happen, which is what protects sender
    reputation. No message is ever sent: the connection is closed after the
    server answers.
    """
    import smtplib

    try:
        import dns.resolver  # type: ignore
    except ImportError:
        log.debug("dnspython not installed; cannot verify %s", address)
        return False

    domain = address.split("@")[-1]
    try:
        records = dns.resolver.resolve(domain, "MX")
        host = str(sorted(records, key=lambda r: r.preference)[0].exchange).rstrip(".")
    except Exception:  # noqa: BLE001
        return False

    try:
        with smtplib.SMTP(host, 25, timeout=timeout) as smtp:
            smtp.helo("example.com")
            smtp.mail("verify@example.com")
            code, _ = smtp.rcpt(address)
            return code in (250, 251)
    except (smtplib.SMTPException, OSError):
        return False


def enrich(fetcher: Fetcher, contacts: list[Contact], domain: str, *, verify: bool = True) -> list[Contact]:
    """Attach an email to each contact, and score the result."""
    observed = harvest_emails(fetcher, domain)
    pattern = infer_pattern(observed)
    if pattern:
        log.info("%s email pattern looks like %s (%d observed)", domain, pattern, len(observed))

    for contact in contacts:
        if pattern:
            contact.pattern = pattern
            contact.email = derive_email(contact.name, domain, pattern)
            if contact.email and verify:
                contact.verified = verify_email(contact.email)
        contact.confidence = contact.score()
    return contacts
