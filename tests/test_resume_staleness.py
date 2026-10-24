"""A saved resume is not automatically a current one.

A Resume row is kept and reused for the life of a job. That meant a PDF
rendered under the old layout stayed attached, and a Render application was
submitted in the superseded template - SUMMARY section, four skills lines,
wrong section order - hours after the layout had been corrected. Nothing in the
pipeline noticed, because a resume existed.
"""

from __future__ import annotations

import os
import time

import pytest

from jobhunt.resume.render import TEMPLATE_PATH, is_stale


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "resume.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    return p


def _touch(path, when):
    os.utime(path, (when, when))


def test_a_resume_written_after_the_template_is_current(pdf):
    _touch(pdf, time.time() + 60)
    assert is_stale(pdf) == ""


def test_a_resume_older_than_the_template_is_stale(pdf):
    _touch(pdf, TEMPLATE_PATH.stat().st_mtime - 60)
    assert "layout changed" in is_stale(pdf)


def test_a_changed_profile_makes_it_stale(pdf):
    """Compared by content, so only edits that reach the page count."""
    from jobhunt.config import settings
    from jobhunt.resume.profile import Profile

    _touch(pdf, time.time() + 60)
    profile = Profile.load(settings().profile_path)
    assert "profile changed" in is_stale(pdf, profile=profile, fingerprint="stale00000000000")


def test_an_unchanged_profile_is_not_stale(pdf):
    from jobhunt.config import settings
    from jobhunt.resume.profile import Profile
    from jobhunt.resume.render import profile_fingerprint

    _touch(pdf, time.time() + 60)
    profile = Profile.load(settings().profile_path)
    assert is_stale(pdf, profile=profile, fingerprint=profile_fingerprint(profile)) == ""


def test_an_answer_only_edit_does_not_trigger_a_re_render(pdf):
    """Sponsorship, notice period and citizenship never appear on the page.

    Keying this to the file's timestamp marked all sixteen resumes stale the
    moment `citizenship: India` was saved, and each false positive is an LLM
    call.
    """
    from jobhunt.config import settings
    from jobhunt.resume.profile import Profile
    from jobhunt.resume.render import profile_fingerprint

    profile = Profile.load(settings().profile_path)
    before = profile_fingerprint(profile)
    profile.answers.citizenship = "Somewhere else"
    profile.answers.notice_period_days = 999
    assert profile_fingerprint(profile) == before


def test_a_missing_pdf_is_stale(tmp_path):
    """The row survives the file; a deleted PDF must be re-rendered, not sent."""
    assert is_stale(tmp_path / "gone.pdf") == "the PDF is missing"


def test_a_row_with_no_fingerprint_is_judged_on_layout_alone(pdf):
    """Rows written before this check exists have nothing to compare."""
    from jobhunt.config import settings
    from jobhunt.resume.profile import Profile

    _touch(pdf, time.time() + 60)
    assert is_stale(pdf, profile=Profile.load(settings().profile_path)) == ""


def test_the_reason_is_returned_for_the_log(pdf):
    """Callers print this, so it has to read as a sentence."""
    _touch(pdf, TEMPLATE_PATH.stat().st_mtime - 60)
    reason = is_stale(pdf)
    assert reason.endswith("since this was written")
