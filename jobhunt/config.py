"""Central configuration.

Three layers:
  * :class:`Settings`          - environment/secrets and paths (from ``.env``).
  * ``config/*.yaml``          - shipped defaults for the job-targeting rules.
  * ``<data>/profile/targets.yaml`` - your own overrides of those rules, written
    by the setup page. Deep-merged over the defaults.

Keeping the targeting rules in YAML means retuning the funnel never requires a
code change. Keeping *your* values out of ``config/`` means they are never
committed, and a ``git pull`` never fights with them.
"""

from __future__ import annotations

import copy
import functools
import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"


class Settings(BaseSettings):
    """Environment-backed settings. Secrets live here, never in YAML."""

    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Where everything personal lives: database, profile, resumes, browser
    # session. Relative paths are resolved against the project root.
    jobhunt_data_dir: str = "data"

    # Browser for form filling and sign-in. Blank = Playwright's bundled
    # Chromium, falling back to installed Chrome, then Edge, if it cannot
    # start. Or pin one: "chromium" | "chrome" | "msedge".
    jobhunt_browser_channel: str = ""

    # LLM backend: "claude_cli" (subscription, default) | "anthropic_api"
    jobhunt_llm_backend: str = "claude_cli"
    claude_cli_path: str = ""
    anthropic_api_key: str = ""

    # Gmail / search. Blank means <data>/profile/client_secret.json.
    google_oauth_client_secret_file: str = ""
    google_cse_api_key: str = ""
    google_cse_cx: str = ""

    # Paths
    @property
    def data_dir(self) -> Path:
        p = Path(self.jobhunt_data_dir or "data").expanduser()
        return p if p.is_absolute() else ROOT / p

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobs.db"

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "profile"

    @property
    def profile_path(self) -> Path:
        return self.profile_dir / "profile.yaml"

    @property
    def profile_draft_path(self) -> Path:
        """A parsed-but-unconfirmed profile, while the setup page is open."""
        return self.profile_dir / "profile.draft.yaml"

    @property
    def uploaded_resume_path(self) -> Path:
        return self.profile_dir / "resume.pdf"

    @property
    def local_targets_path(self) -> Path:
        return self.profile_dir / "targets.yaml"

    @property
    def resumes_dir(self) -> Path:
        return self.data_dir / "resumes"

    @property
    def cover_letters_dir(self) -> Path:
        return self.data_dir / "cover_letters"

    @property
    def screenshots_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "playwright-profile"

    @property
    def gmail_token_path(self) -> Path:
        return self.profile_dir / "gmail_token.json"

    @property
    def google_client_secret_path(self) -> Path:
        configured = (self.google_oauth_client_secret_file or "").replace("\\", "/")
        # Older .env files spelled out the default. Treat that as "the default",
        # so it follows JOBHUNT_DATA_DIR instead of pinning the project's data/.
        if configured.strip() in ("", "data/profile/client_secret.json"):
            return self.profile_dir / "client_secret.json"
        if self.google_oauth_client_secret_file:
            p = Path(self.google_oauth_client_secret_file).expanduser()
            return p if p.is_absolute() else ROOT / p
        return self.profile_dir / "client_secret.json"


@functools.lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()


ENV_PATH = ROOT / ".env"


def update_env(values: dict[str, str], path: Path | None = None) -> None:
    """Set ``KEY=value`` lines in ``.env``, keeping comments and order.

    Used by the Connections page so the AI engine can be configured without
    opening a text editor. Settings are re-read immediately afterwards.
    """
    path = path or ENV_PATH
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = {k.upper(): v for k, v in values.items()}
    for key, value in pending.items():
        if any(c in value for c in "\r\n"):
            raise ValueError(f"{key} cannot contain a line break")

    def render(key: str, value: str) -> str:
        needs_quotes = any(c in value for c in ' #"\'')
        return f'{key}="{value}"' if needs_quotes and '"' not in value else f"{key}={value}"

    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key.upper() in pending:
            out.append(render(key.upper(), pending.pop(key.upper())))
        else:
            out.append(line)
    out.extend(render(k, v) for k, v in pending.items())

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.replace(path)
    settings.cache_clear()


def profile_exists() -> bool:
    """Whether setup has been completed on this machine."""
    return settings().profile_path.exists()


# --------------------------------------------------------------------------
# YAML config
# --------------------------------------------------------------------------

_lock = threading.Lock()
_cache: dict[str, tuple[tuple[int, ...], dict[str, Any]]] = {}


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """``override`` on top of ``base``. Dicts merge; everything else replaces."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _cached(name: str, files: tuple[Path, ...]) -> dict[str, Any]:
    """Load and merge ``files`` in order, re-reading only when one changed.

    Keyed on modification time rather than cached forever: the dashboard and
    the scheduler are long-lived processes, and a setting saved on the setup
    page has to reach the next run without a restart.
    """
    key = tuple(_mtime(f) for f in files)
    with _lock:
        hit = _cache.get(name)
        if hit is not None and hit[0] == key:
            return hit[1]
        merged: dict[str, Any] = {}
        for f in files:
            merged = deep_merge(merged, _read(f))
        _cache[name] = (key, merged)
        return merged


def load_yaml(name: str) -> dict[str, Any]:
    """Load ``config/<name>.yaml``. Returns ``{}`` if absent."""
    return _cached(name, (CONFIG / f"{name}.yaml",))


def targets() -> dict[str, Any]:
    """Targeting rules: shipped defaults with your local overrides on top."""
    return _cached("targets", (CONFIG / "targets.yaml", settings().local_targets_path))


def local_targets() -> dict[str, Any]:
    """Only your overrides, as written by the setup page."""
    return _read(settings().local_targets_path)


def save_local_targets(overrides: dict[str, Any]) -> None:
    """Merge ``overrides`` into your local targets file.

    Only the keys you have changed are written, so a later improvement to a
    shipped default still reaches you for everything you left alone.
    """
    path = settings().local_targets_path
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = deep_merge(_read(path), overrides)
    tmp = path.with_suffix(".yaml.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(merged, fh, sort_keys=False, allow_unicode=True, width=100)
    tmp.replace(path)
    with _lock:
        _cache.pop("targets", None)


def company_tiers() -> dict[str, Any]:
    return load_yaml("company_tiers")


def known_sponsors() -> dict[str, Any]:
    return load_yaml("known_sponsors")
