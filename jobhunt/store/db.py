"""Database engine and session helpers."""

from __future__ import annotations

import functools
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from jobhunt.config import settings, targets

# Importing models for their side effect of registering tables on SQLModel's
# metadata - init_db() cannot create what has not been imported.
from jobhunt.store import models  # noqa: F401  (registers tables)
from jobhunt.store.models import RateLimit


@functools.lru_cache(maxsize=1)
def get_engine():
    path = settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        echo=False,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        # WAL lets the dashboard read while a run is writing - without it the
        # UI blocks every time the scheduler fires.
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def init_db() -> None:
    SQLModel.metadata.create_all(get_engine())


@contextmanager
def session() -> Iterator[Session]:
    """A transactional session whose objects stay readable after commit.

    ``expire_on_commit=False`` matters here: agents routinely return Job rows
    for a caller to render, and the default behaviour expires every attribute
    on commit, so the first attribute access outside the block raises
    DetachedInstanceError.
    """
    with Session(get_engine(), expire_on_commit=False) as s:
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise


# --------------------------------------------------------------------------
# Daily rate limiting
# --------------------------------------------------------------------------


def _today() -> str:
    tz = targets().get("schedule", {}).get("timezone", "Asia/Kolkata")
    return datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")


def remaining_quota(key: str) -> int:
    """How many more of ``key`` may happen today."""
    caps = targets().get("limits", {})
    cap = {
        "cold_email": caps.get("cold_emails_per_day", 15),
        "application": caps.get("applications_per_day", 25),
        "linkedin_view": caps.get("linkedin_profile_views_per_run", 40),
        "linkedin_search": caps.get("linkedin_searches_per_run", 15),
    }.get(key, 0)

    with session() as s:
        row = s.exec(
            select(RateLimit).where(RateLimit.key == key, RateLimit.day == _today())
        ).first()
        used = row.count if row else 0
    return max(0, cap - used)


def consume_quota(key: str, n: int = 1) -> bool:
    """Consume ``n`` units of today's quota. Returns False if it would exceed."""
    if remaining_quota(key) < n:
        return False
    with session() as s:
        row = s.exec(
            select(RateLimit).where(RateLimit.key == key, RateLimit.day == _today())
        ).first()
        if row is None:
            row = RateLimit(key=key, day=_today(), count=0)
        row.count += n
        s.add(row)
    return True
