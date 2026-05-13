"""Fixtures for FastAPI route tests (hermetic manga + data dirs)."""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    """TestClient with tmp MANGA_ROOT/DATA_DIR, root folder configured, no background workers."""
    monkeypatch.setenv("MANGA_TEST_SKIP_LIFESPAN", "1")
    monkeypatch.setenv("RUNTIME_CRONTAB_PATH", str(tmp_path / "runtime.crontab"))
    monkeypatch.setenv("RECONCILE_INTERVAL_SECONDS", "999999")

    manga = tmp_path / "manga"
    data = tmp_path / "data"
    library = manga / "library"
    library.mkdir(parents=True)
    data.mkdir()

    monkeypatch.setenv("MANGA_ROOT", str(manga))
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("SYNC_LOG", str(data / "sync.log"))

    import db as dbmod

    monkeypatch.setattr(dbmod, "DATA_DIR", str(data))
    monkeypatch.setattr(dbmod, "MANGA_ROOT", str(manga))

    import sync_config as sc

    monkeypatch.setattr(sc, "_settings_cache", None)
    monkeypatch.setattr(sc, "_settings_cache_at", 0.0)

    from sync_config import save_settings

    save_settings({"root_folders": ["library"]})

    import web.app as appmod

    monkeypatch.setattr(appmod, "MANGA_ROOT", str(manga))
    monkeypatch.setattr(appmod, "_MANGA_ROOT_REAL", os.path.realpath(str(manga)))
    monkeypatch.setattr(appmod, "DATA_DIR", str(data))
    monkeypatch.setattr(appmod, "_conn", None)

    from fastapi.testclient import TestClient

    with TestClient(appmod.app, raise_server_exceptions=False) as client:
        yield client
