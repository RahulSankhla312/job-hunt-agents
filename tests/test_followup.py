"""Post-interview follow-up drafting.

The highest-leverage email in the funnel and the one most often skipped. The
risk it carries is specific: a model asked to write a thank-you note will
happily invent a conversation that never happened, and sending that is worse
than sending nothing.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from jobhunt.agents.writer import FOLLOWUP_STAGES, follow_up
from jobhunt.resume.profile import Profile
from jobhunt.store.models import Job


@pytest.fixture
def job():
    return Job(
        source="test", source_id="fu-1", url="https://example.com",
        company="Render", title="Software Engineer, Dev Velocity",
        dedupe_hash="fu-1", description="CI/CD tooling for concurrent agents.",
    )


@pytest.fixture
def profile():
    from jobhunt.config import settings

    return Profile.load(settings().profile_path)


def _capture(prompt_box):
    def fake(prompt, **kwargs):
        prompt_box.append({"prompt": prompt, "system": kwargs.get("system", "")})
        return {"note": "Thanks for the time today. I shipped the GitLab CI pipeline."}

    return fake


def test_every_stage_has_its_own_framing():
    """A recruiter screen and a final round do not get the same note."""
    assert set(FOLLOWUP_STAGES) == {"screen", "interview", "onsite", "offer"}
    assert len(set(FOLLOWUP_STAGES.values())) == 4


def test_notes_reach_the_prompt(job, profile):
    box = []
    with patch("jobhunt.agents.writer.complete_json", _capture(box)):
        follow_up(profile, job, "interview", notes="Priya asked about duplicate API calls")
    assert "Priya asked about duplicate API calls" in box[0]["prompt"]


def test_without_notes_it_is_told_not_to_invent_one(job, profile):
    """The whole failure mode in one instruction."""
    box = []
    with patch("jobhunt.agents.writer.complete_json", _capture(box)):
        follow_up(profile, job, "interview")
    assert "do not describe" in box[0]["prompt"]
    assert "No notes" in box[0]["prompt"]


def test_interviewer_name_is_used_when_known(job, profile):
    box = []
    with patch("jobhunt.agents.writer.complete_json", _capture(box)):
        follow_up(profile, job, "screen", interviewer="Dana")
    assert "Address it to Dana" in box[0]["prompt"]


def test_an_unknown_stage_falls_back_rather_than_crashing(job, profile):
    box = []
    with patch("jobhunt.agents.writer.complete_json", _capture(box)):
        result = follow_up(profile, job, "promoted")
    assert result.ok
    assert FOLLOWUP_STAGES["interview"] in box[0]["prompt"]


def test_a_fabricated_claim_is_blocked(job, profile):
    """Same gate as the resume: unverifiable claims do not ship."""
    def invent(prompt, **kwargs):
        return {"note": "Following up - as I mentioned, I led a team of 40 at Google."}

    with patch("jobhunt.agents.writer.complete_json", invent):
        result = follow_up(profile, job, "interview")
    assert not result.ok
    assert result.violations
