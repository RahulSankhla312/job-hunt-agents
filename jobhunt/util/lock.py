"""A cross-process lock for pipeline runs.

A run can start from four places: the dashboard's buttons, the dashboard's
built-in scheduler, ``run.py run-now`` and the standalone ``run.py schedule``.
Two runs at once would fill the same application form twice and double-count
the daily caps, so every run takes this lock first.

It is an OS-level lock on an open file handle rather than a "lock file exists"
check: the operating system releases it when the process dies, so a crashed run
can never wedge the next one.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import IO


class LockHeld(RuntimeError):
    """Another process holds the lock."""


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[str] | None = None

    def acquire(self, label: str = "") -> bool:
        """Take the lock without waiting. False if someone else has it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False

        # Informational only - who holds it, for the error message.
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()} {label} {datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}\n")
            fh.flush()
        except OSError:
            pass
        self._fh = fh
        return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()

    def holder(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def __enter__(self) -> FileLock:
        if not self.acquire():
            raise LockHeld(self.holder() or str(self.path))
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
