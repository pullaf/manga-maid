"""Tests for per-call connection factory and new db helpers introduced in v2.1.0."""
import os
import pytest
import db as _db


@pytest.fixture
def tmp_conn(tmp_path):
    os.environ.setdefault("DATA_DIR", str(tmp_path))
    conn = _db.init_db(str(tmp_path))
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# _get_conn() is a factory — each call must return a distinct object
# ---------------------------------------------------------------------------

def test_get_conn_returns_distinct_objects(tmp_path):
    a = _db.get_conn(str(tmp_path))
    b = _db.get_conn(str(tmp_path))
    try:
        assert a is not b, "_get_conn() should return fresh connections, not a singleton"
    finally:
        a.close()
        b.close()


def test_get_conn_connections_are_independent(tmp_path):
    _db.init_db(str(tmp_path))
    a = _db.get_conn(str(tmp_path))
    b = _db.get_conn(str(tmp_path))
    try:
        # close one — the other must still work
        a.close()
        row = b.execute("SELECT COUNT(*) FROM series").fetchone()
        assert row[0] == 0
    finally:
        b.close()


# ---------------------------------------------------------------------------
# mark_chapter_downloaded commit=False — rows visible only after explicit commit
# ---------------------------------------------------------------------------

def test_mark_chapter_no_commit_not_visible_on_rollback(tmp_path):
    conn = _db.init_db(str(tmp_path))
    now = _db._now()
    conn.execute(
        "INSERT INTO series (title, path, language, created_at, updated_at) VALUES (?,?,?,?,?)",
        ("Test", "test-series", "en", now, now),
    )
    conn.commit()
    series_id = conn.execute("SELECT id FROM series WHERE path='test-series'").fetchone()["id"]
    conn.execute(
        "INSERT INTO chapters (series_id, source_chapter_id, chapter_num, status) VALUES (?,?,?,?)",
        (series_id, "ch1", 1.0, "queued"),
    )
    conn.commit()
    ch_id = conn.execute("SELECT id FROM chapters WHERE series_id=?", (series_id,)).fetchone()["id"]

    _db.mark_chapter_downloaded(conn, ch_id, "test-series/ch1.cbz", 1234, commit=False)

    # roll back — update must be gone
    conn.rollback()
    row = conn.execute("SELECT path FROM chapters WHERE id=?", (ch_id,)).fetchone()
    assert row["path"] is None, "commit=False should not have persisted the update"
    conn.close()


def test_mark_chapter_no_commit_visible_after_explicit_commit(tmp_path):
    conn = _db.init_db(str(tmp_path))
    now = _db._now()
    conn.execute(
        "INSERT INTO series (title, path, language, created_at, updated_at) VALUES (?,?,?,?,?)",
        ("Test", "test-series2", "en", now, now),
    )
    conn.commit()
    series_id = conn.execute("SELECT id FROM series WHERE path='test-series2'").fetchone()["id"]
    conn.execute(
        "INSERT INTO chapters (series_id, source_chapter_id, chapter_num, status) VALUES (?,?,?,?)",
        (series_id, "ch2", 2.0, "queued"),
    )
    conn.commit()
    ch_id = conn.execute("SELECT id FROM chapters WHERE series_id=?", (series_id,)).fetchone()["id"]

    _db.mark_chapter_downloaded(conn, ch_id, "test-series2/ch2.cbz", 5678, commit=False)
    conn.commit()

    row = conn.execute("SELECT path, file_size FROM chapters WHERE id=?", (ch_id,)).fetchone()
    assert row["path"] == "test-series2/ch2.cbz"
    assert row["file_size"] == 5678
    conn.close()


# ---------------------------------------------------------------------------
# record_job_timing — stores rolling window and computes correct values
# ---------------------------------------------------------------------------

def test_record_job_timing_stores_entry(tmp_conn):
    _db.record_job_timing(tmp_conn, "sync", 42.5)
    usage = _db.get_usage(tmp_conn)
    timings = usage.get("job_timings") or []
    assert len(timings) == 1
    assert timings[0]["type"] == "sync"
    assert timings[0]["duration_s"] == 42.5


def test_record_job_timing_rolling_window(tmp_conn):
    for i in range(25):
        _db.record_job_timing(tmp_conn, "sync", float(i))
    usage = _db.get_usage(tmp_conn)
    timings = usage.get("job_timings") or []
    assert len(timings) == 20, "should keep at most 20 entries"
    # most recent 20 entries are values 5..24
    values = [t["duration_s"] for t in timings]
    assert values[0] == 5.0
    assert values[-1] == 24.0


def test_record_job_timing_multiple_types(tmp_conn):
    _db.record_job_timing(tmp_conn, "sync", 10.0)
    _db.record_job_timing(tmp_conn, "reconcile", 3.0)
    usage = _db.get_usage(tmp_conn)
    timings = usage.get("job_timings") or []
    types = {t["type"] for t in timings}
    assert "sync" in types
    assert "reconcile" in types
