"""Sending your own resume unchanged.

Asked for directly: "why not use this only, why changing?" - some people want
their own PDF on every application, word for word, instead of a per-job
rewrite. In that mode no resume is rewritten and no AI call is made.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import select
from starlette.testclient import TestClient

from jobhunt.config import save_local_targets, settings
from jobhunt.resume.render import original_fingerprint, resume_staleness

PDF = b"%PDF-1.4 my own resume"


@pytest.fixture
def own_resume(local_targets_file):
    path = settings().uploaded_resume_path
    before = path.read_bytes() if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PDF)
    yield path
    if before is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(before)


def _mode(mode: str) -> None:
    save_local_targets({"resume": {"mode": mode}})


def test_in_original_mode_a_tailored_resume_is_superseded(own_resume, tmp_path):
    _mode("original")
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(b"%PDF")
    assert "your own resume" in resume_staleness(pdf, {"layout_fingerprint": "x"}, None)


def test_an_unchanged_copy_of_your_pdf_is_current(own_resume, tmp_path):
    _mode("original")
    pdf = tmp_path / "copy.pdf"
    pdf.write_bytes(PDF)
    detail = {"kind": "original", "original_sha": original_fingerprint()}
    assert resume_staleness(pdf, detail, None) == ""


def test_uploading_a_newer_pdf_supersedes_the_copies(own_resume, tmp_path):
    _mode("original")
    pdf = tmp_path / "copy.pdf"
    pdf.write_bytes(PDF)
    detail = {"kind": "original", "original_sha": original_fingerprint()}
    own_resume.write_bytes(PDF + b" v2")
    assert "newer resume" in resume_staleness(pdf, detail, None)


def test_switching_back_to_tailored_supersedes_the_copies(own_resume, tmp_path):
    _mode("tailored")
    pdf = tmp_path / "copy.pdf"
    pdf.write_bytes(PDF)
    detail = {"kind": "original", "original_sha": original_fingerprint()}
    assert "tailored" in resume_staleness(pdf, detail, None)


@pytest.fixture
def ready_job():
    from jobhunt.store.db import init_db, session
    from jobhunt.store.models import Job, JobStatus, Resume

    init_db()

    def purge():
        with session() as s:
            ids = [j.id for j in s.exec(select(Job).where(Job.source_id == "own-fixture")).all()]
            for r in s.exec(select(Resume).where(Resume.job_id.in_(ids))).all():
                s.delete(r)
            s.flush()
            for i in ids:
                s.delete(s.get(Job, i))

    purge()
    with session() as s:
        job = Job(source="test", source_id="own-fixture", url="https://example.test/o",
                  company="Ownco", title="Software Engineer", dedupe_hash="own-fixture",
                  status=JobStatus.SCORED, score=4.6, description="Python and React.")
        s.add(job)
        s.flush()
        job_id = job.id
    yield job_id
    purge()


def test_original_mode_attaches_your_pdf_without_any_ai(own_resume, ready_job, monkeypatch):
    import jobhunt.agents.tailor as tailor
    from jobhunt.store.db import session
    from jobhunt.store.models import Job, JobStatus, Resume

    _mode("original")

    def no_ai(*_a, **_k):
        raise AssertionError("tailoring must not run in original mode")

    monkeypatch.setattr(tailor, "tailor_one", no_ai)
    stats, results = tailor.run(limit=1, job_id=ready_job)
    assert stats.succeeded == 1 and results[0].ok

    with session() as s:
        resume = s.exec(select(Resume).where(Resume.job_id == ready_job)).one()
        job = s.get(Job, ready_job)
    assert Path(resume.pdf_path).read_bytes() == PDF, "sent exactly as uploaded"
    assert resume.pdf_path.endswith("_Resume.pdf")
    assert resume.ats_detail["kind"] == "original"
    assert job.status is JobStatus.READY
    assert resume_staleness(resume.pdf_path, resume.ats_detail, None) == ""


def test_original_mode_without_a_pdf_says_what_to_do(local_targets_file, ready_job):
    import jobhunt.agents.tailor as tailor

    path = settings().uploaded_resume_path
    saved = path.read_bytes() if path.exists() else None
    path.unlink(missing_ok=True)
    try:
        _mode("original")
        _, results = tailor.run(limit=1, job_id=ready_job)
        assert not results[0].ok
        assert "Filters page" in results[0].error
    finally:
        if saved is not None:
            path.write_bytes(saved)


# --------------------------------------------------------------------------
# The Filters page
# --------------------------------------------------------------------------


@pytest.fixture
def client():
    from jobhunt.web.app import app

    return TestClient(app)


def test_choosing_original_without_a_pdf_is_refused(client, local_targets_file):
    path = settings().uploaded_resume_path
    saved = path.read_bytes() if path.exists() else None
    path.unlink(missing_ok=True)
    try:
        r = client.post("/filters", data={"t_resume_mode": "original", "t_direct_titles": "x"})
        assert r.status_code == 400
        assert "Upload your resume PDF" in r.text
    finally:
        if saved is not None:
            path.write_bytes(saved)


def test_uploading_and_choosing_your_own_resume(client, local_targets_file, monkeypatch):
    from jobhunt.config import targets
    from jobhunt.graph.runner import runner

    started: list[str] = []
    monkeypatch.setattr(runner, "start_or_queue", lambda which, why: started.append(which) or (True, ""))
    path = settings().uploaded_resume_path
    saved = path.read_bytes() if path.exists() else None
    try:
        r = client.post("/filters/resume", files={"resume": ("mine.pdf", PDF, "application/pdf")},
                        follow_redirects=False)
        assert r.status_code == 303 and path.read_bytes() == PDF
        assert client.get("/filters/resume.pdf").content == PDF

        _mode("tailored")
        client.post("/filters", data={"t_resume_mode": "original", "t_direct_titles": "x"})
        assert targets()["resume"]["mode"] == "original"
        assert "refresh" in started, "switching to your own PDF re-attaches it at once"
    finally:
        if saved is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(saved)


def test_a_non_pdf_upload_is_refused(client):
    r = client.post("/filters/resume", files={"resume": ("x.pdf", b"hello", "application/pdf")},
                    follow_redirects=False)
    assert "PDF" in r.headers["location"]
