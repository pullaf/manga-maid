"""Tests for manga-sync.py - imported as manga_sync via conftest."""
import os
from unittest.mock import MagicMock, patch

import manga_sync


# ---------------------------------------------------------------------------
# _mdx_download
# ---------------------------------------------------------------------------

def _mock_run(returncode=0):
    m = MagicMock()
    m.returncode = returncode
    m.stderr = ""
    m.stdout = ""
    return m


def _ch_row(source_chapter_id="abc123", chapter_num=7.0):
    return {"source_chapter_id": source_chapter_id, "chapter_num": chapter_num}


def test_mdx_download_uses_chapter_url():
    with patch.object(manga_sync.subprocess, "run", return_value=_mock_run()) as mock_run:
        manga_sync._mdx_download(_ch_row("abc123"), "/out", "cbz", "[%1 %2] %3 ch.%5")
    cmd = mock_run.call_args[0][0]
    assert "-s" in cmd
    assert "https://mangadex.org/chapter/abc123" in cmd


def test_mdx_download_passes_format():
    with patch.object(manga_sync.subprocess, "run", return_value=_mock_run()) as mock_run:
        manga_sync._mdx_download(_ch_row(), "/out", "zip", "%3 ch.%5")
    cmd = mock_run.call_args[0][0]
    assert "-e" in cmd and cmd[cmd.index("-e") + 1] == "zip"


def test_mdx_download_passes_naming_template():
    with patch.object(manga_sync.subprocess, "run", return_value=_mock_run()) as mock_run:
        manga_sync._mdx_download(_ch_row(), "/out", "cbz", "%3 ch.%5")
    cmd = mock_run.call_args[0][0]
    assert "--file-name" in cmd and cmd[cmd.index("--file-name") + 1] == "%3 ch.%5"


def test_mdx_download_passes_output_dir():
    with patch.object(manga_sync.subprocess, "run", return_value=_mock_run()) as mock_run:
        manga_sync._mdx_download(_ch_row(), "/some/dir", "cbz", "%3")
    cmd = mock_run.call_args[0][0]
    assert "-o" in cmd and cmd[cmd.index("-o") + 1] == "/some/dir"


def test_mdx_download_returns_false_on_error():
    with patch.object(manga_sync.subprocess, "run", return_value=_mock_run(returncode=1)):
        assert manga_sync._mdx_download(_ch_row(), "/out", "cbz", "%3") is False


def test_mdx_download_returns_true_on_success():
    with patch.object(manga_sync.subprocess, "run", return_value=_mock_run(returncode=0)):
        assert manga_sync._mdx_download(_ch_row(), "/out", "cbz", "%3") is True


# ---------------------------------------------------------------------------
# fetch_volume_covers
# ---------------------------------------------------------------------------

def test_fetch_volume_covers():
    mock_data = {
        "data": [
            {"attributes": {"volume": "1", "fileName": "cover1.jpg"}},
            {"attributes": {"volume": "2", "fileName": "cover2.jpg"}},
            {"attributes": {"volume": "3", "fileName": "cover3.jpg"}},
        ]
    }
    from sources.mangadex import MangaDexSource
    with patch.object(MangaDexSource, "_api_get", return_value=mock_data):
        covers = manga_sync.fetch_volume_covers("manga-id")
    assert covers["1"] == "https://uploads.mangadex.org/covers/manga-id/cover1.jpg.512.jpg"
    assert covers["2"] == "https://uploads.mangadex.org/covers/manga-id/cover2.jpg.512.jpg"
    assert "3" in covers


def test_fetch_volume_covers_skips_null_volume():
    mock_data = {
        "data": [
            {"attributes": {"volume": None, "fileName": "cover.jpg"}},
            {"attributes": {"volume": "1", "fileName": "real.jpg"}},
        ]
    }
    from sources.mangadex import MangaDexSource
    with patch.object(MangaDexSource, "_api_get", return_value=mock_data):
        covers = manga_sync.fetch_volume_covers("manga-id")
    assert None not in covers
    assert "1" in covers


def test_fetch_volume_covers_skips_missing_filename():
    mock_data = {
        "data": [
            {"attributes": {"volume": "1", "fileName": None}},
        ]
    }
    from sources.mangadex import MangaDexSource
    with patch.object(MangaDexSource, "_api_get", return_value=mock_data):
        covers = manga_sync.fetch_volume_covers("manga-id")
    assert covers == {}


def test_fetch_volume_covers_paginates():
    """Volume N can sit on page 2 when the title has >100 cover entries."""
    first = [
        {"attributes": {"volume": str(i), "fileName": f"c{i}.jpg"}}
        for i in range(1, 101)
    ]
    page1 = {"data": first, "total": 101}
    page2 = {
        "data": [{"attributes": {"volume": "101", "fileName": "v101.jpg"}}],
        "total": 101,
    }
    from sources.mangadex import MangaDexSource
    with patch.object(MangaDexSource, "_api_get", side_effect=[page1, page2]) as m, patch(
        "sources.mangadex.time.sleep"
    ):
        covers = manga_sync.fetch_volume_covers("mid")
    assert m.call_count == 2
    assert "101" in covers
    assert "1" in covers


# ---------------------------------------------------------------------------
# _series_preferred_groups
# ---------------------------------------------------------------------------

def test_preferred_groups_returns_list_from_row():
    row = {"preferred_groups": ["GroupA", "GroupB"], "preferred_groups_json": None, "preferred_group": None}
    assert manga_sync._series_preferred_groups(row) == ["GroupA", "GroupB"]


def test_preferred_groups_empty_when_none():
    row = {"preferred_groups": None, "preferred_groups_json": None, "preferred_group": None}
    result = manga_sync._series_preferred_groups(row)
    assert isinstance(result, list)
    assert result == []


# ---------------------------------------------------------------------------
# _rotate_log
# ---------------------------------------------------------------------------

def test_rotate_log_trims_to_max(tmp_path, monkeypatch):
    log = tmp_path / "sync.log"
    lines = [f"line {i}\n" for i in range(6000)]
    log.write_text("".join(lines))
    monkeypatch.setattr(manga_sync, "SYNC_LOG", str(log))
    monkeypatch.setattr(manga_sync, "SYNC_LOG_MAX_LINES", 5000)
    manga_sync._rotate_log()
    result = log.read_text().splitlines()
    assert len(result) == 5000
    assert result[0] == "line 1000"
    assert result[-1] == "line 5999"


def test_rotate_log_noop_when_small(tmp_path, monkeypatch):
    log = tmp_path / "sync.log"
    log.write_text("".join(f"line {i}\n" for i in range(100)))
    monkeypatch.setattr(manga_sync, "SYNC_LOG", str(log))
    monkeypatch.setattr(manga_sync, "SYNC_LOG_MAX_LINES", 5000)
    manga_sync._rotate_log()
    assert len(log.read_text().splitlines()) == 100


def test_rotate_log_handles_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(manga_sync, "SYNC_LOG", str(tmp_path / "nonexistent.log"))
    manga_sync._rotate_log()  # should not raise


# ---------------------------------------------------------------------------
# sources/suwayomi - _parse_chapter_name
# ---------------------------------------------------------------------------

def test_parse_chapter_name_full():
    from sources.suwayomi import _parse_chapter_name
    vol, ch, title = _parse_chapter_name("Vol.2 Ch.14.5 - Some Title", 14.5)
    assert vol == 2.0
    assert ch == 14.5
    assert title == "Some Title"


def test_parse_chapter_name_no_volume():
    from sources.suwayomi import _parse_chapter_name
    vol, ch, title = _parse_chapter_name("Ch.1 - First Chapter", 1.0)
    assert vol is None
    assert ch == 1.0
    assert title == "First Chapter"


def test_parse_chapter_name_no_title():
    from sources.suwayomi import _parse_chapter_name
    vol, ch, title = _parse_chapter_name("Vol.3", 7.0)
    assert vol == 3.0
    assert ch == 7.0
    assert title is None


def test_parse_chapter_name_empty():
    from sources.suwayomi import _parse_chapter_name
    vol, ch, title = _parse_chapter_name("", 5.0)
    assert vol is None
    assert ch == 5.0
    assert title is None


def test_parse_chapter_name_negative_ch_num():
    from sources.suwayomi import _parse_chapter_name
    _, ch, _ = _parse_chapter_name("Ch.1 - Prologue", -1.0)
    assert ch is None


def test_stem_shows_volume_num_when_present():
    assert manga_sync._stem_shows_volume_num(
        "Yancha Gal vol.1 ch.1 (Sugoi Gyaru Scans!)", 1
    )
    assert manga_sync._stem_shows_volume_num("Title Vol.17 ch.208", 17)
    assert not manga_sync._stem_shows_volume_num("Title ch.208", 17)
    assert not manga_sync._stem_shows_volume_num("Title vol.2 ch.1", 1)


def test_stem_declares_any_volume():
    assert manga_sync._stem_declares_any_volume(
        "Yancha Gal no Anjou-san vol.17 ch.209 (Sho Habby Scans)"
    )
    assert manga_sync._stem_declares_any_volume("Title Vol.3 ch.1")
    assert not manga_sync._stem_declares_any_volume("Title ch.1")


def test_normalize_volume_cover_key():
    assert manga_sync._normalize_volume_cover_key(17) == "17"
    assert manga_sync._normalize_volume_cover_key("17.0") == "17"
    assert manga_sync._normalize_volume_cover_key("1") == "1"
    assert manga_sync._normalize_volume_cover_key("") == ""


def test_volume_cover_urls_by_canonical_key():
    assert manga_sync._volume_cover_urls_by_canonical_key({"17": "http://x/a"})["17"] == "http://x/a"
    assert manga_sync._volume_cover_urls_by_canonical_key({"17.0": "http://x/b"})["17"] == "http://x/b"


def test_missing_mdx_cover_is_notable():
    assert manga_sync._missing_mdx_cover_is_notable("17")
    assert not manga_sync._missing_mdx_cover_is_notable("-100000")
    assert not manga_sync._missing_mdx_cover_is_notable("0")
    assert not manga_sync._missing_mdx_cover_is_notable("-1")
    assert not manga_sync._missing_mdx_cover_is_notable("")
    assert not manga_sync._missing_mdx_cover_is_notable("Special")


def test_chapter_data_external_url_flag():
    """``externalUrl`` is exposed for tooling; sync does not override user/preferred uploads."""
    from sources.mangadex import _ChapterData

    hosted = {
        "id": "a",
        "attributes": {"chapter": "1", "publishAt": "2024-01-01T00:00:00Z"},
        "relationships": [],
    }
    assert _ChapterData(hosted).is_mangadex_hosted
    ext = {
        "id": "b",
        "attributes": {
            "chapter": "1",
            "externalUrl": "https://example.com/read",
            "publishAt": "2024-01-01T00:00:00Z",
        },
        "relationships": [],
    }
    assert not _ChapterData(ext).is_mangadex_hosted


def test_manga_archive_sanity_rejects_tiny_non_zip(tmp_path):
    p = tmp_path / "bad.cbz"
    p.write_bytes(b"not a zip" * 20)
    assert not manga_sync._manga_archive_passes_sanity_check(str(p))


def test_manga_archive_sanity_accepts_plump_zip(tmp_path):
    import zipfile

    # Starts like JPEG (SOI + stub JFIF) so magic-byte check passes; bulk padding for size.
    fake_page = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        + b"\xff" * 6000
        + b"\xff\xd9"
    )
    p = tmp_path / "ok.cbz"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("0001.jpg", fake_page)
    assert manga_sync._manga_archive_passes_sanity_check(str(p))


def test_manga_archive_rejects_zip_with_only_comicinfo(tmp_path):
    import zipfile

    p = tmp_path / "meta.cbz"
    payload = b"<ComicInfo/>" * 500
    with zipfile.ZipFile(p, "w") as zf:
        zi = zipfile.ZipInfo("ComicInfo.xml")
        zi.compress_type = zipfile.ZIP_STORED
        zf.writestr(zi, payload)
    assert os.path.getsize(p) >= manga_sync.MIN_MDX_ARCHIVE_BYTES
    assert not manga_sync._manga_archive_passes_sanity_check(str(p))


def test_manga_archive_rejects_html_renamed_jpg(tmp_path):
    import zipfile

    p = tmp_path / "fake.cbz"
    html = b"<html><body>error</body></html>" * 400
    with zipfile.ZipFile(p, "w") as zf:
        zi = zipfile.ZipInfo("0001.jpg")
        zi.compress_type = zipfile.ZIP_STORED
        zf.writestr(zi, html)
    assert os.path.getsize(p) >= manga_sync.MIN_MDX_ARCHIVE_BYTES
    assert not manga_sync._manga_archive_passes_sanity_check(str(p))


def test_junk_chapter_file_cleared(tmp_path, monkeypatch):
    import sqlite3

    manga_root = tmp_path / "manga"
    (manga_root / "en" / "T").mkdir(parents=True)
    bad = manga_root / "en" / "T" / "x.cbz"
    bad.write_bytes(b"x" * 100)
    monkeypatch.setattr(manga_sync, "MANGA_ROOT", str(manga_root))

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE chapters (id INTEGER PRIMARY KEY, path TEXT, status TEXT, "
        "file_size INTEGER, has_comicinfo INTEGER)"
    )
    conn.execute(
        "INSERT INTO chapters (id, path, status, file_size, has_comicinfo) VALUES (1, ?, 'downloaded', 100, 0)",
        ("en/T/x.cbz",),
    )
    existing = conn.execute("SELECT id, path, status FROM chapters WHERE id=1").fetchone()
    assert manga_sync._junk_chapter_file_cleared(conn, existing)
    row = conn.execute("SELECT path, status, file_size FROM chapters WHERE id=1").fetchone()
    assert row["path"] is None
    assert row["status"] == "known"
    assert row["file_size"] is None
    assert not bad.exists()
