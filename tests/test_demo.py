"""The demo: a made-up candidate and sample jobs, in a folder of its own."""

from __future__ import annotations

import yaml


def test_demo_seeds_its_own_folder_and_leaves_the_real_one_alone(tmp_path, monkeypatch):
    from jobhunt.config import settings
    from jobhunt.store import db

    real = settings().data_dir
    monkeypatch.setenv("JOBHUNT_DATA_DIR", str(real))  # restored after the test
    try:
        from jobhunt.demo import JOBS, seed

        folder = seed(tmp_path / "demo")
        assert settings().data_dir == folder != real

        profile = yaml.safe_load((folder / "profile" / "profile.yaml").read_text(encoding="utf-8"))
        assert profile["identity"]["email"].endswith("@example.com")

        from sqlmodel import select

        from jobhunt.store.models import Application, Job, JobStatus

        with db.session() as s:
            jobs = s.exec(select(Job)).all()
            assert len(jobs) == len(JOBS)
            statuses = {j.status for j in jobs}
            assert {JobStatus.QUEUED_APPROVAL, JobStatus.APPLIED, JobStatus.REJECTED_GATE} <= statuses
            assert all(j.score is not None for j in jobs if j.status is JobStatus.QUEUED_APPROVAL)
            assert len(s.exec(select(Application)).all()) == 2

        # Running it again starts from scratch rather than piling up jobs.
        seed(folder)
        with db.session() as s:
            assert len(s.exec(select(Job)).all()) == len(JOBS)
    finally:
        db.get_engine().dispose()
        monkeypatch.undo()
        settings.cache_clear()
        db.get_engine.cache_clear()
    assert settings().data_dir == real
