"""Persistent logged-in browser session.

The agents never handle a password. You log in once, by hand, in a visible
window; Playwright keeps the cookies in a profile directory and every later run
reuses them headlessly.

That is not only a safety property - it is also what keeps the automation
within what a normal person's browser does. The rate limits below exist for the
same reason: LinkedIn's User Agreement prohibits automated access, and the
realistic failure mode is a restricted account, which would cost far more than
any referral is worth.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jobhunt.config import settings, targets

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page

log = logging.getLogger(__name__)

LOGIN_CHECKS = {
    "linkedin": ("https://www.linkedin.com/feed/", "linkedin.com/feed"),
    "glassdoor": ("https://www.glassdoor.com/member/home/index.htm", "member/home"),
}

# Deliberately slow and jittered. This is a human-paced assistant, not a
# crawler; the throughput cap is the point, not a limitation.
MIN_ACTION_DELAY = 2.5
MAX_ACTION_DELAY = 6.0


class SessionError(RuntimeError):
    """The browser profile is not logged in, or the page did not load."""


# The browser that last launched successfully in this process. Once the bundled
# Chromium has failed to start, there is no point paying for the failure again.
_working_channel: str | None = None


def _channels() -> list[str | None]:
    """Browsers to try, in order. None means Playwright's bundled Chromium.

    The bundled build is unsigned, and some Windows machines refuse to start
    it (a side-by-side manifest error, or application control policy). The
    Chrome or Edge already installed is signed and almost always present, so
    it is the fallback - with its own separate profile, never your everyday one.
    """
    pinned = (settings().jobhunt_browser_channel or "").strip().lower()
    if pinned:
        return [None if pinned == "chromium" else pinned]
    order: list[str | None] = [None, "chrome", "msedge"]
    if _working_channel in order:
        order.remove(_working_channel)
        order.insert(0, _working_channel)
    return order


def launch_with_fallback(launch: Any, **kwargs: Any) -> Any:
    """Call a Playwright launch method, trying each available browser in turn."""
    global _working_channel

    last: Exception | None = None
    for channel in _channels():
        try:
            result = launch(**kwargs, **({"channel": channel} if channel else {}))
        except Exception as exc:  # noqa: BLE001 - try the next browser
            last = exc
            log.warning(
                "browser %s failed to start: %s",
                channel or "bundled chromium",
                str(exc).splitlines()[0][:160],
            )
            continue
        if channel != _working_channel:
            if channel:
                log.warning("using the installed %s browser", channel)
            _working_channel = channel
        return result
    assert last is not None
    raise last


BROWSER_BUSY = (
    "the browser is busy - a LinkedIn sign-in window, the referral step or the "
    "apply step has it open. Try again in a few minutes."
)


def _profile_lock():
    from jobhunt.util.lock import FileLock

    return FileLock(settings().data_dir / ".browser.lock")


def browser_in_use() -> bool:
    """Whether something (here or in another process) has the profile open.

    Only one browser may use the saved profile at a time. This lock covers
    exactly the time a browser is open - not a whole run - so signing in to
    LinkedIn works while a run is only downloading job lists.
    """
    lock = _profile_lock()
    if lock.acquire("probe"):
        lock.release()
        return False
    return True


@dataclass
class BrowserSession:
    """A Playwright context backed by the persistent profile directory."""

    headless: bool = True
    context: Any = None
    _playwright: Any = None
    _lock: Any = None

    def start(self) -> BrowserSession:
        from playwright.sync_api import sync_playwright

        profile_dir = settings().browser_profile_dir
        profile_dir.mkdir(parents=True, exist_ok=True)

        lock = _profile_lock()
        if not lock.acquire("browser"):
            raise SessionError(BROWSER_BUSY)
        self._lock = lock

        try:
            self._playwright = sync_playwright().start()
        except Exception:
            self._release()
            raise
        try:
            self.context = launch_with_fallback(
                self._playwright.chromium.launch_persistent_context,
                user_data_dir=str(profile_dir),
                headless=self.headless,
                viewport={"width": 1360, "height": 900},
                locale="en-IN",
                timezone_id=targets().get("schedule", {}).get("timezone", "Asia/Kolkata"),
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            self._playwright.stop()
            self._playwright = None
            self._release()
            raise
        return self

    def _release(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()

    def close(self) -> None:
        try:
            if self.context:
                self.context.close()
        finally:
            try:
                if self._playwright:
                    self._playwright.stop()
            finally:
                self._release()

    def page(self) -> Page:
        if self.context is None:
            raise SessionError("session not started")
        return self.context.pages[0] if self.context.pages else self.context.new_page()

    def pause(self) -> None:
        """Human-paced delay between actions."""
        time.sleep(random.uniform(MIN_ACTION_DELAY, MAX_ACTION_DELAY))


@contextmanager
def browser(headless: bool = True) -> Iterator[BrowserSession]:
    session = BrowserSession(headless=headless).start()
    try:
        yield session
    finally:
        session.close()


def is_logged_in(site: str = "linkedin", *, headless: bool = True) -> tuple[bool, str]:
    """Check whether the stored profile still has a live session."""
    if site not in LOGIN_CHECKS:
        return False, f"unknown site {site!r}"
    url, marker = LOGIN_CHECKS[site]

    try:
        with browser(headless=headless) as session:
            page = session.page()
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            landed = page.url
            if marker in landed:
                return True, f"logged in ({landed[:60]})"
            if "login" in landed or "signup" in landed or "authwall" in landed:
                return False, "not signed in - use Sign in to LinkedIn on the Connections page"
            return False, f"unexpected page: {landed[:80]}"
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"


LOGIN_PAGES = {
    "linkedin": "https://www.linkedin.com/login",
    "glassdoor": "https://www.glassdoor.com/profile/login_input.htm",
}


def wait_for_login(
    site: str = "linkedin",
    *,
    timeout_s: int = 600,
    cancel: threading.Event | None = None,
) -> tuple[bool, str]:
    """Open a visible window at the sign-in page and wait for you to finish.

    The dashboard's version of :func:`interactive_login`: there is no terminal
    to press Enter in, so this watches the window instead. It returns once any
    tab reaches the signed-in page, the window is closed, the timeout passes,
    or ``cancel`` is set. Nothing is typed or read - you sign in yourself.
    """
    if site not in LOGIN_CHECKS:
        return False, f"unknown site {site!r}"
    home, marker = LOGIN_CHECKS[site]

    with browser(headless=False) as session:
        page = session.page()
        try:
            page.goto(home, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:  # noqa: BLE001 - reported to the page
            return False, f"could not open {site}: {str(exc).splitlines()[0][:120]}"
        if marker in page.url:
            return True, "already signed in"
        if "login" not in page.url and site in LOGIN_PAGES:
            try:
                page.goto(LOGIN_PAGES[site], wait_until="domcontentloaded", timeout=60_000)
            except Exception:  # noqa: BLE001 - the user can navigate themselves
                pass

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                return False, "cancelled"
            try:
                pages = session.context.pages
            except Exception:  # noqa: BLE001 - context gone: window closed
                pages = []
            if not pages:
                return False, "the window was closed before sign-in finished"
            if any(marker in p.url for p in pages):
                # Give the site a moment to finish writing its cookies.
                time.sleep(3)
                return True, "signed in"
            time.sleep(1.5)
        return False, f"timed out after {timeout_s // 60} minutes"


def interactive_login(site: str = "linkedin") -> bool:
    """Open a visible browser so the user can sign in themselves.

    Blocks until they confirm. Nothing here reads or stores credentials - the
    only artefact is the cookie jar Chromium writes into the profile directory.
    """
    url = LOGIN_CHECKS.get(site, ("https://www.linkedin.com/login", ""))[0]
    with browser(headless=False) as session:
        page = session.page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        print(
            f"\nA browser window is open at {site}.\n"
            "Sign in there yourself - nothing is typed or stored by this program.\n"
            "Leave the window open until you are fully signed in, then come back here."
        )
        input("Press Enter once you are signed in... ")
        try:
            page.goto(
                LOGIN_CHECKS[site][0], wait_until="domcontentloaded", timeout=45_000
            )
            ok = LOGIN_CHECKS[site][1] in page.url
        except Exception:  # noqa: BLE001
            ok = False
    return ok
