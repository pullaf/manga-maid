"""delete_series must succeed when rename_log references the series (FK cleanup)."""

from __future__ import annotations

import db as dbmod


def test_delete_series_removes_rename_log_first(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    manga_root = tmp_path / "manga"
    data_dir.mkdir()
    manga_root.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("MANGA_ROOT", str(manga_root))
    monkeypatch.setattr(dbmod, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(dbmod, "MANGA_ROOT", str(manga_root))

    conn = dbmod.init_db(str(data_dir))
    sid = dbmod.insert_series(
        conn,
        path="en/Example",
        title="Example",
        language="en",
        start_chapter=0.0,
        source="mangadex",
        source_id="00000000-0000-0000-0000-000000000001",
    )
    dbmod.log_rename(
        conn,
        "old.cbz",
        "new.cbz",
        "rename",
        "test",
        series_id=sid,
    )
    assert conn.execute("SELECT COUNT(*) FROM rename_log WHERE series_id=?", (sid,)).fetchone()[0] == 1

    assert dbmod.delete_series(conn, "en/Example") is True
    assert conn.execute("SELECT COUNT(*) FROM series WHERE id=?", (sid,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM rename_log WHERE series_id=?", (sid,)).fetchone()[0] == 0
