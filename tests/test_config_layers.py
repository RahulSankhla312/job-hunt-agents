"""Configuration layering, the .env writer, and the run lock."""

from __future__ import annotations

import os
import time

import pytest

from jobhunt.config import (
    deep_merge,
    local_targets,
    save_local_targets,
    settings,
    targets,
    update_env,
)
from jobhunt.resume.profile import ApplicationAnswers
from jobhunt.util.lock import FileLock, LockHeld


def test_deep_merge_merges_dicts_and_replaces_lists():
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 1}
    out = deep_merge(base, {"a": {"y": [3]}, "c": 2})
    assert out == {"a": {"x": 1, "y": [3]}, "b": 1, "c": 2}
    assert base["a"]["y"] == [1, 2], "the input must not be mutated"


def test_the_data_dir_follows_the_environment(data_dir):
    assert settings().data_dir == data_dir
    assert settings().db_path.parent == data_dir


def test_local_overrides_layer_over_the_shipped_defaults(local_targets_file):
    shipped = targets()["compensation"]["us_remote_min_usd"]
    save_local_targets({"compensation": {"india_min_ctc_lpa": 12.5}})
    cfg = targets()
    assert cfg["compensation"]["india_min_ctc_lpa"] == 12.5
    assert cfg["compensation"]["us_remote_min_usd"] == shipped, "untouched keys keep defaults"
    assert local_targets() == {"compensation": {"india_min_ctc_lpa": 12.5}}


def test_a_saved_change_reaches_a_long_running_process(local_targets_file):
    """The dashboard and scheduler never restart; targets() must notice edits."""
    save_local_targets({"scoring": {"apply_threshold": 3.1}})
    assert targets()["scoring"]["apply_threshold"] == 3.1
    time.sleep(0.02)
    save_local_targets({"scoring": {"apply_threshold": 4.2}})
    assert targets()["scoring"]["apply_threshold"] == 4.2


def test_update_env_replaces_keys_and_keeps_everything_else(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nJOBHUNT_LLM_BACKEND=claude_cli\nOTHER=1\n", encoding="utf-8")
    update_env({"JOBHUNT_LLM_BACKEND": "anthropic_api", "ANTHROPIC_API_KEY": "k-123"}, env)
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "# comment",
        "JOBHUNT_LLM_BACKEND=anthropic_api",
        "OTHER=1",
        "ANTHROPIC_API_KEY=k-123",
    ]


def test_update_env_refuses_a_line_break(tmp_path):
    with pytest.raises(ValueError):
        update_env({"X": "a\nB=evil"}, tmp_path / ".env")


# --------------------------------------------------------------------------
# Citizenship is judged against the stated country
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("citizenship", "location", "expected"),
    [
        ("India", "Bengaluru, India", "Yes"),
        ("India", "San Francisco, CA", "No"),
        ("Canada", "Toronto, Canada", "Yes"),
        ("Canada", "Bengaluru, India", "No"),
        ("", "Bengaluru, India", "No"),
    ],
)
def test_citizenship_answer(citizenship, location, expected):
    assert ApplicationAnswers(citizenship=citizenship).citizenship_answer(location) == expected


# --------------------------------------------------------------------------
# The run lock
# --------------------------------------------------------------------------


def test_the_lock_excludes_a_second_holder(tmp_path):
    path = tmp_path / "run.lock"
    first, second = FileLock(path), FileLock(path)
    assert first.acquire("one")
    try:
        assert not second.acquire("two"), "two runs at once must be impossible"
    finally:
        first.release()
    assert second.acquire("two"), "a released lock must be takeable again"
    second.release()


def test_the_lock_context_manager_raises_when_held(tmp_path):
    path = tmp_path / "run.lock"
    with FileLock(path), pytest.raises(LockHeld):
        FileLock(path).__enter__()


def test_pipeline_runs_are_exclusive():
    from jobhunt.graph.pipeline import RunInProgress, exclusive

    with exclusive("test"), pytest.raises(RunInProgress), exclusive("second"):
        pass


def test_a_run_without_a_profile_says_so(no_profile):
    from jobhunt.graph.pipeline import ProfileMissing, morning

    with pytest.raises(ProfileMissing):
        morning()


def test_log_setup_writes_a_file(data_dir):
    import logging

    from jobhunt.logs import configure

    path = configure("test", logging.WARNING)
    logging.getLogger("jobhunt.test").info("hello from the test")
    for h in logging.getLogger().handlers:
        h.flush()
    assert path.parent == data_dir / "logs"
    assert "hello from the test" in path.read_text(encoding="utf-8")
    assert os.path.getsize(path) > 0
