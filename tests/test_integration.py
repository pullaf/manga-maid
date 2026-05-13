"""Integration tests — real MangaDex API + ``mdx``, official Test manga only.

https://mangadex.org/title/f9c33607-9180-4ba6-b85c-e4b5faee7192

Run::

    pytest tests/test_integration.py --run-integration -v

Environment::

    MDEX_TEST_MANGA_ID   UUID (default: MangaDex official \"Test\" manga)
    MDEX_TEST_LANG       translatedLanguage (default: en)

No curated romance/obscure titles in-repo — only the sanctioned stress-test title.

The pipeline test patches ``MangaDexSource.iter_feed`` so we fetch **one descending feed page**
instead of walking tens of thousands of chapters from ch.1 upward (what production ``iter_feed``
does with ``order[chapter]=asc``). ``start_chapter`` is set to the **latest** chapter number so
``get_chapters_to_download`` queues ~one chapter — exercising ``insert_series``, feed → DB
upserts, ``start_chapter`` filtering (the UI calls this “Start from chapter ≥”; legacy ``since``
was migrated to this column — see ``db.py``), sync download, and naming under ``chapter_naming``.
"""

from __future__ import annotations

import os
import re
import shutil
from unittest.mock import patch

import pytest

import db as dbmod
import manga_sync
from sources.mangadex import CONTENT_RATINGS, MangaDexSource

pytestmark = pytest.mark.integration

OFFICIAL_TEST_MANGA_ID = "f9c33607-9180-4ba6-b85c-e4b5faee7192"
MANGA_ID = os.environ.get("MDEX_TEST_MANGA_ID", OFFICIAL_TEST_MANGA_ID).strip()
LANG = os.environ.get("MDEX_TEST_LANG", "en").strip() or "en"

_SERIES_REL_PATH = "library/md_integration"


@pytest.fixture
def mdx():
    path = shutil.which("mdx")
    if not path:
        pytest.skip("mdx not found in PATH")
    return path


@pytest.fixture
def isolated_library(tmp_path, monkeypatch):
    """Hermetic ``MANGA_ROOT`` + ``DATA_DIR`` + fresh SQLite."""
    manga_root = tmp_path / "manga"
    data_dir = tmp_path / "data"
    series_dir = manga_root.joinpath(*_SERIES_REL_PATH.split("/"))
    series_dir.mkdir(parents=True)
    data_dir.mkdir()

    monkeypatch.setenv("MANGA_ROOT", str(manga_root))
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("SYNC_LOG", str(data_dir / "sync.log"))

    monkeypatch.setattr(dbmod, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(dbmod, "MANGA_ROOT", str(manga_root))

    monkeypatch.setattr(manga_sync, "MANGA_ROOT", str(manga_root))
    monkeypatch.setattr(manga_sync, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(manga_sync, "SYNC_LOG", str(data_dir / "sync.log"))

    conn = dbmod.init_db(str(data_dir))
    return conn, series_dir


def _api_latest_chapter_num() -> float:
    """Single MD API request — highest chapter for ``LANG``."""
    src = MangaDexSource()
    data = src._api_get(
        f"/manga/{MANGA_ID}/feed",
        {
            "translatedLanguage[]": LANG,
            "limit": 1,
            "offset": 0,
            "order[chapter]": "desc",
            "includes[]": "scanlation_group",
            "contentRating[]": CONTENT_RATINGS,
        },
    )
    items = data.get("data") or []
    assert len(items) >= 1
    return float(items[0]["attributes"]["chapter"])


def _bounded_desc_feed_iter_feed(self, manga_id: str, lang: str, params_extra=None):
    """Test substitute for ``iter_feed``: one **desc** page, ignores caller ``asc`` ordering.

    Production ``_sync_chapters_mdx`` passes ``order[chapter]=asc``, which paginates from ch.1 —
    unusable for MangaDex's official Test title (huge catalogue).
    """
    params = {
        "translatedLanguage[]": lang,
        "limit": 120,
        "offset": 0,
        "order[chapter]": "desc",
        "includes[]": "scanlation_group",
        "contentRating[]": CONTENT_RATINGS,
    }
    data = self._api_get(f"/manga/{manga_id}/feed", params)
    yield from data.get("data") or []


class TestMangaDexOfficialAPI:
    """Cheap API-only checks (no DB, no ``mdx``)."""

    def test_metadata_title(self):
        meta = MangaDexSource().get_metadata(MANGA_ID)
        title = (meta.get("title") or "").strip()
        assert title
        assert "test" in title.lower()

    def test_volume_covers(self):
        covers = manga_sync.fetch_volume_covers(MANGA_ID)
        assert len(covers) >= 1
        for url in list(covers.values())[:5]:
            assert "uploads.mangadex.org/covers/" in url


class TestOfficialPipeline:
    """DB insert + bounded feed + ``start_chapter`` + real chapter download."""

    def test_insert_series_sync_latest_chapter_download_named_cbz(
        self, mdx, isolated_library,
    ):
        conn, series_dir = isolated_library
        latest = _api_latest_chapter_num()

        sid = dbmod.insert_series(
            conn,
            path=_SERIES_REL_PATH,
            title='Official "Test" Manga',
            language=LANG,
            start_chapter=latest,
            source="mangadex",
            source_id=MANGA_ID,
            sync_configured=1,
        )
        assert sid >= 1

        row = dbmod.get_series_by_path(conn, _SERIES_REL_PATH)
        assert row is not None
        assert row["linked"]
        assert row["source_id"] == MANGA_ID
        assert float(row["start_chapter"]) == latest

        settings = {
            "download_delay": 0.0,
            "file_format": "cbz",
            "chapter_naming": "%3 ch.%5",
            "merge_volumes": False,
            "webhook_url": "",
            "file_permission_mask": None,
        }

        with patch.object(MangaDexSource, "iter_feed", _bounded_desc_feed_iter_feed):
            got = manga_sync._sync_one_series(row, conn, settings, None)

        assert got == 1, "expected exactly one chapter queued at start_chapter>=latest"
        cbzs = list(series_dir.glob("*.cbz"))
        assert len(cbzs) == 1
        stem = cbzs[0].stem
        assert "ch." in stem.lower()
        m = re.search(r"ch\.(\d+(?:\.\d+)?)", stem, re.I)
        assert m is not None
        assert abs(float(m.group(1)) - latest) < 1e-6
