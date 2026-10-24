"""Repository hygiene: the two dependency lists agree, and nothing personal ships."""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _names(specs: list[str]) -> dict[str, str]:
    out = {}
    for spec in specs:
        spec = spec.split("#", 1)[0].strip()
        if not spec or spec.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[; ]", spec, maxsplit=1)[0].lower().replace("_", "-")
        out[name] = spec.replace(" ", "")
    return out


def test_requirements_and_pyproject_agree():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = _names(pyproject["project"]["dependencies"])
    listed = _names((ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines())
    assert declared == listed


def test_every_template_the_app_renders_exists():
    templates = ROOT / "jobhunt" / "web" / "templates"
    source = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "jobhunt" / "web").glob("*.py"))
    for name in set(re.findall(r'"([a-z_]+\.html)"', source)):
        assert (templates / name).exists(), name


def test_no_secrets_or_personal_data_are_tracked():
    """What `git ls-files` would publish must not include local state."""
    try:
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return  # not a git checkout (e.g. an sdist) - nothing to check
    forbidden = [
        f for f in tracked
        if f == ".env"
        or (f.startswith("data/") and f != "data/.gitkeep")
        or f.endswith((".db", ".db-wal", ".db-shm"))
        or "client_secret" in f
        or "gmail_token" in f
    ]
    assert forbidden == []
