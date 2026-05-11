"""Tests for manga-sync.py - imported as manga_sync via conftest."""
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
