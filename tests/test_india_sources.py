"""LinkedIn, Wellfound and SmartRecruiters sources, and the rules around them.

Parsed from recorded page fragments, so the suite never touches the network.
"""

from __future__ import annotations

import json

import pytest

# --------------------------------------------------------------------------
# LinkedIn (logged-out search)
# --------------------------------------------------------------------------

CARDS = """
<li><div class="base-card" data-entity-urn="urn:li:jobPosting:111">
  <h3 class="base-search-card__title">  Software Development Engineer I </h3>
  <h4 class="base-search-card__subtitle"><a href="#">Zepto</a></h4>
  <span class="job-search-card__location">Bengaluru, Karnataka, India</span>
  <time class="job-search-card__listdate" datetime="2026-09-25">3 days ago</time>
</div></li>
<li><div class="base-card" data-entity-urn="urn:li:jobPosting:222">
  <h3 class="base-search-card__title">Engineering Manager</h3>
  <h4 class="base-search-card__subtitle"><a href="#">Zepto</a></h4>
  <span class="job-search-card__location">Mumbai, India</span>
</div></li>
<li><div class="base-card" data-entity-urn="urn:li:jobPosting:333">
  <h3 class="base-search-card__title">Backend Engineer</h3>
  <h4 class="base-search-card__subtitle"><a href="#">Zepto Fans Club Consulting &amp; Co</a></h4>
  <span class="job-search-card__location">Pune, India</span>
</div></li>
"""

POSTING = """
<div class="show-more-less-html__markup">
  <p>Build <b>Python</b> services.</p><ul><li>2+ years of experience</li></ul>
</div>
<ul><li>
  <h3 class="description__job-criteria-subheader">Seniority level</h3>
  <span class="description__job-criteria-text">Entry level</span>
</li></ul>
"""


def test_linkedin_cards_are_parsed():
    from jobhunt.sources.linkedin_jobs import parse_cards

    cards = parse_cards(CARDS)
    assert [c["id"] for c in cards] == ["111", "222", "333"]
    assert cards[0]["title"] == "Software Development Engineer I"
    assert cards[0]["company"] == "Zepto"
    assert cards[0]["location"] == "Bengaluru, Karnataka, India"
    assert cards[0]["posted"] == "2026-09-25"
    assert cards[2]["company"] == "Zepto Fans Club Consulting & Co"


def test_linkedin_posting_is_parsed():
    from jobhunt.sources.linkedin_jobs import parse_posting

    detail = parse_posting(POSTING)
    assert "Python" in detail["description"] and "2+ years" in detail["description"]
    assert detail["criteria"] == {"Seniority level": "Entry level"}


@pytest.mark.parametrize(
    ("title", "ok"),
    [("Software Development Engineer I", True), ("AI Engineer", True),
     ("Engineering Manager", False), ("Sales Executive", False), ("SDE Intern", False)],
)
def test_linkedin_title_prefilter(title, ok):
    from jobhunt.sources.linkedin_jobs import worth_reading

    assert worth_reading(title) is ok


class FakeFetcher:
    """Serves search pages and postings; records every URL asked for."""

    def __init__(self, search_html: str, posting_html: str = POSTING, fail_after: int | None = None):
        self.search_html, self.posting_html = search_html, posting_html
        self.calls: list[str] = []
        self.fail_after = fail_after

    def get(self, url, params=None, retries=2):
        self.calls.append(url + (f"?{params}" if params else ""))
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            return None

        class R:
            pass

        r = R()
        r.text = self.search_html if "search" in url else self.posting_html
        return r


def test_a_watched_company_keeps_only_its_own_posts(monkeypatch):
    import jobhunt.sources.linkedin_jobs as li

    monkeypatch.setattr(li, "settings", lambda: {
        **li.DEFAULTS, "keywords": [], "watch_companies": ["Zepto"], "pages_per_company": 1,
    })
    jobs = li.fetch_linkedin(FakeFetcher(CARDS))
    assert [j.company for j in jobs] == ["Zepto"], "the consultancy match and the manager role are dropped"
    job = jobs[0]
    assert job.source == "linkedin" and job.ats == "linkedin"
    assert job.url == "https://www.linkedin.com/jobs/view/111"


def test_watched_companies_are_searched_before_general_keywords(monkeypatch):
    import jobhunt.sources.linkedin_jobs as li

    monkeypatch.setattr(li, "settings", lambda: {
        **li.DEFAULTS, "keywords": ["software engineer"], "watch_companies": ["Zepto"],
        "pages_per_company": 1, "pages_per_query": 1,
    })
    fetcher = FakeFetcher("")
    li.fetch_linkedin(fetcher)
    searches = [c for c in fetcher.calls if "search" in c]
    assert "Zepto software engineer" in searches[0]


def test_throttling_keeps_what_was_found(monkeypatch):
    import jobhunt.sources.linkedin_jobs as li

    monkeypatch.setattr(li, "settings", lambda: {
        **li.DEFAULTS, "keywords": ["a", "b", "c"], "watch_companies": [], "pages_per_query": 1,
    })
    jobs = li.fetch_linkedin(FakeFetcher(CARDS, fail_after=2))
    assert len(jobs) >= 1


# --------------------------------------------------------------------------
# Wellfound
# --------------------------------------------------------------------------


def _wellfound_page(jobs: list[dict], startups: list[dict]) -> str:
    data = {"__typename": "Query"}
    apollo = {}
    for j in jobs:
        apollo[f"JobListingSearchResult:{j['id']}"] = {"__typename": "JobListingSearchResult", **j}
    for s in startups:
        apollo[f"StartupResult:{s['id']}"] = {"__typename": "StartupResult", **s}
    payload = {"props": {"pageProps": {"apolloState": {"data": {**apollo, "ROOT_QUERY": data}}}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'


def test_wellfound_page_is_parsed_with_salary():
    from jobhunt.sources.wellfound import parse_page

    html = _wellfound_page(
        [{"id": "9", "title": "Backend Engineer", "slug": "backend-engineer", "jobType": "full-time",
          "compensation": "₹30L – ₹45L", "locationNames": ["Bengaluru"], "remote": False,
          "liveStartAt": 1790000000, "yearsExperienceMin": 1, "description": "Go and Postgres."},
         {"id": "10", "title": "Design Intern", "slug": "design-intern", "jobType": "internship",
          "compensation": "", "locationNames": ["Pune"]}],
        [{"id": "1", "name": "Acme Labs", "highlightedJobListings": [
            {"__ref": "JobListingSearchResult:9"}, {"__ref": "JobListingSearchResult:10"}]}],
    )
    jobs = parse_page(html)
    assert len(jobs) == 1, "internships are dropped"
    j = jobs[0]
    assert (j.company, j.title, j.location) == ("Acme Labs", "Backend Engineer", "Bengaluru")
    assert (j.comp_min, j.comp_max, j.comp_currency) == (3_000_000, 4_500_000, "INR")
    assert j.url == "https://wellfound.com/jobs/9-backend-engineer"
    assert j.description.startswith("Experience: 1+ years.")


@pytest.mark.parametrize(
    ("text", "expected"),
    [("₹15L – ₹25L", (1_500_000, 2_500_000, "INR")),
     ("₹8,00,000 – ₹12,00,000", (800_000, 1_200_000, "INR")),
     ("$120k – $160k", (120_000, 160_000, "USD")),
     ("", (None, None, None))],
)
def test_wellfound_compensation(text, expected):
    from jobhunt.sources.wellfound import parse_compensation

    assert parse_compensation(text) == expected


# --------------------------------------------------------------------------
# SmartRecruiters
# --------------------------------------------------------------------------


def test_smartrecruiters_board_is_read(monkeypatch):
    from jobhunt.sources.smartrecruiters import fetch_smartrecruiters

    class F:
        def json(self, url, params=None):
            if url.endswith("/postings"):
                return {"totalFound": 2, "content": [
                    {"id": "1", "name": "Software Dev Engineer II", "releasedDate": "2026-09-02T10:00:00Z",
                     "location": {"city": "BENGALURU", "country": "in"}},
                    {"id": "2", "name": "Sales Manager", "location": {"city": "Kolkata", "country": "in"}},
                ]}
            return {"postingUrl": "https://jobs.smartrecruiters.com/SWIGGY/1",
                    "jobAd": {"sections": {"jobDescription": {"text": "<p>Build Java services</p>"},
                                           "qualifications": {"text": "<p>2+ years</p>"}}}}

    jobs = fetch_smartrecruiters(F(), "swiggy", "Swiggy")
    assert [j.title for j in jobs] == ["Software Dev Engineer II"]
    assert jobs[0].location == "Bengaluru, India"
    assert "Build Java services" in jobs[0].description and "2+ years" in jobs[0].description
    assert jobs[0].url == "https://jobs.smartrecruiters.com/SWIGGY/1"


# --------------------------------------------------------------------------
# Gate rules
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("company", "skipped"),
    [("Infosys", True), ("Infosys BPM Ltd", True), ("Tata Consultancy Services", True),
     ("TCS", True), ("Tactics Labs", False), ("Zepto", False), ("Accenture in India", True)],
)
def test_the_skip_list_matches_whole_words(company, skipped, monkeypatch):
    import jobhunt.agents.gatekeeper as gk

    monkeypatch.setattr(gk, "targets", lambda: {"skip_companies": ["Infosys", "TCS", "Tata Consultancy Services", "Accenture"]})
    assert (gk.skipped_company(company) is not None) is skipped


def test_dollar_pay_on_an_india_role_is_judged_in_rupees(monkeypatch):
    import jobhunt.agents.gatekeeper as gk

    monkeypatch.setattr(gk, "targets", lambda: {"compensation": {"india_min_ctc_lpa": 45, "usd_to_inr": 85}})
    ok, _, reason = gk.check_compensation("Bitwarden", "Noida, India", 58_000, 58_000, "USD")
    assert ok and "₹" in reason, reason
    ok, _, _ = gk.check_compensation("Tiny", "Pune, India", 30_000, 30_000, "USD")
    assert not ok


def test_filters_form_saves_where_to_look():
    from jobhunt.web.setup import parse_targets

    errors: list[str] = []
    out = parse_targets({
        "t_sources_present": "1", "t_linkedin_enabled": "on",
        "t_linkedin_keywords": "AI engineer\nSDE 1", "t_watch_companies": "Zepto\nNutanix",
        "t_skip_companies": "Infosys",
    }, errors)
    assert errors == []
    assert out["sources"]["linkedin"] == {
        "enabled": True, "keywords": ["AI engineer", "SDE 1"], "watch_companies": ["Zepto", "Nutanix"],
    }
    assert out["sources"]["wellfound"] == {"enabled": False}
    assert out["skip_companies"] == ["Infosys"]


# --------------------------------------------------------------------------
# LinkedIn jobs are applied to by you
# --------------------------------------------------------------------------


@pytest.fixture
def linkedin_job():
    from sqlmodel import select

    from jobhunt.store.db import init_db, session
    from jobhunt.store.models import Job, JobStatus

    init_db()

    def purge():
        with session() as s:
            for j in s.exec(select(Job).where(Job.source_id == "li-fixture")).all():
                s.delete(j)

    purge()
    with session() as s:
        job = Job(source="linkedin", source_id="li-fixture", url="https://www.linkedin.com/jobs/view/1",
                  company="Zepto", title="SDE I", dedupe_hash="li-fixture", ats="linkedin",
                  status=JobStatus.READY, score=4.8)
        s.add(job)
        s.flush()
        job_id = job.id
    yield job_id
    purge()


def test_the_evening_run_never_opens_a_browser_for_linkedin_jobs(linkedin_job, monkeypatch):
    import jobhunt.agents.applicant as applicant
    from jobhunt.store.db import session
    from jobhunt.store.models import Job, JobStatus

    def forbidden(*_a, **_k):
        raise AssertionError("no browser and no cover letter for a LinkedIn job")

    monkeypatch.setattr(applicant, "browser", forbidden)
    monkeypatch.setattr(applicant, "prepare_cover_letter", forbidden)
    applicant.run(limit=50)
    with session() as s:
        job = s.get(Job, linkedin_job)
    assert job.status is JobStatus.QUEUED_APPROVAL
    assert "apply on LinkedIn yourself" in job.status_reason


def test_the_job_page_offers_apply_on_linkedin(linkedin_job):
    from starlette.testclient import TestClient

    from jobhunt.web.app import app

    body = TestClient(app).get(f"/job/{linkedin_job}").text
    assert "Apply on LinkedIn" in body
    assert "Fill it &amp; let me submit" not in body


@pytest.mark.parametrize(
    ("listed", "watched", "same"),
    [("Zepto", "Zepto", True), ("Flipkart Internet Private Limited", "Flipkart", True),
     ("Uber India Systems Pvt Ltd", "Uber", True), ("Zepto Fans Club Consulting & Co", "Zepto", False),
     ("Dream11", "Dream11", True), ("Swiggy Instamart", "Swiggy", False), ("Olacabs", "Ola", False)],
)
def test_watched_company_matching(listed, watched, same):
    from jobhunt.sources.linkedin_jobs import same_company

    assert same_company(listed, watched) is same


def test_smartrecruiters_skips_roles_abroad_without_reading_them():
    from jobhunt.sources.smartrecruiters import fetch_smartrecruiters

    read: list[str] = []

    class F:
        def json(self, url, params=None):
            if url.endswith("/postings"):
                return {"totalFound": 3, "content": [
                    {"id": "1", "name": "Software Engineer", "location": {"city": "Hyderabad", "country": "in"}},
                    {"id": "2", "name": "Software Engineer", "location": {"city": "Kuala Lumpur", "country": "my"}},
                    {"id": "3", "name": "Software Engineer", "location": {"country": "us", "remote": True}},
                ]}
            read.append(url.rsplit("/", 1)[-1])
            return {}

    jobs = fetch_smartrecruiters(F(), "grab", "Grab")
    assert read == ["1", "3"], "the Malaysia role is never opened"
    assert len(jobs) == 2


# --------------------------------------------------------------------------
# Scoring order - found in use: the first version crashed the Analyst
# ('int' object is not subscriptable) and scored nothing.
# --------------------------------------------------------------------------


def test_known_companies_are_scored_first_and_selection_runs_on_a_real_db():
    from datetime import UTC, datetime, timedelta

    from sqlmodel import select

    from jobhunt.agents.analyst import pick_for_scoring
    from jobhunt.store.db import init_db, session
    from jobhunt.store.models import Eligibility, Job, JobStatus

    init_db()
    now = datetime.now(UTC)
    rows = [
        ("pick-agency", "Random Agency", Eligibility.INDIA_ELIGIBLE, now),
        ("pick-zepto", "Zepto", Eligibility.INDIA_ELIGIBLE, now - timedelta(days=5)),
        ("pick-swiggy", "Swiggy", Eligibility.INDIA_ELIGIBLE, now - timedelta(days=9)),
        ("pick-us", "Zepto", Eligibility.UNKNOWN, now),
    ]
    with session() as s:
        for sid, *_ in rows:
            for j in s.exec(select(Job).where(Job.source_id == sid)).all():
                s.delete(j)
    with session() as s:
        for sid, company, elig, posted in rows:
            s.add(Job(source="test", source_id=sid, url=f"https://x.test/{sid}", company=company,
                      title="SDE", dedupe_hash=sid, status=JobStatus.GATED, eligibility=elig,
                      posted_at=posted))
    try:
        with session() as s:
            ids = pick_for_scoring(s)
            by_id = {j.id: j.source_id for j in s.exec(select(Job).where(Job.id.in_(ids))).all()}
        order = [by_id[i] for i in ids if i in by_id]
        assert order == ["pick-zepto", "pick-swiggy", "pick-agency", "pick-us"]
        with session() as s:
            assert len(pick_for_scoring(s, limit=2)) == 2
    finally:
        with session() as s:
            for sid, *_ in rows:
                for j in s.exec(select(Job).where(Job.source_id == sid)).all():
                    s.delete(j)
