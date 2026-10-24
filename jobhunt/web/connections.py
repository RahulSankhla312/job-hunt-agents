"""Account connections, driven from the dashboard.

Three things the agents need that are not part of your profile:

* **AI engine** - the Claude Code CLI on your subscription, or an Anthropic API
  key. Everything that reads or writes text depends on it.
* **LinkedIn** - a signed-in browser session, used read-only to find people
  who could refer you. Optional.
* **Gmail** - to send approved outreach and read replies. Optional.

Each check or sign-in can take from seconds to minutes (a browser window you
are typing into, a Google consent screen), so each runs on a background
thread and the page polls. The last known result is kept on disk so a restart
does not forget that LinkedIn was connected.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from jobhunt.config import settings

log = logging.getLogger(__name__)

KEYS = ("ai", "linkedin", "gmail")


def _tidy(message: str | None) -> str:
    """One readable status line. Playwright appends a 40-line launch log."""
    text = (message or "").split("\nCall log:", 1)[0]
    return " ".join(text.split())[:300]


@dataclass
class Status:
    ok: bool | None = None          # None = never checked
    message: str = ""
    checked_at: str | None = None   # ISO timestamp, UTC
    running: bool = False
    action: str = ""                # what is running: "check" | "sign-in" | "connect"


class Connections:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status: dict[str, Status] = {}
        self._cancel = threading.Event()
        self._loaded_from: str | None = None

    # ---- persistence -----------------------------------------------------

    def _path(self):
        return settings().profile_dir / "connections.json"

    def _ensure_loaded(self) -> None:
        path = self._path()
        if self._loaded_from == str(path):
            return
        self._status = {k: Status() for k in KEYS}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for key in KEYS:
                if isinstance(data.get(key), dict):
                    saved = {k: v for k, v in data[key].items() if k in Status.__annotations__}
                    saved["message"] = _tidy(saved.get("message"))
                    self._status[key] = Status(**{**saved, "running": False, "action": ""})
        except (OSError, ValueError, TypeError):
            pass
        self._loaded_from = str(path)

    def _save(self) -> None:
        path = self._path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {k: {**asdict(v), "running": False} for k, v in self._status.items()}
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("could not save connection status: %s", exc)

    # ---- state -----------------------------------------------------------

    def get(self, key: str) -> Status:
        with self._lock:
            self._ensure_loaded()
            return Status(**asdict(self._status[key]))

    def snapshot(self) -> dict[str, Status]:
        return {k: self.get(k) for k in KEYS}

    def busy(self) -> bool:
        return any(s.running for s in self.snapshot().values())

    def _start(
        self, key: str, action: str, fn: Callable[[], tuple[bool | None, str]]
    ) -> tuple[bool, str]:
        with self._lock:
            self._ensure_loaded()
            if self._status[key].running:
                return False, "already in progress"
            self._status[key].running = True
            self._status[key].action = action

        def work() -> None:
            try:
                ok, message = fn()
            except Exception as exc:  # noqa: BLE001 - shown on the page, never raised
                log.exception("%s %s failed", key, action)
                ok, message = False, f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._ensure_loaded()
                st = self._status[key]
                st.running, st.action = False, ""
                if ok is not None:
                    st.ok = ok
                st.message = _tidy(message)
                st.checked_at = datetime.now(UTC).isoformat(timespec="seconds")
                self._save()

        threading.Thread(target=work, name=f"connect-{key}", daemon=True).start()
        return True, "started"

    # ---- AI engine -------------------------------------------------------

    def check_ai(self) -> tuple[bool, str]:
        def run() -> tuple[bool, str]:
            from jobhunt.llm.adapter import get_backend

            backend = get_backend()
            ok, detail = backend.healthcheck()
            return ok, f"[{backend.name}] {detail}"

        return self._start("ai", "check", run)

    # ---- LinkedIn --------------------------------------------------------

    def _with_browser_lock(self, label: str, fn: Callable[[], tuple[bool, str]]):
        """Only one browser may use the saved profile at a time.

        The profile has its own lock, held only while a browser is actually
        open - so a run that is just downloading job lists does not block
        signing in.
        """
        from jobhunt.sources.browser.session import BROWSER_BUSY, SessionError, browser_in_use

        if browser_in_use():
            return None, BROWSER_BUSY
        try:
            return fn()
        except SessionError as exc:
            return None, str(exc)

    def check_linkedin(self) -> tuple[bool, str]:
        from jobhunt.sources.browser.session import is_logged_in

        return self._start(
            "linkedin", "check",
            lambda: self._with_browser_lock("linkedin-check", lambda: is_logged_in("linkedin")),
        )

    def sign_in_linkedin(self, timeout_s: int = 600) -> tuple[bool, str]:
        from jobhunt.sources.browser.session import wait_for_login

        self._cancel.clear()
        return self._start(
            "linkedin", "sign-in",
            lambda: self._with_browser_lock(
                "linkedin-login",
                lambda: wait_for_login("linkedin", timeout_s=timeout_s, cancel=self._cancel),
            ),
        )

    def cancel_sign_in(self) -> None:
        self._cancel.set()

    # ---- Gmail -----------------------------------------------------------

    def connect_gmail(self) -> tuple[bool, str]:
        def run() -> tuple[bool, str]:
            from jobhunt.sources.gmail import GmailUnavailable, _client, token_path

            try:
                _client()
            except GmailUnavailable as exc:
                return False, str(exc)
            return True, f"connected - token saved to {token_path()}"

        return self._start("gmail", "connect", run)

    def disconnect_gmail(self) -> None:
        from jobhunt.sources.gmail import token_path

        token_path().unlink(missing_ok=True)
        with self._lock:
            self._ensure_loaded()
            self._status["gmail"] = Status(ok=False, message="disconnected")
            self._save()


def ai_summary() -> dict[str, Any]:
    """What the AI engine is configured as, without calling it."""
    from jobhunt.llm.adapter import discover_claude_cli

    s = settings()
    backend = (s.jobhunt_llm_backend or "claude_cli").strip()
    cli = discover_claude_cli()
    return {
        "backend": backend,
        "cli_path": str(cli) if cli else "",
        "api_key_set": bool(s.anthropic_api_key),
        "configured": bool(cli) if backend == "claude_cli" else bool(s.anthropic_api_key),
    }


def validate_client_secret(raw: bytes) -> str | None:
    """None if this is a Google *Desktop app* OAuth client JSON, else why not."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "that file is not JSON"
    if not isinstance(data, dict):
        return "that file is not an OAuth client"
    if "installed" not in data:
        kind = next(iter(data), "unknown")
        return (
            f"that is a '{kind}' OAuth client - create a 'Desktop app' client "
            "in Google Cloud Console instead"
        )
    if not data["installed"].get("client_id"):
        return "the file has no client_id"
    return None


connections = Connections()
