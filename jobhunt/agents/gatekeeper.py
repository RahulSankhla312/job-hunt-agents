"""Gatekeeper - deterministic filtering, before any LLM call.

This is where ~95% of the funnel is cut. Three independent classifiers run in
order of cost:

1. **level** - title and years-of-experience banding (pure regex);
2. **work authorisation** - three-way, because "US-only" and "sponsors visas"
   are opposite answers for an India-based applicant and a binary drop loses
   every sponsoring role;
3. **compensation** - published figures when a source gives them, a tier
   estimate otherwise.

Every rejection records *why*. A filter you cannot audit is a filter you
cannot tune, and a silently-dropped good job is invisible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlmodel import select

from jobhunt.config import company_tiers, known_sponsors, targets
from jobhunt.store.db import session
from jobhunt.store.models import Eligibility, Job, JobStatus, LevelBand

# Conversion only ever used to compare a USD figure against the USD floor and
# an INR figure against the INR floor - never to convert between the two,
# which would be meaningless for comparing an India CTC with a US base.
LAKH = 100_000


# --------------------------------------------------------------------------
# Level
# --------------------------------------------------------------------------

_YEARS_PATTERNS = [
    re.compile(r"(\d+)\s*\+?\s*(?:-|to|–)\s*(\d+)\s*(?:\+)?\s*years?", re.I),
    re.compile(r"(?:minimum|at least|min\.?|over)\s*(?:of\s*)?(\d+)\s*\+?\s*years?", re.I),
    re.compile(r"(\d+)\s*\+\s*years?", re.I),
    re.compile(r"(\d+)\s*years?\s+of\s+(?:relevant\s+|professional\s+|industry\s+)?experience", re.I),
]
_PHD = re.compile(r"\bph\.?\s?d\b|\bdoctorate\b", re.I)
_PHD_OPTIONAL = re.compile(r"ph\.?\s?d[^.]{0,60}\b(?:or|preferred|a plus|nice to have|equivalent)\b", re.I)

# Seniority markers that demote an otherwise-direct title match. Enumerating
# every senior phrasing in the config is hopeless; this catches the prefix.
_SENIORITY_PREFIX = re.compile(r"\b(?:senior|snr|sr\.?|lead|principal|staff)\b", re.I)

_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7}

# An explicit ladder marker: "SDE 3", "Level 4", "IC3", "L5", "SWE-2".
_EXPLICIT_LEVEL = re.compile(
    r"\b(?:l|lvl|level|ic|sde|swe|mts|engineer|developer|scientist)\s*[-–]?\s*"
    r"(?:(?P<num>[1-7])|(?P<roman>i{1,3}|iv|vi{0,2}))\b",
    re.IGNORECASE,
)


def title_level_number(title: str) -> int | None:
    """The ladder rung a title names, in Arabic digits, or None.

    Both spellings have to be handled. Listing "software engineer ii" in the
    config covered the Roman form only, so "Software Engineer 3" fell through
    to the generic "software engineer" pattern and banded as a *direct* match
    for a candidate with 1.3 years - as did "Software Engineer 4".
    """
    if not title:
        return None
    for m in _EXPLICIT_LEVEL.finditer(title):
        if num := m.group("num"):
            return int(num)
        if roman := m.group("roman"):
            return _ROMAN.get(roman.lower())
    return None


def years_required(description: str) -> int | None:
    """Smallest credible "years of experience" demand in a JD.

    Descriptions list several figures (a 2-year minimum for the role, an
    8-year one for a nice-to-have). The minimum is the bar that actually
    gates an application, so that is what is returned.
    """
    if not description:
        return None
    found: list[int] = []
    head = description[:6000]  # requirements live near the top; avoid boilerplate
    for pattern in _YEARS_PATTERNS:
        for m in pattern.finditer(head):
            groups = [int(g) for g in m.groups() if g and g.isdigit()]
            if groups:
                found.append(min(groups))
    credible = [y for y in found if 0 < y <= 20]
    return min(credible) if credible else None


def _phrase_in(phrase: str, title: str) -> bool:
    """Word-boundary containment.

    Plain ``in`` matches "staff" inside "Member of Technical Staff", which is
    the standard AI-lab title for an individual contributor and precisely the
    kind of role this search wants.
    """
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase.lower())}(?![a-z0-9])", title) is not None


def _best_match(patterns: list[str], title: str) -> str | None:
    """Longest matching pattern wins.

    "Software Engineer II" contains "software engineer", so a first-match scan
    banded it as a direct match instead of a stretch. The most specific
    pattern is the correct one.
    """
    hits = [p for p in patterns if _phrase_in(p, title)]
    return max(hits, key=len) if hits else None


def classify_level(title: str, description: str = "") -> tuple[LevelBand, str]:
    cfg = targets().get("level", {})
    t = title.lower().strip()

    if cfg.get("phd_required_is_reject", True) and _PHD.search(description or ""):
        if not _PHD_OPTIONAL.search(description or ""):
            return LevelBand.REJECT, "PhD appears to be required"

    years = years_required(description)
    max_years = cfg.get("max_years_required", 3)
    if years is not None and years > max_years:
        return LevelBand.REJECT, f"requires {years}+ years (cap {max_years})"

    # The AI track is an explicit allowlist, checked before the reject list so
    # "Member of Technical Staff" is not thrown out for the word "staff".
    ai_hit = _best_match(cfg.get("ai_track_titles", []), t)

    reject_hit = _best_match(cfg.get("reject_titles", []), t)
    if reject_hit:
        # The allowlist only overrides a reject when the rejected word is part
        # of the allowlisted phrase itself - "staff" inside "member of
        # technical staff". Comparing lengths instead let "Manager, Applied AI
        # Engineering" and "Applied AI Architect" through as IC AI roles,
        # because "applied ai" is simply a longer string than "manager".
        overridden = ai_hit is not None and reject_hit.lower() in ai_hit.lower()
        if not overridden:
            return LevelBand.REJECT, f"title contains {reject_hit!r}"

    if ai_hit:
        return LevelBand.AI_TRACK, f"AI/ML track ({ai_hit!r}, {years or '?'} yrs required)"

    direct_hit = _best_match(cfg.get("direct_titles", []), t)
    stretch_hit = _best_match(cfg.get("stretch_titles", []), t)

    # An explicit ladder rung outranks every title pattern: it is the clearest
    # statement a posting makes about seniority.
    rung = title_level_number(t)
    max_rung = int(cfg.get("max_level_number", 2))
    if rung is not None:
        if rung > max_rung:
            return LevelBand.REJECT, f"level {rung} title (cap {max_rung})"
        if rung >= 2:
            return LevelBand.STRETCH, f"stretch (level {rung}, {years or '?'} yrs required)"
        return LevelBand.DIRECT, f"direct match (level {rung}, {years or '?'} yrs required)"

    # A seniority prefix always demotes a direct match. Listing every phrasing
    # is hopeless - "Senior Software Development Engineer" matched the generic
    # "software development engineer" and banded as a direct fit, when at
    # Amazon that title is L6 and two levels above this candidate.
    if direct_hit and _SENIORITY_PREFIX.search(t):
        return LevelBand.STRETCH, f"stretch (senior-titled {direct_hit!r}, {years or '?'} yrs)"

    # Most specific wins across both bands, so "Software Engineer II" lands on
    # the stretch pattern rather than the generic direct one.
    if stretch_hit and (not direct_hit or len(stretch_hit) >= len(direct_hit)):
        return LevelBand.STRETCH, f"stretch ({stretch_hit!r}, {years or '?'} yrs required)"
    if direct_hit:
        return LevelBand.DIRECT, f"direct match ({direct_hit!r}, {years or '?'} yrs required)"

    return LevelBand.REJECT, "title matches no configured band"


# --------------------------------------------------------------------------
# Work authorisation
# --------------------------------------------------------------------------

_US_LOCATION = re.compile(
    r"\b(?:united states|usa|u\.s\.|us only|california|new york|seattle|austin|boston|"
    r"san francisco|nyc|sf bay|remote\s*[-(]?\s*us)\b",
    re.I,
)
_INDIA_LOCATION = re.compile(r"\b(?:india|bengaluru|bangalore|hyderabad|pune|gurgaon|noida|chennai|mumbai|delhi)\b", re.I)


def classify_eligibility(
    location: str, description: str, company: str = ""
) -> tuple[Eligibility, str]:
    """Three-way: India-eligible, sponsorship-offered, or US-only.

    Only the last is dropped. A US posting that is simply silent about
    sponsorship is returned as UNKNOWN and handed to the Analyst - silently
    dropping those loses every role that would in fact have sponsored.
    """
    cfg = targets().get("work_authorization", {})
    blob = f"{location}\n{description[:8000]}".lower()

    # Explicit refusal wins over everything - it is the one unambiguous signal.
    for pattern in cfg.get("us_only_patterns", []):
        if pattern.lower() in blob:
            return Eligibility.US_ONLY_NO_SPONSORSHIP, f"states {pattern!r}"

    loc = location.lower()
    for pattern in cfg.get("india_eligible_patterns", []):
        if pattern.lower() in loc:
            return Eligibility.INDIA_ELIGIBLE, f"location matches {pattern!r}"

    for pattern in cfg.get("sponsorship_offered_patterns", []):
        if pattern.lower() in blob:
            return Eligibility.SPONSORSHIP_OFFERED, f"states {pattern!r}"

    if _INDIA_LOCATION.search(loc):
        return Eligibility.INDIA_ELIGIBLE, "India location"

    if _US_LOCATION.search(loc):
        # The known-sponsor list is built from US H-1B filings, so it only
        # says anything about US roles. Applying it globally marked a London
        # posting as sponsoring on the strength of a US visa programme.
        if company and company in set(known_sponsors().get("companies", [])):
            return Eligibility.SPONSORSHIP_OFFERED, f"{company} is a known US visa sponsor"
        return Eligibility.UNKNOWN, "US location, sponsorship not stated - needs review"

    return Eligibility.UNKNOWN, "no clear eligibility signal"


# --------------------------------------------------------------------------
# Compensation
# --------------------------------------------------------------------------

_LPA = re.compile(r"(?:₹|inr|rs\.?)?\s*(\d+(?:\.\d+)?)\s*(?:-|–|to)?\s*(\d+(?:\.\d+)?)?\s*(?:lpa|lakhs?\s*(?:per annum|pa)?|l\s*pa)", re.I)
_USD = re.compile(r"\$\s*(\d[\d,]*)(?:\s*[kK])?\s*(?:-|–|to)\s*\$?\s*(\d[\d,]*)(?:\s*[kK])?", re.I)


def parse_comp_text(text: str) -> tuple[float | None, float | None, str | None]:
    """Pull a salary band out of free text, when no structured field exists."""
    if not text:
        return None, None, None

    if m := _LPA.search(text):
        lo = float(m.group(1)) * LAKH
        hi = float(m.group(2)) * LAKH if m.group(2) else None
        return lo, hi, "INR"

    if m := _USD.search(text):
        def scale(raw: str, whole: str) -> float:
            v = float(raw.replace(",", ""))
            # "$120k - $150k" and "$120,000 - $150,000" must agree.
            return v * 1000 if ("k" in whole.lower() and v < 1000) else v

        return scale(m.group(1), m.group(0)), scale(m.group(2), m.group(0)), "USD"

    return None, None, None


def _norm_company(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def skipped_company(company: str) -> str | None:
    """The skip-list entry this company matches, if any.

    Matched as whole words, so "Infosys" skips "Infosys BPM" but "TCS" does
    not skip "Tactics".
    """
    name = f" {_norm_company(company)} "
    for entry in targets().get("skip_companies") or []:
        key = _norm_company(str(entry))
        if key and f" {key} " in name:
            return str(entry)
    return None


def _tier_of(company: str) -> int | None:
    for entry in company_tiers().get("companies", []):
        if entry["name"].lower() == company.lower():
            return entry.get("tier")
    return None


def check_compensation(
    company: str,
    location: str,
    comp_min: float | None,
    comp_max: float | None,
    currency: str | None,
    description: str = "",
) -> tuple[bool, bool, str]:
    """Return ``(passes_floor, is_estimated, reason)``."""
    cfg = targets().get("compensation", {})
    india_floor = float(cfg.get("india_min_ctc_lpa", 45)) * LAKH
    us_floor = float(cfg.get("us_remote_min_usd", 120_000))
    is_india = bool(_INDIA_LOCATION.search(location or ""))

    if comp_min is None and comp_max is None:
        comp_min, comp_max, currency = parse_comp_text(description[:4000])

    if comp_min or comp_max:
        best = comp_max or comp_min or 0
        cur = (currency or "").upper()
        if cur == "INR":
            ok = best >= india_floor
            return ok, False, f"published ₹{best / LAKH:.1f}L vs floor ₹{india_floor / LAKH:.0f}L"
        if cur == "USD" and is_india:
            # An India role paid in dollars (remote-first startups do this) is
            # judged against the India floor, not the US one.
            rate = float(cfg.get("usd_to_inr", 85))
            inr = best * rate
            ok = inr >= india_floor
            return ok, False, (
                f"published ${best:,.0f} (~₹{inr / LAKH:.0f}L) vs floor ₹{india_floor / LAKH:.0f}L"
            )
        if cur in ("USD", "", None):
            ok = best >= us_floor
            return ok, False, f"published ${best:,.0f} vs floor ${us_floor:,.0f}"
        # Another currency (EUR, GBP, CAD): not comparable to either floor
        # without an FX feed, so let the Analyst judge rather than guess.
        return True, True, f"published {cur} {best:,.0f} - currency not comparable, deferring"

    tier = _tier_of(company)
    if tier is None:
        return True, True, "unknown company, no comp published - deferring to Analyst"

    bands = company_tiers().get("bands", {}).get(tier, {})
    key = "india_ctc_lpa" if is_india else "us_base_usd"
    band = bands.get(key)
    if not band:
        return True, True, f"tier {tier}, no band configured - deferring"

    band_max = float(band[1]) * (LAKH if is_india else 1)
    floor = india_floor if is_india else us_floor
    unit = f"₹{band[0]}-{band[1]}L" if is_india else f"${band[0]:,}-{band[1]:,}"
    pass_tier = cfg.get("pass_unknown_comp_at_tier", 1)

    if tier <= pass_tier:
        return True, True, f"tier {tier} ({unit}) clears floor on estimate"
    if band_max >= floor:
        return True, True, f"tier {tier} ({unit}) may clear floor - deferring"
    return False, True, f"tier {tier} ({unit}) below floor"


# --------------------------------------------------------------------------
# Batch pass over the database
# --------------------------------------------------------------------------


@dataclass
class GateStats:
    considered: int = 0
    rejected_level: int = 0
    rejected_auth: int = 0
    rejected_comp: int = 0
    rejected_company: int = 0
    passed: int = 0
    by_band: dict[str, int] = field(default_factory=dict)
    by_eligibility: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "considered": self.considered,
            "rejected_level": self.rejected_level,
            "rejected_auth": self.rejected_auth,
            "rejected_comp": self.rejected_comp,
            "passed": self.passed,
            "by_band": self.by_band,
            "by_eligibility": self.by_eligibility,
        }


def run(*, limit: int | None = None, dry_run: bool = False) -> GateStats:
    """Apply all three filters to every unfiltered job.

    Ordered cheapest-first: the level regex eliminates most rows before the
    longer description scans ever run.
    """
    stats = GateStats()

    with session() as s:
        query = select(Job).where(Job.status == JobStatus.NEW)
        if limit:
            query = query.limit(limit)
        jobs = s.exec(query).all()

        for job in jobs:
            stats.considered += 1
            description = job.description or ""

            if skipped := skipped_company(job.company):
                stats.rejected_company += 1
                job.status = JobStatus.REJECTED_GATE
                job.status_reason = f"company: on your skip list ({skipped})"
                continue

            band, level_reason = classify_level(job.title, description)
            job.level_band = band
            job.level_reason = level_reason
            stats.by_band[band.value] = stats.by_band.get(band.value, 0) + 1
            if band is LevelBand.REJECT:
                stats.rejected_level += 1
                job.status = JobStatus.REJECTED_GATE
                job.status_reason = f"level: {level_reason}"
                continue

            eligibility, auth_reason = classify_eligibility(
                job.location or "", description, job.company
            )
            job.eligibility = eligibility
            job.eligibility_reason = auth_reason
            stats.by_eligibility[eligibility.value] = (
                stats.by_eligibility.get(eligibility.value, 0) + 1
            )
            if eligibility is Eligibility.US_ONLY_NO_SPONSORSHIP:
                stats.rejected_auth += 1
                job.status = JobStatus.REJECTED_GATE
                job.status_reason = f"work auth: {auth_reason}"
                continue

            passes, estimated, comp_reason = check_compensation(
                job.company, job.location or "", job.comp_min, job.comp_max,
                job.comp_currency, description,
            )
            job.comp_is_estimated = estimated
            job.comp_reason = comp_reason
            if not passes:
                stats.rejected_comp += 1
                job.status = JobStatus.REJECTED_GATE
                job.status_reason = f"comp: {comp_reason}"
                continue

            stats.passed += 1
            # GATED, not NEW: the query above selects NEW, so leaving it there
            # would re-filter the same rows on every run and lose the
            # distinction between "passed the gate" and "never gated".
            job.status = JobStatus.GATED
            job.status_reason = "passed gatekeeper"

        if dry_run:
            s.rollback()
    return stats


def reset() -> int:
    """Return every gated/rejected job to unfiltered.

    Needed whenever ``defaults/targets.yaml`` changes: the filters are only
    tunable if their previous verdicts can be thrown away and recomputed.
    Jobs that have progressed past scoring are left alone.
    """
    resettable = (JobStatus.GATED, JobStatus.REJECTED_GATE)
    with session() as s:
        jobs = s.exec(select(Job).where(Job.status.in_(resettable))).all()
        for job in jobs:
            job.status = JobStatus.NEW
            job.status_reason = None
        return len(jobs)


# --------------------------------------------------------------------------
# Auditing work that already moved past the gate
# --------------------------------------------------------------------------


PROGRESSED = (
    JobStatus.SCORED,
    JobStatus.TAILORING,
    JobStatus.READY,
    JobStatus.AWAITING_REFERRAL,
    JobStatus.QUEUED_APPROVAL,
)


@dataclass
class AuditFinding:
    job_id: int
    company: str
    title: str
    score: float | None
    status: str
    old_band: str
    new_band: str
    reason: str


@dataclass
class AuditReport:
    checked: int = 0
    rejected: list[AuditFinding] = field(default_factory=list)
    rebanded: list[AuditFinding] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "checked": self.checked,
            "rejected": len(self.rejected),
            "rebanded": len(self.rebanded),
        }


def audit_progressed(*, apply_changes: bool = False) -> AuditReport:
    """Re-check jobs that are already past the gate against today's rules.

    :func:`reset` deliberately leaves scored and tailored jobs alone, which is
    right - it must not throw away a day's work every time a threshold moves.
    But that also means a level rule tightened *after* those jobs passed never
    reaches them, and a role two rungs too senior sits in the queue looking
    approved. This closes that gap explicitly rather than silently.

    Jobs already APPLIED are never touched: the application is out, and
    rewriting its status would only lose the record of what was sent.
    """
    report = AuditReport()

    with session() as s:
        jobs = s.exec(select(Job).where(Job.status.in_(PROGRESSED))).all()
        report.checked = len(jobs)

        for job in jobs:
            band, reason = classify_level(job.title, job.description or "")
            old = job.level_band.value if hasattr(job.level_band, "value") else str(job.level_band)
            if band == old:
                continue

            finding = AuditFinding(
                job_id=job.id or 0,
                company=job.company,
                title=job.title,
                score=job.score,
                status=job.status.value,
                old_band=old,
                new_band=band.value,
                reason=reason,
            )

            if band == LevelBand.REJECT:
                report.rejected.append(finding)
                if apply_changes:
                    job.level_band = band
                    job.level_reason = reason
                    job.status = JobStatus.REJECTED_GATE
                    job.status_reason = f"level: {reason} (re-audited after a config change)"
            else:
                report.rebanded.append(finding)
                if apply_changes:
                    job.level_band = band
                    job.level_reason = reason

        if not apply_changes:
            s.rollback()

    return report
