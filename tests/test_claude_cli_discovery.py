"""Finding the Claude Code binary wherever the desktop app put it."""

from __future__ import annotations

import os

import pytest

from jobhunt.llm import adapter

NAME = "claude.exe" if os.name == "nt" else "claude"


@pytest.fixture
def no_path_or_setting(monkeypatch, tmp_path):
    monkeypatch.setattr(adapter.shutil, "which", lambda _name: None)
    monkeypatch.setattr(adapter, "settings", lambda: type("S", (), {"claude_cli_path": ""})())
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(adapter.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return tmp_path


def _make(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


def test_per_profile_install_with_build_folders_is_found(no_path_or_setting):
    """Found on a real machine: Claude-Data/<profile>/claude-code/<version>/<build>/."""
    base = no_path_or_setting / "local" / "Claude-Data" / "claude_abc" / "claude-code"
    _make(base / "2.1.304" / "aed62990" / NAME)
    newest = _make(base / "2.1.308" / "c17a819a" / NAME)
    assert adapter.discover_claude_cli() == newest


def test_the_older_flat_layout_still_works(no_path_or_setting):
    exe = _make(no_path_or_setting / "roaming" / "Claude" / "claude-code" / "1.0.9" / NAME)
    assert adapter.discover_claude_cli() == exe


def test_nothing_installed_returns_none(no_path_or_setting):
    assert adapter.discover_claude_cli() is None


def test_an_expired_sign_in_is_reported_as_one(monkeypatch, tmp_path):
    """Found on a real machine: exit 1, empty stderr, the reason only in JSON stdout."""
    import json
    import subprocess

    exe = _make(tmp_path / NAME)
    monkeypatch.setattr(adapter, "discover_claude_cli", lambda: exe)
    out = json.dumps({"is_error": True, "result": "Failed to authenticate: OAuth session expired"})
    monkeypatch.setattr(
        adapter.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout=out, stderr=""),
    )
    with pytest.raises(adapter.LLMAuthError, match="/login"):
        adapter.ClaudeCLIBackend().complete("hi", system=None, model="m", max_tokens=5)


def test_other_failures_carry_the_json_reason(monkeypatch, tmp_path):
    import json
    import subprocess

    exe = _make(tmp_path / NAME)
    monkeypatch.setattr(adapter, "discover_claude_cli", lambda: exe)
    out = json.dumps({"is_error": True, "result": "Model not available"})
    monkeypatch.setattr(
        adapter.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout=out, stderr=""),
    )
    with pytest.raises(adapter.LLMError, match="Model not available"):
        adapter.ClaudeCLIBackend().complete("hi", system=None, model="m", max_tokens=5)
