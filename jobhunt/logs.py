"""Logging: a rotating file per process kind, plus the console when there is one.

Files live in ``<data>/logs/``: ``web.log`` for the dashboard (and the runs and
scheduler it hosts), ``scheduler.log`` for the standalone scheduler, ``cli.log``
for one-off commands. Separate files because Windows cannot rotate a log that
another process still has open.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from jobhunt.config import settings

_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s"
_CONSOLE_FORMAT = "[%(levelname)s] %(message)s"

# Chatty libraries whose INFO lines drown out the pipeline's own.
_QUIET = ("httpx", "httpcore", "apscheduler", "urllib3", "googleapiclient", "uvicorn.access")


def configure(kind: str = "cli", level: int = logging.WARNING) -> Path:
    """Set up logging once per process. Returns the log file path.

    ``level`` is what reaches the console; the file always gets INFO and up,
    because the file is what you read after something went wrong unattended.
    """
    root = logging.getLogger()
    path = settings().logs_dir / f"{kind}.log"
    if getattr(root, "_jobhunt_configured", None) == str(path):
        for h in root.handlers:
            if getattr(h, "_jobhunt_console", False):
                h.setLevel(level)
        return path

    for h in list(root.handlers):
        if getattr(h, "_jobhunt_handler", False):
            root.removeHandler(h)
            h.close()

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter(_FILE_FORMAT))
        fh._jobhunt_handler = True  # type: ignore[attr-defined]
        root.addHandler(fh)
    except OSError as exc:  # read-only disk etc. - console logging still works
        print(f"warning: cannot write logs to {path}: {exc}", file=sys.stderr)

    # pythonw.exe (the logon task) has no console at all.
    if sys.stderr is not None:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(level)
        ch.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
        ch._jobhunt_handler = True  # type: ignore[attr-defined]
        ch._jobhunt_console = True  # type: ignore[attr-defined]
        root.addHandler(ch)

    root.setLevel(min(level, logging.INFO))
    for name in _QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
    root._jobhunt_configured = str(path)  # type: ignore[attr-defined]
    return path
