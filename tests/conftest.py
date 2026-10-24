"""Test isolation.

Every test runs against a throwaway data directory. Without this the suite
wrote fixture jobs into your real ``data/jobs.db`` - and on a fresh clone (or
in CI) it had no database or profile to run against at all.

The environment variable must be set before anything imports ``jobhunt``:
settings are read once and cached.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_DATA = Path(tempfile.mkdtemp(prefix="jobhunt-tests-"))
os.environ["JOBHUNT_DATA_DIR"] = str(_DATA)
atexit.register(shutil.rmtree, _DATA, ignore_errors=True)

import pytest  # noqa: E402

FIXTURE_PROFILE = Path(__file__).parent / "fixtures" / "profile.yaml"


def write_sample_profile() -> Path:
    """Install the fixture profile: a real-shaped work history with fictional
    identity, pay and demographic answers. Personal data does not belong in a
    test suite."""
    from jobhunt.config import settings

    path = settings().profile_path
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE_PROFILE, path)
    return path


write_sample_profile()


@pytest.fixture
def data_dir() -> Path:
    return _DATA


@pytest.fixture
def no_profile():
    """Run a test as a brand-new install: no profile saved yet."""
    from jobhunt.config import settings

    path = settings().profile_path
    backup = path.with_suffix(".bak")
    path.replace(backup)
    try:
        yield
    finally:
        settings().profile_draft_path.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        backup.replace(path)


@pytest.fixture
def local_targets_file():
    """Restore the local targets overrides after a test that saves filters."""
    from jobhunt.config import settings

    path = settings().local_targets_path
    before = path.read_bytes() if path.exists() else None
    yield path
    if before is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(before)
