"""Tests for manga-sync.py — imported as manga_sync via conftest."""
from unittest.mock import MagicMock, patch

import manga_sync


# ---------------------------------------------------------------------------
# volumes_on_disk / chapters_on_disk
# ---------------------------------------------------------------------------

def test_volumes_on_disk(tmp_path):
    (tmp_path / "Title vol.1.cbz").touch()
    (tmp_path / "Title vol.2.cbz").touch()
    (tmp_path / "Title vol.10.zip").touch()
    (tmp_path / "not-manga.txt").touch()
    assert manga_sync.volumes_on_disk(str(tmp_path)) == {1, 2, 10}


def test_volumes_on_disk_empty(tmp_path):
    assert manga_sync.volumes_on_disk(str(tmp_path)) == set()


def test_volumes_on_disk_ignores_chapters(tmp_path):
    (tmp_path / "Title ch.5.cbz").touch()
    assert manga_sync.volumes_on_disk(str(tmp_path)) == set()


def test_chapters_on_disk(tmp_path):
    (tmp_path / "Title ch.1.cbz").touch()
    (tmp_path / "Title ch.2.5.cbz").touch()
    (tmp_path / "Title vol.1 ch.3.cbz").touch()
    (tmp_path / "ignore.txt").touch()
    assert manga_sync.chapters_on_disk(str(tmp_path)) == {1.0, 2.5, 3.0}


def test_chapters_on_disk_empty(tmp_path):
    assert manga_sync.chapters_on_disk(str(tmp_path)) == set()


# ---------------------------------------------------------------------------
# fetch_new_volumes
# ---------------------------------------------------------------------------

def test_fetch_new_volumes_basic():
    mock_agg = {
        "volumes": {
            "1": {"chapters": {"1": {}, "2": {}}},
            "2": {"chapters": {"3": {}}},
            "3": {"chapters": {"4": {}}},
        }
    }
    config = {"id": "test-id", "language": "en"}
    with patch.object(manga_sync, "_api_get", return_value=mock_agg):
        result = manga_sync.fetch_new_volumes(config, after_vol=1)
    assert result == [2, 3]


def test_fetch_new_volumes_excludes_none_key():
    mock_agg = {"volumes": {"none": {"chapters": {"99": {}}}}}
    config = {"id": "test-id", "language": "en"}
    with patch.object(manga_sync, "_api_get", return_value=mock_agg):
        result = manga_sync.fetch_new_volumes(config, after_vol=0)
    assert result == []


def test_fetch_new_volumes_empty_when_up_to_date():
    mock_agg = {"volumes": {"1": {}, "2": {}, "3": {}}}
    config = {"id": "test-id", "language": "en"}
    with patch.object(manga_sync, "_api_get", return_value=mock_agg):
        result = manga_sync.fetch_new_volumes(config, after_vol=5)
    assert result == []


def test_fetch_new_volumes_deduplicates():
    mock_agg = {"volumes": {"2": {}, "2": {}}}  # noqa: F601 (intentional dup key)
    config = {"id": "test-id", "language": "en"}
    with patch.object(manga_sync, "_api_get", return_value=mock_agg):
        result = manga_sync.fetch_new_volumes(config, after_vol=0)
    assert len(result) == len(set(result))


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
    with patch.object(manga_sync, "_api_get", return_value=mock_data):
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
    with patch.object(manga_sync, "_api_get", return_value=mock_data):
        covers = manga_sync.fetch_volume_covers("manga-id")
    assert None not in covers
    assert "1" in covers


def test_fetch_volume_covers_skips_missing_filename():
    mock_data = {
        "data": [
            {"attributes": {"volume": "1", "fileName": None}},
        ]
    }
    with patch.object(manga_sync, "_api_get", return_value=mock_data):
        covers = manga_sync.fetch_volume_covers("manga-id")
    assert covers == {}


# ---------------------------------------------------------------------------
# mdx_download_chapter — checks flags passed to mdx
# ---------------------------------------------------------------------------

def _mock_run(returncode=0):
    m = MagicMock()
    m.returncode = returncode
    m.stderr = ""
    m.stdout = ""
    return m


def test_mdx_chapter_uses_chapter_flag():
    ch = MagicMock(ch_str="7")
    config = {"id": "mid", "language": "en"}
    settings = {"file_format": "cbz", "chapter_naming": "[%1 %2] %3 vol.%4 ch.%5"}
    with patch.object(manga_sync.subprocess, "run", return_value=_mock_run()) as mock_run:
        manga_sync.mdx_download_chapter(ch, config, "/out", settings)
    cmd = mock_run.call_args[0][0]
    assert "-c" in cmd
    assert "7" in cmd
    assert "-v" not in cmd


def test_mdx_chapter_applies_format_and_extension():
    ch = MagicMock(ch_str="1")
    config = {"id": "mid", "language": "en"}
    settings = {"file_format": "zip", "chapter_naming": "%3 ch.%5"}
    with patch.object(manga_sync.subprocess, "run", return_value=_mock_run()) as mock_run:
        manga_sync.mdx_download_chapter(ch, config, "/out", settings)
    cmd = mock_run.call_args[0][0]
    assert "-e" in cmd and cmd[cmd.index("-e") + 1] == "zip"
    assert "--file-name" in cmd and cmd[cmd.index("--file-name") + 1] == "%3 ch.%5"


def test_mdx_chapter_passes_translator():
    ch = MagicMock(ch_str="1")
    config = {"id": "mid", "language": "en", "translator": "TestGroup"}
    settings = {"file_format": "cbz", "chapter_naming": "%3 ch.%5"}
    with patch.object(manga_sync.subprocess, "run", return_value=_mock_run()) as mock_run:
        manga_sync.mdx_download_chapter(ch, config, "/out", settings)
    cmd = mock_run.call_args[0][0]
    assert "-t" in cmd and cmd[cmd.index("-t") + 1] == "TestGroup"


def test_mdx_chapter_no_translator_when_absent():
    ch = MagicMock(ch_str="1")
    config = {"id": "mid", "language": "en"}
    settings = {"file_format": "cbz", "chapter_naming": "%3 ch.%5"}
    with patch.object(manga_sync.subprocess, "run", return_value=_mock_run()) as mock_run:
        manga_sync.mdx_download_chapter(ch, config, "/out", settings)
    cmd = mock_run.call_args[0][0]
    assert "-t" not in cmd


def test_mdx_chapter_returns_false_on_error():
    ch = MagicMock(ch_str="1")
    config = {"id": "mid", "language": "en"}
    settings = {"file_format": "cbz", "chapter_naming": "%3 ch.%5"}
    with patch.object(manga_sync.subprocess, "run", return_value=_mock_run(returncode=1)):
        result = manga_sync.mdx_download_chapter(ch, config, "/out", settings)
    assert result is False


# ---------------------------------------------------------------------------
# mdx_download_volume — checks -v flag and volume naming
# ---------------------------------------------------------------------------

def test_mdx_volume_uses_volume_flag():
    config = {"id": "mid", "language": "en"}
    settings = {"file_format": "cbz", "volume_naming": "[%1 %2] %3 vol.%4"}
    with patch.object(manga_sync.subprocess, "run", return_value=_mock_run()) as mock_run:
        manga_sync.mdx_download_volume(3, config, "/out", settings)
    cmd = mock_run.call_args[0][0]
    assert "-v" in cmd and cmd[cmd.index("-v") + 1] == "3"
    assert "-c" not in cmd


def test_mdx_volume_applies_volume_naming():
    config = {"id": "mid", "language": "en"}
    settings = {"file_format": "cbz", "volume_naming": "%3 vol.%4"}
    with patch.object(manga_sync.subprocess, "run", return_value=_mock_run()) as mock_run:
        manga_sync.mdx_download_volume(1, config, "/out", settings)
    cmd = mock_run.call_args[0][0]
    assert "--file-name" in cmd and cmd[cmd.index("--file-name") + 1] == "%3 vol.%4"


def test_mdx_volume_returns_false_on_error():
    config = {"id": "mid", "language": "en"}
    settings = {"file_format": "cbz", "volume_naming": "%3 vol.%4"}
    with patch.object(manga_sync.subprocess, "run", return_value=_mock_run(returncode=1)):
        result = manga_sync.mdx_download_volume(1, config, "/out", settings)
    assert result is False


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
# _translator_match
# ---------------------------------------------------------------------------

def test_translator_match_no_filter():
    assert manga_sync._translator_match({}, None) is True
    assert manga_sync._translator_match({}, "") is True


def test_translator_match_case_insensitive():
    data = {"relationships": [{"type": "scanlation_group",
                               "attributes": {"name": "Mankitsu Scans"}}]}
    assert manga_sync._translator_match(data, "mankitsu") is True
    assert manga_sync._translator_match(data, "MANKITSU") is True


def test_translator_match_no_match():
    data = {"relationships": [{"type": "scanlation_group",
                               "attributes": {"name": "Other Group"}}]}
    assert manga_sync._translator_match(data, "Mankitsu") is False
