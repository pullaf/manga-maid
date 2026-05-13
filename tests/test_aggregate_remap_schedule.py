"""Weekly debounce for MangaDex /aggregate volume remapping."""

from datetime import datetime, timedelta

import pytest

import db as dbmod


@pytest.fixture
def conn(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(dbmod, "DATA_DIR", str(data))
    return dbmod.init_db(str(data))


def test_weekly_not_due_without_materialized_gap(conn):
    sid = dbmod.insert_series(
        conn,
        path="x/a",
        title="T",
        language="en",
        start_chapter=0.0,
        source="mangadex",
        source_id="00000000-0000-0000-0000-000000000001",
    )
    dbmod.upsert_chapter(
        conn,
        sid,
        1.0,
        "mangadex",
        "ch-1",
        status="known",
    )
    assert dbmod.series_has_materialized_chapter_missing_volume(conn, sid) is False
    assert dbmod.weekly_mdx_aggregate_volume_remap_due(conn, sid, 7) is False


def test_weekly_due_when_file_row_missing_volume_and_no_prior_touch(conn):
    sid = dbmod.insert_series(
        conn,
        path="x/b",
        title="T",
        language="en",
        start_chapter=0.0,
        source="mangadex",
        source_id="00000000-0000-0000-0000-000000000002",
    )
    dbmod.upsert_chapter(
        conn,
        sid,
        1.0,
        "mangadex",
        "ch-1",
        status="downloaded",
        path="x/b/f.cbz",
    )
    assert dbmod.series_has_materialized_chapter_missing_volume(conn, sid) is True
    assert dbmod.weekly_mdx_aggregate_volume_remap_due(conn, sid, 7) is True


def test_weekly_suppressed_after_touch_within_interval(conn):
    sid = dbmod.insert_series(
        conn,
        path="x/c",
        title="T",
        language="en",
        start_chapter=0.0,
        source="mangadex",
        source_id="00000000-0000-0000-0000-000000000003",
    )
    dbmod.upsert_chapter(
        conn,
        sid,
        2.0,
        "mangadex",
        "ch-2",
        status="downloaded",
        path="x/c/g.cbz",
    )
    dbmod.touch_series_aggregate_volume_remap_at(conn, sid)
    assert dbmod.weekly_mdx_aggregate_volume_remap_due(conn, sid, 7) is False


def test_weekly_due_again_after_interval_with_still_missing_volume(conn):
    sid = dbmod.insert_series(
        conn,
        path="x/d",
        title="T",
        language="en",
        start_chapter=0.0,
        source="mangadex",
        source_id="00000000-0000-0000-0000-000000000004",
    )
    dbmod.upsert_chapter(
        conn,
        sid,
        3.0,
        "mangadex",
        "ch-3",
        status="on_disk",
        path="x/d/h.cbz",
    )
    old = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE series SET last_aggregate_volume_remap_at=? WHERE id=?",
        (old, sid),
    )
    conn.commit()
    assert dbmod.weekly_mdx_aggregate_volume_remap_due(conn, sid, 7) is True
