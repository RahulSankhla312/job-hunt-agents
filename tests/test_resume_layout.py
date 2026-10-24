"""Old-layout resumes must never be offered or sent as if current.

Found in use: /resume/5 served a resume from before the Overleaf layout was
restored (SUMMARY section, split skills, "EXPERIENCE"), and the dashboard's
Apply and Submit buttons attached the newest resume without checking it.
Meanwhile the timestamp-based check marked every resume stale whenever a
comment in the template was edited.
"""

from __future__ import annotations

import pytest
from sqlmodel import select
from starlette.testclient import TestClient

import jobhunt.resume.render as render
from jobhunt.resume.render import (
    OLD_LAYOUT,
    is_stale,
    layout_fingerprint,
    layout_matches,
    resume_staleness,
)

# Heading lines as pdfminer extracts them from the two layouts.
OLD = """Aarav Mehta
SUMMARY
Software Engineer at Northwind building internal developer tooling.
SKILLS
Languages: Python, TypeScript
EXPERIENCE
Software Engineer, Northwind
"""
CURRENT = """Aarav Mehta
EDUCATION
Indian Institute of Information Technology, Pune
SKILLS
2021 - 2025
Technical Skills : Python, Java
WORK EXPERIENCE
Software Engineer
ACHIEVEMENTS
Solved 750+ problems.
PROJECTS
AI-ASSISTED ENGINEERING AGENT PLATFORM
"""


def test_the_old_layout_is_recognised():
    assert layout_matches(OLD) is False


def test_the_current_layout_is_recognised():
    assert layout_matches(CURRENT) is True


def test_optional_sections_may_be_absent():
    no_projects = CURRENT.split("PROJECTS")[0]
    assert layout_matches(no_projects) is True


def test_sections_out_of_order_are_not_the_current_layout():
    swapped = CURRENT.replace("EDUCATION", "@@").replace("WORK EXPERIENCE", "EDUCATION")
    swapped = swapped.replace("@@", "WORK EXPERIENCE")
    assert layout_matches(swapped) is False


def test_editing_a_template_comment_changes_nothing(tmp_path, monkeypatch):
    """Comments render nothing, so they must not make every resume stale."""
    template = tmp_path / "template.html"
    template.write_text("<!-- by A -->\n<h2>Education</h2>{# note #}<style>/* x */p{}</style>",
                        encoding="utf-8")
    monkeypatch.setattr(render, "TEMPLATE_PATH", template)
    before = layout_fingerprint()
    template.write_text("<!-- by B, edited -->\n<h2>Education</h2>{# other #}<style>/* y */p{}</style>",
                        encoding="utf-8")
    assert layout_fingerprint() == before
    template.write_text("<h2>Summary</h2><h2>Education</h2>", encoding="utf-8")
    assert layout_fingerprint() != before


def test_a_recorded_layout_is_compared_by_content(tmp_path):
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    assert is_stale(pdf, layout=layout_fingerprint()) == ""
    assert "layout changed" in is_stale(pdf, layout="0000000000000000")


def test_a_row_judged_outdated_stays_outdated(tmp_path):
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    assert resume_staleness(pdf, {"layout_outdated": True}, None) == OLD_LAYOUT


# --------------------------------------------------------------------------
# The dashboard rewrites an outdated resume before any apply
# --------------------------------------------------------------------------


@pytest.fixture
def stale_job(tmp_path):
    from jobhunt.store.db import init_db, session
    from jobhunt.store.models import Job, JobStatus, Resume

    init_db()
    pdf = tmp_path / "old.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    def purge():
        with session() as s:
            ids = [j.id for j in s.exec(select(Job).where(Job.source_id == "stale-fixture")).all()]
            for r in s.exec(select(Resume).where(Resume.job_id.in_(ids))).all():
                s.delete(r)
            s.flush()
            for i in ids:
                s.delete(s.get(Job, i))

    purge()
    with session() as s:
        job = Job(source="test", source_id="stale-fixture", url="https://example.test/s",
                  company="Staleco", title="Software Engineer", dedupe_hash="stale-fixture",
                  status=JobStatus.READY, score=4.5)
        s.add(job)
        s.flush()
        s.add(Resume(job_id=job.id, pdf_path=str(pdf), ats_detail={"layout_outdated": True}))
        job_id = job.id
    yield job_id
    purge()


@pytest.mark.parametrize("route", ["apply", "submit"])
def test_apply_buttons_rewrite_an_outdated_resume_first(stale_job, monkeypatch, route):
    import jobhunt.agents.tailor as tailor
    import jobhunt.web.app as web

    order: list[str] = []

    class Ok:
        ok = True
        error = ""

    monkeypatch.setattr(tailor, "run", lambda **kw: (order.append("tailor") or (None, [Ok()])))
    monkeypatch.setattr(web, "_apply_one", lambda job_id, **kw: order.append("apply"))

    client = TestClient(web.app)
    client.post(f"/job/{stale_job}/{route}", follow_redirects=False)
    assert order == ["tailor", "apply"]


def test_a_failed_rewrite_does_not_apply_with_the_old_resume(stale_job, monkeypatch):
    import jobhunt.agents.tailor as tailor
    import jobhunt.web.app as web

    applied: list[int] = []

    class Failed:
        ok = False
        error = "llm down"

    monkeypatch.setattr(tailor, "run", lambda **kw: (None, [Failed()]))
    monkeypatch.setattr(web, "_apply_one", lambda job_id, **kw: applied.append(job_id))

    TestClient(web.app).post(f"/job/{stale_job}/apply", follow_redirects=False)
    assert applied == []


def test_the_job_page_warns_about_an_outdated_resume(stale_job):
    import jobhunt.web.app as web

    body = TestClient(web.app).get(f"/job/{stale_job}").text
    assert "This resume is out of date" in body
    assert "Rewrite resume now" in body
    assert "Open old resume" in body


def test_outdated_jobs_lists_it(stale_job):
    from jobhunt.agents.tailor import outdated_jobs

    assert stale_job in [job_id for job_id, _ in outdated_jobs()]


def test_backfill_records_a_verdict_once(stale_job, monkeypatch):
    """Legacy rows are judged once and the verdict stored, not re-read per page."""
    import jobhunt.resume.render as render_mod
    from jobhunt.agents.tailor import backfill_layout_records
    from jobhunt.store.db import session
    from jobhunt.store.models import Resume

    with session() as s:
        row = s.exec(select(Resume).where(Resume.job_id == stale_job)).one()
        row.ats_detail = {}
        s.add(row)

    reads: list[str] = []

    def judged_current(path):
        reads.append(str(path))
        return True

    monkeypatch.setattr(render_mod, "pdf_follows_layout", judged_current)
    backfill_layout_records()
    backfill_layout_records()
    with session() as s:
        detail = s.exec(select(Resume).where(Resume.job_id == stale_job)).one().ats_detail
    assert detail["layout_fingerprint"] == layout_fingerprint()
    assert len([r for r in reads if "old.pdf" in r]) == 1
