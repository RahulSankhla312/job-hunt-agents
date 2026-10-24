"""When an application is actually submitted.

Every condition below is one that has to hold before the agent clicks Submit.
The point of the test is that each is independently load-bearing: a regression
that quietly drops one would start sending applications you never approved,
and that is not a failure you can take back.
"""

from __future__ import annotations

import pytest

from jobhunt.agents import applicant


@pytest.fixture
def autonomy(monkeypatch):
    """Set the autonomy block for one test."""
    def _set(**overrides):
        cfg = {
            "shadow_mode": False,
            "auto_submit_clean_ats": True,
            "auto_submit_with_essays": False,
            "auto_submit_workday": False,
            "auto_submit_unknown_ats": False,
        }
        cfg.update(overrides)
        monkeypatch.setattr(applicant, "targets", lambda: {"autonomy": cfg})

    return _set


def test_shadow_mode_blocks_everything(autonomy):
    """The master switch, and the reason nothing has been sent so far."""
    autonomy(shadow_mode=True)
    allowed, reason = applicant.may_autosubmit("ashby", [], [])
    assert not allowed
    assert "shadow mode" in reason


def test_a_clean_form_on_a_known_ats_goes(autonomy):
    autonomy()
    allowed, reason = applicant.may_autosubmit("ashby", [], [])
    assert allowed
    assert reason == "clean form on a known ATS"


@pytest.mark.parametrize("ats", ["greenhouse", "ashby", "lever"])
def test_the_three_known_ats(autonomy, ats):
    autonomy()
    assert applicant.may_autosubmit(ats, [], [])[0]


def test_an_unanswered_essay_stops_it(autonomy):
    autonomy()
    allowed, reason = applicant.may_autosubmit("ashby", ["Why this company?"], [])
    assert not allowed
    assert "free-text" in reason


def test_a_required_field_left_empty_stops_it(autonomy):
    """Fails closed: an unfilled required field means an incomplete application."""
    autonomy()
    allowed, reason = applicant.may_autosubmit("ashby", [], ["Location"])
    assert not allowed
    assert "required field" in reason


def test_workday_is_off_by_default(autonomy):
    autonomy()
    allowed, reason = applicant.may_autosubmit("workday", [], [])
    assert not allowed
    assert "multi-step" in reason


def test_an_unknown_ats_is_off_by_default(autonomy):
    autonomy()
    allowed, reason = applicant.may_autosubmit("smartrecruiters", [], [])
    assert not allowed
    assert "unrecognised" in reason


def test_shadow_mode_wins_over_every_other_permission(autonomy):
    """Even with everything else opened up, the master switch holds."""
    autonomy(
        shadow_mode=True,
        auto_submit_with_essays=True,
        auto_submit_workday=True,
        auto_submit_unknown_ats=True,
    )
    for ats in ("ashby", "workday", "whatever"):
        assert not applicant.may_autosubmit(ats, ["essay"], ["field"])[0]


def test_the_default_when_config_is_missing_is_to_not_send(monkeypatch):
    """An empty or broken config must never mean "send everything"."""
    monkeypatch.setattr(applicant, "targets", lambda: {})
    assert not applicant.may_autosubmit("ashby", [], [])[0]


# --------------------------------------------------------------------------
# The one-click button
# --------------------------------------------------------------------------


def test_an_explicit_click_stands_in_for_shadow_mode(autonomy):
    """Shadow mode stops unattended runs, not a deliberate choice.

    Clicking Submit on one posting is a narrower and stronger signal of intent
    than flipping a global switch, so it is accepted in its place.
    """
    autonomy(shadow_mode=True)
    assert not applicant.may_autosubmit("ashby", [], [])[0]
    assert applicant.may_autosubmit("ashby", [], [], requested=True)[0]


def test_a_click_does_not_excuse_an_incomplete_form(autonomy):
    """The rest of the gate is about the application being any good."""
    autonomy(shadow_mode=True)
    allowed, reason = applicant.may_autosubmit(
        "ashby", [], ["Preferred Work Location"], requested=True
    )
    assert not allowed
    assert "required field" in reason


def test_a_click_does_not_excuse_an_unanswered_essay(autonomy):
    autonomy(shadow_mode=True)
    allowed, reason = applicant.may_autosubmit(
        "ashby", ["Why this company?"], [], requested=True
    )
    assert not allowed
    assert "free-text" in reason


def test_a_click_does_not_excuse_an_unknown_ats(autonomy):
    autonomy(shadow_mode=True)
    allowed, reason = applicant.may_autosubmit("smartrecruiters", [], [], requested=True)
    assert not allowed
    assert "unrecognised" in reason


# --------------------------------------------------------------------------
# Approve carries through to the submit
# --------------------------------------------------------------------------


def test_approve_submits_by_default():
    """`_advance` is what the Approve button runs, and it now sends.

    Approving is a deliberate click on one posting, so it is consent in the
    same sense the Submit button was. The signature default is the behaviour:
    if this flips back to False, Approve silently stops applying again.
    """
    import inspect

    from jobhunt.web.app import _advance

    assert inspect.signature(_advance).parameters["requested"].default is True


def test_the_scheduler_path_is_not_requested():
    """The unattended run must still obey shadow mode.

    applicant.run() is what the 18:30 job calls; nothing there may pass
    requested=True, or the nightly sweep would start sending on its own.
    """
    import inspect

    from jobhunt.agents import applicant

    source = inspect.getsource(applicant.run)
    assert "requested=True" not in source
