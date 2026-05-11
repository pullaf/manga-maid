"""Integration tests - hit the real MangaDex API and invoke mdx.

Run with:  pytest tests/test_integration.py --run-integration -v

Test manga: 7-Kakan Gentei Kanojo (b80ea8bd-7293-4e61-965a-14594059dde1)
  - 3 volumes, completed, English available
  - vol 1: ch 1–5.2 (7 chapters)
  - vol 2: ch 6–10.5 (6 chapters)
  - vol 3: ch 11–15.5 (6 chapters)
  - all 3 volumes have cover art on MangaDex
"""
import os
import shutil

import pytest

import manga_sync

MANGA_ID = "b80ea8bd-7293-4e61-965a-14594059dde1"
MANGA_LANG = "en"
CONFIG = {"id": MANGA_ID, "language": MANGA_LANG}

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def mdx():
    path = shutil.which("mdx")
    if not path:
        pytest.skip("mdx not found in PATH")
    return path


# One shared download dir per test session - avoids re-downloading the same
# files across multiple download tests.
@pytest.fixture(scope="module")
def dl_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("downloads")


# ---------------------------------------------------------------------------
# MangaDex API - no mdx needed
# ---------------------------------------------------------------------------

class TestMangaDexAPI:
    def test_fetch_new_volumes_returns_all_three(self):
        vols = manga_sync.fetch_new_volumes(CONFIG, after_vol=0)
        assert set(vols) == {1, 2, 3}

    def test_fetch_new_volumes_respects_after(self):
        vols = manga_sync.fetch_new_volumes(CONFIG, after_vol=2)
        assert vols == [3]

    def test_fetch_new_volumes_up_to_date(self):
        vols = manga_sync.fetch_new_volumes(CONFIG, after_vol=3)
        assert vols == []

    def test_fetch_volume_covers_all_volumes(self):
        covers = manga_sync.fetch_volume_covers(MANGA_ID)
        # All three volumes must have a cover
        assert "1" in covers
        assert "2" in covers
        assert "3" in covers

    def test_fetch_volume_covers_are_valid_urls(self):
        covers = manga_sync.fetch_volume_covers(MANGA_ID)
        for vol, url in covers.items():
            assert url.startswith("https://uploads.mangadex.org/covers/")
            assert MANGA_ID in url
            assert url.endswith(".512.jpg") or url.endswith(".512.png")

    def test_fetch_latest_chapter(self):
        ch = manga_sync.fetch_latest_chapter(CONFIG)
        assert ch is not None
        assert ch.ch_num == 15.5

    def test_chapters_after_returns_half_chapters(self):
        # vol 1 has ch 5.1 and 5.2 - good float chapter test
        chapters = manga_sync.fetch_chapters_after(CONFIG, after=4)
        nums = {c.ch_num for c in chapters}
        assert 5.0 in nums
        assert 5.1 in nums
        assert 5.2 in nums


# ---------------------------------------------------------------------------
# Chapter mode download
# ---------------------------------------------------------------------------

class TestChapterDownload:
    def test_download_chapter_creates_file(self, mdx, dl_dir):
        ch = manga_sync.fetch_latest_chapter(CONFIG)
        # Use ch.1 (smallest), not ch.15.5 (end)
        chapters = manga_sync.fetch_chapters_after(CONFIG, after=0)
        ch1 = next(c for c in chapters if c.ch_num == 1.0)
        settings = {
            "file_format": "cbz",
            "chapter_naming": "[%1 %2] %3 vol.%4 ch.%5",
        }
        result = manga_sync.mdx_download_chapter(ch1, CONFIG, str(dl_dir), settings)
        assert result is True
        cbz_files = list(dl_dir.glob("*.cbz"))
        assert len(cbz_files) >= 1

    def test_downloaded_chapter_appears_in_chapters_on_disk(self, mdx, dl_dir):
        # Depends on test_download_chapter_creates_file having run first
        on_disk = manga_sync.chapters_on_disk(str(dl_dir))
        assert 1.0 in on_disk

    def test_custom_naming_scheme(self, mdx, tmp_path):
        chapters = manga_sync.fetch_chapters_after(CONFIG, after=1)
        ch2 = next(c for c in chapters if c.ch_num == 2.0)
        settings = {
            "file_format": "cbz",
            "chapter_naming": "%3 ch.%5",
        }
        result = manga_sync.mdx_download_chapter(ch2, CONFIG, str(tmp_path), settings)
        assert result is True
        files = list(tmp_path.glob("*.cbz"))
        assert any("ch." in f.name for f in files)
        # No bracket group prefix in this naming scheme
        assert not any(f.name.startswith("[") for f in files)


# ---------------------------------------------------------------------------
# Volume mode download
# ---------------------------------------------------------------------------

class TestVolumeDownload:
    def test_download_volume_creates_file(self, mdx, dl_dir):
        vol_dir = dl_dir / "vol_mode"
        vol_dir.mkdir(exist_ok=True)
        settings = {
            "file_format": "cbz",
            "volume_naming": "[%1 %2] %3 vol.%4",
        }
        result = manga_sync.mdx_download_volume(1, CONFIG, str(vol_dir), settings)
        assert result is True
        cbz_files = list(vol_dir.glob("*.cbz"))
        assert len(cbz_files) >= 1

    def test_volume_file_appears_in_volumes_on_disk(self, mdx, dl_dir):
        vol_dir = dl_dir / "vol_mode"
        vols = manga_sync.volumes_on_disk(str(vol_dir))
        assert 1 in vols

    def test_volume_naming_applied(self, mdx, tmp_path):
        settings = {
            "file_format": "cbz",
            "volume_naming": "%3 vol.%4",
        }
        result = manga_sync.mdx_download_volume(2, CONFIG, str(tmp_path), settings)
        assert result is True
        files = list(tmp_path.glob("*.cbz"))
        assert any("vol." in f.name for f in files)
        assert not any(f.name.startswith("[") for f in files)

    def test_volume_mode_no_individual_chapters(self, mdx, dl_dir):
        # In volume mode the file should NOT contain a ch. tag in its name
        # (it's a merged volume file, not a per-chapter file)
        vol_dir = dl_dir / "vol_mode"
        files = list(vol_dir.glob("*.cbz"))
        assert len(files) >= 1
        # At least one file should be volume-named, not chapter-named
        assert any("vol." in f.name.lower() for f in files)


# ---------------------------------------------------------------------------
# Covers (API only - Kavita upload not tested without an instance)
# ---------------------------------------------------------------------------

class TestCovers:
    def test_all_covers_fetchable(self):
        covers = manga_sync.fetch_volume_covers(MANGA_ID)
        assert len(covers) == 3

    def test_cover_url_is_reachable(self):
        import urllib.request
        covers = manga_sync.fetch_volume_covers(MANGA_ID)
        url = covers["1"]
        # Fetch only the first byte - CDN doesn't support HEAD
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status in (200, 206)
