"""SuwayomiSource + client behaviour with GQL payloads shaped like MANGA Plus (Suwayomi).

Live reference (same queries as ``sources/suwayomi.py``) was sampled against
``fetchSourceManga`` / ``fetchManga`` / ``fetchChapters`` / ``fetchChapterPages``
on MANGA Plus by SHUEISHA (EN); ids are fictional for the test doubles.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sources.suwayomi import SuwayomiClient, SuwayomiSource

# --- GQL ``data`` payloads (what ``SuwayomiClient._gql`` returns) ------------

_GQL_SEARCH_ONE_PIECE = {
    "fetchSourceManga": {
        "mangas": [
            {
                "id": 1,
                "title": "One Piece",
                "thumbnailUrl": "/api/v1/manga/1/thumbnail",
            }
        ],
        "hasNextPage": False,
    }
}

_GQL_FETCH_MANGA_ONE_PIECE = {
    "fetchManga": {
        "manga": {
            "id": 1,
            "title": "One Piece",
            "description": "As a child, Monkey D. Luffy…",
            "author": "Eiichiro Oda",
            "artist": "Eiichiro Oda",
            "genre": [
                "Simulrelease",
                "Serialization: Weekly Shounen Jump",
                "Schedule: Weekly",
                "Rating: Teen",
            ],
            "status": "ONGOING",
            "thumbnailUrl": "/api/v1/manga/1/thumbnail",
        }
    }
}

_GQL_FETCH_CHAPTERS_MPLUS = {
    "fetchChapters": {
        "chapters": [
            {
                "id": 1,
                "name": "#001 - Chapter 1: Romance Dawn",
                "chapterNumber": 1.0,
                "scanlator": "MANGA Plus",
                "uploadDate": "1547996400000",
                "sourceOrder": 1,
            },
            {
                "id": 4,
                "name": "#1180 - Chapter 1180: Omen",
                "chapterNumber": 1180.0,
                "scanlator": "MANGA Plus",
                "uploadDate": "1776610800000",
                "sourceOrder": 4,
            },
            {
                "id": 2,
                "name": "#002 - Chapter 2: They Call Him \u2018Straw Hat Luffy\u2019",
                "chapterNumber": 2.0,
                "scanlator": "MANGA Plus",
                "uploadDate": "1547996400000",
                "sourceOrder": 2,
            },
        ]
    }
}

_GQL_FETCH_PAGES = {
    "fetchChapterPages": {
        "pages": [
            "/api/v1/manga/1/chapter/1/page/0",
            "/api/v1/manga/1/chapter/1/page/1",
            "/api/v1/manga/1/chapter/1/page/2",
        ]
    }
}


def _gql_router(query: str, variables: dict | None = None):
    variables = variables or {}
    if "fetchSourceManga" in query:
        assert variables.get("query") == "One Piece"
        assert variables.get("page") == 1
        return _GQL_SEARCH_ONE_PIECE
    if "mutation FetchManga" in query:
        return _GQL_FETCH_MANGA_ONE_PIECE
    if "mutation FetchChapters" in query:
        assert variables.get("mangaId") == 1
        return _GQL_FETCH_CHAPTERS_MPLUS
    if "mutation FetchChapterPages" in query:
        assert variables.get("chapterId") == 1
        return _GQL_FETCH_PAGES
    raise AssertionError(f"unexpected GQL query: {query[:80]}…")


@pytest.fixture
def mplus_source():
    client = SuwayomiClient("http://suwayomi:4567")
    return SuwayomiSource(
        client,
        "1998944621602463790",
        "MANGA Plus by SHUEISHA (EN)",
        "en",
    )


def test_search_mangaplus_one_piece_thumbnail_proxy(mplus_source):
    with patch.object(SuwayomiClient, "_gql", side_effect=_gql_router):
        hits = mplus_source.search("One Piece")
    assert len(hits) == 1
    h = hits[0]
    assert h["title"] == "One Piece"
    assert h["manga_id"] == "1"
    assert h["source_key"] == "suwayomi:1998944621602463790"
    assert h["thumbnail_url"] == "/api/proxy/suwayomi/thumbnail/1"


def test_get_metadata_genre_list_and_status(mplus_source):
    with patch.object(SuwayomiClient, "_gql", side_effect=_gql_router):
        meta = mplus_source.get_metadata("1")
    assert meta["title"] == "One Piece"
    assert meta["status"] == "ongoing"
    assert "Simulrelease" in meta["tags"]
    assert meta["authors"] == ["Eiichiro Oda"]
    assert meta["artists"] == ["Eiichiro Oda"]


def test_iter_chapters_mplus_names_and_string_upload_ms(mplus_source):
    with patch.object(SuwayomiClient, "_gql", side_effect=_gql_router):
        chs = mplus_source.iter_chapters("1")
    by_num = {c["chapter_num"]: c for c in chs}
    assert by_num[1.0]["title"] == "Chapter 1: Romance Dawn"
    assert by_num[1.0]["group_name"] == "MANGA Plus"
    assert by_num[1.0]["publish_date"] == "2019-01-20"
    assert by_num[1180.0]["title"] == "Chapter 1180: Omen"
    assert by_num[1180.0]["publish_date"] == "2026-04-19"
    assert "\u2018Straw Hat" in (by_num[2.0]["title"] or "")


def test_parse_chapter_name_mangaplus_hash_style():
    from sources.suwayomi import _parse_chapter_name

    vol, ch, title = _parse_chapter_name("#1180 - Chapter 1180: Omen", 1180.0)
    assert vol is None
    assert ch == 1180.0
    assert title == "Chapter 1180: Omen"


def test_fetch_chapter_pages_rest_paths():
    client = SuwayomiClient("http://suwayomi:4567")
    with patch.object(SuwayomiClient, "_gql", return_value=_GQL_FETCH_PAGES):
        pages = client.fetch_chapter_pages(1)
    assert pages[0] == "/api/v1/manga/1/chapter/1/page/0"
    assert pages[-1] == "/api/v1/manga/1/chapter/1/page/2"
    assert all(p.startswith("/api/v1/manga/1/chapter/1/page/") for p in pages)
