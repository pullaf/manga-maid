"""Tests for manga-sync.py - imported as manga_sync via conftest."""
import json
import os
from unittest.mock import MagicMock, patch

import manga_sync


def test_companion_mangadex_id_is_shared_for_suwayomi_features():
    assert manga_sync.mangadex_id_for_series({
        "source_name": "suwayomi:123",
        "source_id": "456",
        "mangadex_id": "mdx-uuid",
    }) == "mdx-uuid"


def test_primary_mangadex_id_is_shared_for_mangadex_features():
    assert manga_sync.mangadex_id_for_series({
        "source_name": "mangadex",
        "source_id": "mdx-uuid",
        "mangadex_id": "mdx-uuid",
    }) == "mdx-uuid"


def test_suwayomi_companion_is_eligible_for_download_volume_remap():
    series = {
        "id": 7,
        "source_name": "suwayomi:123",
        "source_id": "456",
        "mangadex_id": "mdx-uuid",
    }
    assert manga_sync._aggregate_remap_mdx_id(
        series, [{"id": 1}], MagicMock(), 7
    ) == "mdx-uuid"


def test_suwayomi_without_companion_skips_mangadex_volume_remap():
    series = {
        "id": 7,
        "source_name": "suwayomi:123",
        "source_id": "456",
        "mangadex_id": None,
    }
    assert manga_sync._aggregate_remap_mdx_id(
        series, [{"id": 1}], MagicMock(), 7
    ) is None


def test_suwayomi_empty_collection_error_is_actionable_and_short():
    raw = RuntimeError(
        "Suwayomi GQL error: [{'message': 'Exception while fetching data "
        "(/fetchChapters) : Collection is empty.\\njava.util.NoSuchElementException'}]"
    )
    summary = manga_sync._friendly_sync_error(raw)
    assert "source returned an empty manga collection" in summary
    assert "java.util" not in summary
    assert len(summary) < 300


def test_aggregate_language_fallback_fills_only_missing_chapters():
    from sources.mangadex import MangaDexSource

    preferred = {"result": "ok", "volumes": {
        "none": {"volume": "none", "chapters": {"20": {"chapter": "20"}}},
        "1": {"volume": "1", "chapters": {"1": {"chapter": "1", "id": "preferred"}}},
    }}
    all_languages = {"result": "ok", "volumes": {
        "2": {"volume": "2", "chapters": {
            "1": {"chapter": "1", "id": "fallback-conflict"},
            "20": {"chapter": "20", "id": "fallback-fill"},
        }},
        "3": {"volume": "3", "chapters": {"30": {"chapter": "30"}}},
    }}
    source = MangaDexSource()
    with patch.object(source, "get_aggregate", side_effect=[preferred, all_languages]):
        merged = source.get_aggregate_with_language_fallback("id", "en")

    assert merged["volumes"]["1"]["chapters"]["1"]["id"] == "preferred"
    assert "1" not in merged["volumes"]["2"]["chapters"]
    assert merged["volumes"]["2"]["chapters"]["20"]["id"] == "fallback-fill"
    assert "30" in merged["volumes"]["3"]["chapters"]


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
    assert covers["1"][0] == "https://uploads.mangadex.org/covers/manga-id/cover1.jpg.512.jpg"
    assert covers["2"][0] == "https://uploads.mangadex.org/covers/manga-id/cover2.jpg.512.jpg"
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


# ---------------------------------------------------------------------------
# Cover locale selection
# ---------------------------------------------------------------------------

def _cover_page(entries):
    return {"data": [{"attributes": a} for a in entries], "total": len(entries)}


# Mirrors the real shape: every volume localized except the newest, which is
# still original-language only until a translated edition ships.
_MIXED_LOCALE_COVERS = _cover_page([
    {"volume": "1", "fileName": "v1-ja.jpg",   "locale": "ja"},
    {"volume": "1", "fileName": "v1-en.jpg",   "locale": "en"},
    {"volume": "1", "fileName": "v1-esla.jpg", "locale": "es-la"},
    {"volume": "2", "fileName": "v2-ja.jpg",   "locale": "ja"},
])


def _covers_with(preference, original_language="ja", data=None):
    from sources.mangadex import MangaDexSource
    with patch.object(MangaDexSource, "_api_get",
                      return_value=data or _MIXED_LOCALE_COVERS):
        return manga_sync.fetch_volume_covers("mid", preference, original_language)


def test_cover_preference_picks_requested_locale():
    covers = _covers_with("en")
    assert covers["1"][1] == "en"
    assert covers["1"][0].endswith("v1-en.jpg.512.jpg")


def test_cover_preference_falls_back_per_volume():
    """A localized series can still have an original-language newest volume."""
    covers = _covers_with("en")
    assert covers["2"][1] == "ja"
    assert covers["2"][0].endswith("v2-ja.jpg.512.jpg")


def test_cover_original_preference():
    covers = _covers_with("original")
    assert covers["1"][1] == "ja"
    assert covers["2"][1] == "ja"


def test_cover_selection_is_deterministic_regardless_of_api_order():
    """Previously last-writer-wins: whichever locale the API listed last won."""
    reversed_page = {"data": list(reversed(_MIXED_LOCALE_COVERS["data"])), "total": 4}
    assert _covers_with("en")["1"] == _covers_with("en", data=reversed_page)["1"]


def test_cover_variants_keep_every_locale():
    from sources.mangadex import MangaDexSource
    with patch.object(MangaDexSource, "_api_get", return_value=_MIXED_LOCALE_COVERS):
        variants = MangaDexSource().get_volume_cover_variants("mid")
    assert set(variants["1"]) == {"ja", "en", "es-la"}
    assert set(variants["2"]) == {"ja"}


# ---------------------------------------------------------------------------
# Title resolution / upgrade safety
# ---------------------------------------------------------------------------

# A cached series whose stored title is the romanization but whose pool has a
# very different English alt title - the shape that would rename a library on
# upgrade if unset preferences re-resolved instead of reusing the cached title.
_META = {
    "title": "Na Honjaman Level-Up",
    "original_language": "ko",
    "titles_json": json.dumps({
        "ko-ro": "Na Honjaman Level-Up",
        "ko":    "나 혼자만 레벨업",
        "en":    "Solo Leveling",
    }),
}


def _titles(series_row=None, settings=None):
    return manga_sync.resolve_series_titles(
        series_row or {}, _META, settings or {}, "Folder Name")


def test_upgrade_with_nothing_configured_changes_nothing():
    display, filename = _titles()
    assert display == "Na Honjaman Level-Up"
    assert filename == "Na Honjaman Level-Up"


def test_global_preference_applies_once_chosen():
    display, filename = _titles(settings={"title_language": "en"})
    assert display == "Solo Leveling"
    assert filename == "Solo Leveling"   # filename follows the title by default


def test_series_override_beats_global():
    display, _ = _titles({"title_language_override": "original"},
                         {"title_language": "en"})
    assert display == "나 혼자만 레벨업"


def test_filename_can_stay_latin_while_title_is_native():
    """The point of splitting the axes: no hangul on disk, native in Kavita."""
    display, filename = _titles(
        settings={"title_language": "original", "filename_language": "romanized"})
    assert display == "나 혼자만 레벨업"
    assert filename == "Na Honjaman Level-Up"


def test_unknown_locale_falls_back_rather_than_blanking():
    display, _ = _titles(settings={"title_language": "sv"})
    assert display == "나 혼자만 레벨업"      # falls back to the original language


def test_missing_pool_falls_back_to_cached_title():
    """Series linked before the upgrade have no titles_json until refetched."""
    meta = {"title": "Cached Title"}
    assert manga_sync.resolve_series_titles({}, meta, {"title_language": "en"}, "x") \
        == ("Cached Title", "Cached Title")


# ---------------------------------------------------------------------------
# Retroactive volume rename
# ---------------------------------------------------------------------------

_VARIANTS = ["Na Honjaman Level-Up", "나 혼자만 레벨업", "Solo Leveling"]


def test_restem_swaps_only_the_title():
    """Group tags and volume numbers must survive a locale rename untouched."""
    assert manga_sync._restem_with_title(
        "[en Group] Na Honjaman Level-Up vol.1", _VARIANTS, "Solo Leveling") \
        == "[en Group] Solo Leveling vol.1"


def test_restem_leaves_unrecognised_stems_alone():
    """A manually renamed file is not rebuilt from the template."""
    assert manga_sync._restem_with_title(
        "something the user named themselves", _VARIANTS, "Solo Leveling") is None


def test_restem_is_a_noop_when_already_correct():
    assert manga_sync._restem_with_title(
        "[en] Solo Leveling vol.1", _VARIANTS, "Solo Leveling") is None


def test_restem_prefers_the_longest_matching_variant():
    """A short title must not half-replace a longer one containing it."""
    variants = sorted(["Abyss", "Made in Abyss"], key=len, reverse=True)
    assert manga_sync._restem_with_title("Made in Abyss vol.1", variants, "X") \
        == "X vol.1"


def test_restem_sanitises_the_replacement():
    out = manga_sync._restem_with_title("Solo Leveling vol.1", ["Solo Leveling"],
                                        "A/B:C")
    assert "/" not in out and ":" not in out


def test_rename_is_gated_on_an_explicit_choice():
    """Nothing configured: an upgrade must never rename a library."""
    assert not manga_sync._has_explicit_filename_locale({}, {})
    assert not manga_sync._has_explicit_filename_locale(
        {}, {"title_language": "", "filename_language": ""})
    # A choice at either level, on either axis, enables it.
    assert manga_sync._has_explicit_filename_locale({}, {"title_language": "en"})
    assert manga_sync._has_explicit_filename_locale({}, {"filename_language": "romanized"})
    assert manga_sync._has_explicit_filename_locale(
        {"title_language_override": "en"}, {})
    assert manga_sync._has_explicit_filename_locale(
        {"filename_language_override": "ja-ro"}, {})


def test_excluded_series_are_never_renamed(tmp_path):
    assert manga_sync._rename_volume_stems_for_locale(
        None, {"exclude_from_fix": 1, "id": 1, "path": "x"},
        {"title_language": "en"}, _META, "label") == 0


# ---------------------------------------------------------------------------
# Cover re-embedding
# ---------------------------------------------------------------------------

def _volume_cbz(path, cover=b"OLDCOVER", with_cover=True):
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        if with_cover:
            z.writestr("0000.jpg", cover)
        z.writestr("0001.jpg", b"page1")
        z.writestr("ComicInfo.xml", "<ComicInfo><Series>S</Series></ComicInfo>")
    return path


def test_replace_volume_cover_swaps_only_the_cover(tmp_path):
    import zipfile
    from comicinfo import replace_volume_cover
    p = _volume_cbz(str(tmp_path / "v1.cbz"))
    assert replace_volume_cover(p, b"NEWCOVER")
    with zipfile.ZipFile(p) as z:
        assert z.read("0000.jpg") == b"NEWCOVER"
        assert z.read("0001.jpg") == b"page1"          # pages untouched
        assert "ComicInfo.xml" in z.namelist()         # metadata untouched
        assert z.namelist() == ["0000.jpg", "0001.jpg", "ComicInfo.xml"]


def test_replace_volume_cover_declines_to_insert_by_default(tmp_path):
    import zipfile
    from comicinfo import replace_volume_cover
    p = _volume_cbz(str(tmp_path / "v1.cbz"), with_cover=False)
    assert replace_volume_cover(p, b"NEW") is False
    with zipfile.ZipFile(p) as z:
        assert "0000.jpg" not in z.namelist()


def test_replace_volume_cover_can_insert_when_explicitly_asked(tmp_path):
    """Capability retained, but the sync path deliberately never uses it."""
    import zipfile
    from comicinfo import replace_volume_cover
    p = _volume_cbz(str(tmp_path / "v1.cbz"), with_cover=False)
    assert replace_volume_cover(p, b"NEWCOVER", insert_if_missing=True)
    with zipfile.ZipFile(p) as z:
        assert z.namelist()[0] == "0000.jpg"      # sorts ahead of the pages
        assert z.read("0000.jpg") == b"NEWCOVER"
        assert z.read("0001.jpg") == b"page1"
        assert "ComicInfo.xml" in z.namelist()


def test_insert_does_not_duplicate_an_existing_cover_slot(tmp_path):
    import zipfile
    from comicinfo import replace_volume_cover
    p = _volume_cbz(str(tmp_path / "v1.cbz"))
    assert replace_volume_cover(p, b"NEWCOVER", insert_if_missing=True)
    with zipfile.ZipFile(p) as z:
        assert z.namelist().count("0000.jpg") == 1
        assert z.read("0000.jpg") == b"NEWCOVER"


def test_replace_volume_cover_is_safe_on_missing_or_empty(tmp_path):
    from comicinfo import replace_volume_cover
    assert replace_volume_cover(str(tmp_path / "nope.cbz"), b"X") is False
    assert replace_volume_cover(_volume_cbz(str(tmp_path / "v.cbz")), b"") is False


def test_store_volume_covers_reports_only_real_changes(tmp_path):
    import db as dbmod
    conn = dbmod.init_db(str(tmp_path))
    dbmod.insert_series(conn, path="S", title="S", language="en", start_chapter=0)
    sid = dbmod.get_series_by_path(conn, "S")["id"]

    first = manga_sync._store_volume_covers(conn, sid, {"1": ("http://a", "ja")})
    assert first == [1.0]            # imported volume gaining its first cover
    same = manga_sync._store_volume_covers(conn, sid, {"1": ("http://a", "ja")})
    assert same == []                                     # idempotent
    changed = manga_sync._store_volume_covers(conn, sid, {"1": ("http://b", "en")})
    assert changed == [1.0]                               # locale upgraded
    assert dbmod.get_volume(conn, sid, 1.0)["cover_locale"] == "en"


def test_reembed_rewrites_covers_for_changed_volumes(tmp_path, monkeypatch):
    import zipfile
    import db as dbmod
    conn = dbmod.init_db(str(tmp_path))
    root = tmp_path / "manga"; (root / "S").mkdir(parents=True)
    monkeypatch.setattr(manga_sync, "MANGA_ROOT", str(root))
    monkeypatch.setattr(manga_sync, "_download_cover", lambda url: b"NEWCOVER")

    dbmod.insert_series(conn, path="S", title="S", language="en", start_chapter=0)
    sid = dbmod.get_series_by_path(conn, "S")["id"]
    cbz = _volume_cbz(str(root / "S" / "v1.cbz"))
    dbmod.upsert_volume(conn, sid, 1.0, path="S/v1.cbz", origin="merged",
                        cover_url="http://new", cover_locale="en")
    conn.commit()

    assert manga_sync._reembed_volume_covers(conn, sid, [1.0], {}, "S") == 1
    with zipfile.ZipFile(cbz) as z:
        assert z.read("0000.jpg") == b"NEWCOVER"


def test_reembed_skips_volumes_with_no_file_on_disk(tmp_path, monkeypatch):
    import db as dbmod
    conn = dbmod.init_db(str(tmp_path))
    monkeypatch.setattr(manga_sync, "MANGA_ROOT", str(tmp_path / "manga"))
    monkeypatch.setattr(manga_sync, "_download_cover", lambda url: b"X")
    dbmod.insert_series(conn, path="S", title="S", language="en", start_chapter=0)
    sid = dbmod.get_series_by_path(conn, "S")["id"]
    dbmod.upsert_volume(conn, sid, 1.0, path="S/gone.cbz", cover_url="http://x")
    conn.commit()
    assert manga_sync._reembed_volume_covers(conn, sid, [1.0], {}, "S") == 0


def test_reembed_survives_a_failed_download(tmp_path, monkeypatch):
    import db as dbmod
    conn = dbmod.init_db(str(tmp_path))
    root = tmp_path / "manga"; (root / "S").mkdir(parents=True)
    monkeypatch.setattr(manga_sync, "MANGA_ROOT", str(root))
    monkeypatch.setattr(manga_sync, "_download_cover", lambda url: None)
    dbmod.insert_series(conn, path="S", title="S", language="en", start_chapter=0)
    sid = dbmod.get_series_by_path(conn, "S")["id"]
    _volume_cbz(str(root / "S" / "v1.cbz"))
    dbmod.upsert_volume(conn, sid, 1.0, path="S/v1.cbz", cover_url="http://x")
    conn.commit()
    assert manga_sync._reembed_volume_covers(conn, sid, [1.0], {}, "S") == 0


# ---------------------------------------------------------------------------
# Chapter filenames follow the same setting as volumes
# ---------------------------------------------------------------------------

def test_chapter_filename_title_respects_the_gate():
    """Unset: keep the long-standing folder-name behaviour."""
    assert manga_sync._filename_title_for({}, _META, {}, "Folder Name") == "Folder Name"


def test_chapter_filename_title_uses_the_chosen_locale():
    assert manga_sync._filename_title_for(
        {}, _META, {"title_language": "en"}, "Folder Name") == "Solo Leveling"
    assert manga_sync._filename_title_for(
        {}, _META, {"filename_language": "romanized"}, "Folder Name") \
        == "Na Honjaman Level-Up"


# ---------------------------------------------------------------------------
# Suwayomi + MangaDex companion
# ---------------------------------------------------------------------------

class _FakeSuwayomiSource:
    """Stands in for a non-MangaDex source: one title, no locale metadata."""
    def get_metadata(self, manga_id):
        return {
            "title": "Solo Leveling Ragnarok", "description": None, "tags": [],
            "authors": [], "artists": [], "year": None, "status": None,
            "content_rating": None, "total_volumes": 0, "cover_filename": None,
            "titles": {"en": "Solo Leveling Ragnarok"},
            "original_language": None, "available_locales": [],
        }


_MDX_COMPANION_META = {
    "title": "x", "description": None, "tags": [], "authors": [], "artists": [],
    "year": None, "status": None, "content_rating": None, "total_volumes": 0,
    "cover_filename": None,
    "titles": {"ko-ro": "Na Honjaman Level-Up", "ko": "나 혼자만 레벨업",
               "en": "Solo Leveling"},
    "original_language": "ko", "available_locales": ["ko-ro", "ko", "en"],
}


def _companion_series(tmp_path):
    import db as dbmod
    conn = dbmod.init_db(str(tmp_path))
    dbmod.insert_series(conn, path="S", title="S", language="en", start_chapter=0,
                        source="suwayomi:1", source_id="42")
    conn.execute("UPDATE series SET mangadex_id=? WHERE path='S'", ("mdx-uuid",))
    conn.commit()
    return conn, dbmod.get_series_by_path(conn, "S")


def test_companion_supplies_locales_for_a_suwayomi_series(tmp_path):
    """MangaDex linked for metadata must drive title locales, not just covers."""
    from sources.mangadex import MangaDexSource
    conn, row = _companion_series(tmp_path)
    with patch.object(MangaDexSource, "get_metadata", return_value=_MDX_COMPANION_META):
        meta = manga_sync._fetch_and_cache_meta(
            _FakeSuwayomiSource(), "42", row["id"], "suwayomi:1", "en", conn,
            row, {})
    assert meta["original_language"] == "ko"
    assert json.loads(meta["titles_json"])["en"] == "Solo Leveling"

    disp, _ = manga_sync.resolve_series_titles(row, meta, {"title_language": "en"}, "S")
    assert disp == "Solo Leveling"
    disp, _ = manga_sync.resolve_series_titles(row, meta, {"title_language": "original"}, "S")
    assert disp == "나 혼자만 레벨업"


def test_companion_covers_get_the_right_original_language(tmp_path):
    """Without this the 'original' cover preference degrades to English."""
    from sources.mangadex import MangaDexSource
    conn, row = _companion_series(tmp_path)
    with patch.object(MangaDexSource, "get_metadata", return_value=_MDX_COMPANION_META):
        manga_sync._fetch_and_cache_meta(
            _FakeSuwayomiSource(), "42", row["id"], "suwayomi:1", "en", conn, row, {})
    pref, orig = manga_sync._cover_args(conn, row, {"cover_language": "original"})
    assert (pref, orig) == ("original", "ko")


def test_suwayomi_without_a_companion_still_works(tmp_path):
    """No companion: every preference collapses to the single available title."""
    conn, row = _companion_series(tmp_path)
    conn.execute("UPDATE series SET mangadex_id=NULL WHERE path='S'"); conn.commit()
    row = dict(row); row["mangadex_id"] = None
    meta = manga_sync._fetch_and_cache_meta(
        _FakeSuwayomiSource(), "42", row["id"], "suwayomi:1", "en", conn, row, {})
    disp, fname = manga_sync.resolve_series_titles(
        row, meta, {"title_language": "ja"}, "S")
    assert disp == fname == "Solo Leveling Ragnarok"


def test_cover_rewrites_need_an_explicit_choice():
    """Upgrading must not insert or replace covers in archives already on disk."""
    assert not manga_sync._has_explicit_cover_locale({}, {})
    assert not manga_sync._has_explicit_cover_locale({}, {"cover_language": ""})
    assert manga_sync._has_explicit_cover_locale({}, {"cover_language": "original"})
    assert manga_sync._has_explicit_cover_locale({}, {"cover_language": "en"})
    assert manga_sync._has_explicit_cover_locale(
        {"cover_language_override": "ja"}, {})


def test_unset_cover_language_still_resolves_for_new_merges(tmp_path):
    """Unset means 'do not rewrite', not 'have no preference'."""
    import db as dbmod
    conn = dbmod.init_db(str(tmp_path))
    dbmod.insert_series(conn, path="S", title="S", language="en", start_chapter=0,
                        source="mangadex", source_id="x")
    row = dbmod.get_series_by_path(conn, "S")
    dbmod.upsert_series_metadata(conn, row["id"], "mangadex", original_language="ja")
    conn.commit()
    pref, orig = manga_sync._cover_args(conn, row, {"cover_language": ""})
    assert (pref, orig) == ("original", "ja")


# ---------------------------------------------------------------------------
# Cover art published after the volume was merged
# ---------------------------------------------------------------------------

def _series_with_volume(tmp_path, monkeypatch, cover_locale=None, cover_url=None):
    import db as dbmod
    conn = dbmod.init_db(str(tmp_path))
    root = tmp_path / "manga"; (root / "S").mkdir(parents=True)
    monkeypatch.setattr(manga_sync, "MANGA_ROOT", str(root))
    dbmod.insert_series(conn, path="S", title="S", language="en", start_chapter=0,
                        source="mangadex", source_id="mid")
    sid = dbmod.get_series_by_path(conn, "S")["id"]
    _volume_cbz(str(root / "S" / "v1.cbz"), cover=b"FIRSTPAGE")
    dbmod.upsert_volume(conn, sid, 1.0, path="S/v1.cbz", origin="merged",
                        cover_locale=cover_locale, cover_url=cover_url)
    conn.commit()
    return conn, sid, str(root / "S" / "v1.cbz")


def _late_cover_arrives(conn, sid, monkeypatch, explicit=False):
    monkeypatch.setattr(manga_sync, "fetch_volume_covers",
                        lambda *a, **k: {"1": ("http://real", "ja")})
    monkeypatch.setattr(manga_sync, "_download_cover", lambda url: b"REALCOVER")
    return manga_sync._ensure_volume_cover_urls(
        conn, sid, "mid", "original", "ja", {}, "S", explicit)


def test_placeholder_cover_is_replaced_when_real_art_arrives(tmp_path, monkeypatch):
    """Volume merged before the cover was published, with nothing configured."""
    import zipfile
    conn, sid, cbz = _series_with_volume(
        tmp_path, monkeypatch, cover_locale=manga_sync.loc.PLACEHOLDER_COVER)
    _late_cover_arrives(conn, sid, monkeypatch, explicit=False)
    with zipfile.ZipFile(cbz) as z:
        assert z.read("0000.jpg") == b"REALCOVER"


def test_real_cover_art_is_not_swapped_without_permission(tmp_path, monkeypatch):
    """An existing real cover is a user decision; leave it alone when unset."""
    import zipfile
    conn, sid, cbz = _series_with_volume(
        tmp_path, monkeypatch, cover_locale="en", cover_url="http://old")
    _late_cover_arrives(conn, sid, monkeypatch, explicit=False)
    with zipfile.ZipFile(cbz) as z:
        assert z.read("0000.jpg") == b"FIRSTPAGE"     # untouched


def test_real_cover_art_is_swapped_once_a_language_is_chosen(tmp_path, monkeypatch):
    import zipfile
    conn, sid, cbz = _series_with_volume(
        tmp_path, monkeypatch, cover_locale="en", cover_url="http://old")
    _late_cover_arrives(conn, sid, monkeypatch, explicit=True)
    with zipfile.ZipFile(cbz) as z:
        assert z.read("0000.jpg") == b"REALCOVER"


def test_placeholder_locale_is_never_selectable_as_cover_art():
    """'page' must not leak into pickers or resolution."""
    import locales as L
    assert not L.is_content_locale(L.PLACEHOLDER_COVER)
    assert L.resolve_locale([L.PLACEHOLDER_COVER], "original", "ja") is None
    assert L.PLACEHOLDER_COVER not in L.picker_locales([L.PLACEHOLDER_COVER])


def test_placeholder_is_cleared_once_real_art_lands(tmp_path, monkeypatch):
    import db as dbmod
    conn, sid, _ = _series_with_volume(
        tmp_path, monkeypatch, cover_locale=manga_sync.loc.PLACEHOLDER_COVER)
    _late_cover_arrives(conn, sid, monkeypatch, explicit=False)
    assert dbmod.get_volume(conn, sid, 1.0)["cover_locale"] == "ja"


def test_imported_archives_are_never_modified(tmp_path, monkeypatch):
    """No cover slot means we did not build it; leave the file untouched."""
    import zipfile
    import db as dbmod
    conn = dbmod.init_db(str(tmp_path))
    root = tmp_path / "manga"; (root / "S").mkdir(parents=True)
    monkeypatch.setattr(manga_sync, "MANGA_ROOT", str(root))
    monkeypatch.setattr(manga_sync, "_download_cover", lambda url: b"REALCOVER")
    dbmod.insert_series(conn, path="S", title="S", language="en", start_chapter=0)
    sid = dbmod.get_series_by_path(conn, "S")["id"]
    cbz = _volume_cbz(str(root / "S" / "v1.cbz"), with_cover=False)
    dbmod.upsert_volume(conn, sid, 1.0, path="S/v1.cbz", cover_url="http://real")
    conn.commit()

    assert manga_sync._reembed_volume_covers(conn, sid, [1.0], {}, "S") == 0
    with zipfile.ZipFile(cbz) as z:
        assert z.namelist() == ["0001.jpg", "ComicInfo.xml"]   # byte-for-byte layout


# ---------------------------------------------------------------------------
# Files that were already in the folder
# ---------------------------------------------------------------------------

def _mixed_library(tmp_path, monkeypatch, manage_existing=0):
    """The common shape: volumes the user owns, plus chapters we fetched."""
    import db as dbmod
    conn = dbmod.init_db(str(tmp_path))
    root = tmp_path / "manga"; (root / "S").mkdir(parents=True)
    monkeypatch.setattr(manga_sync, "MANGA_ROOT", str(root))
    dbmod.insert_series(conn, path="S", title="S", language="en", start_chapter=0,
                        source="mangadex", source_id="mid")
    sid = dbmod.get_series_by_path(conn, "S")["id"]
    conn.execute("UPDATE series SET manage_existing_files=? WHERE id=?",
                 (manage_existing, sid))

    for n, origin in ((1.0, None), (2.0, None), (3.0, "merged")):
        name = f"Na Honjaman Level-Up vol.{int(n)}.cbz"
        (root / "S" / name).write_bytes(b"x")
        dbmod.upsert_volume(conn, sid, n, path=f"S/{name}", origin=origin)

    for n, status in ((10.0, "on_disk"), (11.0, "downloaded")):
        name = f"Na Honjaman Level-Up ch.{int(n)}.cbz"
        (root / "S" / name).write_bytes(b"x")
        dbmod.upsert_chapter(conn, sid, n, "mangadex", f"c{int(n)}",
                             path=f"S/{name}")
        conn.execute("UPDATE chapters SET status=? WHERE chapter_num=?", (status, n))
    conn.commit()
    dbmod.upsert_series_metadata(conn, sid, "mangadex", title="Na Honjaman Level-Up",
                                 original_language="ko",
                                 titles_json=_META["titles_json"])
    conn.commit()
    return conn, dbmod.get_series_by_path(conn, "S"), root / "S"


def _rename_all(conn, row, meta):
    for kind in ("volume", "chapter"):
        manga_sync._rename_stems_for_locale(
            conn, row, {"title_language": "en"}, meta, "S", kind=kind)


def test_renames_cover_pre_existing_files_too(tmp_path, monkeypatch):
    """Renames are reversible and logged, so a series is never half-renamed."""
    conn, row, d = _mixed_library(tmp_path, monkeypatch)
    _rename_all(conn, row, _META)
    assert all(p.name.startswith("Solo Leveling") for p in d.iterdir())


def test_renames_are_recorded_so_they_can_be_undone(tmp_path, monkeypatch):
    conn, row, d = _mixed_library(tmp_path, monkeypatch)
    _rename_all(conn, row, _META)
    logged = conn.execute(
        "SELECT old_path, new_path FROM rename_log WHERE reason LIKE '%_stem_locale'"
    ).fetchall()
    assert len(logged) == 5
    assert all(r["old_path"] and r["new_path"] for r in logged)


def test_merging_records_that_the_volume_is_ours(tmp_path):
    import db as dbmod
    conn = dbmod.init_db(str(tmp_path))
    dbmod.insert_series(conn, path="S", title="S", language="en", start_chapter=0)
    sid = dbmod.get_series_by_path(conn, "S")["id"]
    vid = dbmod.upsert_volume(conn, sid, 1.0)
    dbmod.mark_volume_merged(conn, vid, "S/v1.cbz", 123)
    assert dbmod.get_volume(conn, sid, 1.0)["origin"] == "merged"


def test_rows_predating_the_column_are_treated_as_not_ours(tmp_path, monkeypatch):
    """Safe direction: decline to rewrite rather than guess about old rows."""
    conn, row, d = _mixed_library(tmp_path, monkeypatch)
    stale = conn.execute(
        "SELECT id, volume_num AS num, path, origin FROM volumes WHERE volume_num=1"
    ).fetchone()
    assert not manga_sync._row_is_ours(stale, "volume")


def test_cover_writes_stay_inside_archives_we_built(tmp_path, monkeypatch):
    """Overwriting cover bytes we did not write cannot be undone."""
    import zipfile
    import db as dbmod
    conn = dbmod.init_db(str(tmp_path))
    root = tmp_path / "manga"; (root / "S").mkdir(parents=True)
    monkeypatch.setattr(manga_sync, "MANGA_ROOT", str(root))
    monkeypatch.setattr(manga_sync, "_download_cover", lambda url: b"REALCOVER")
    dbmod.insert_series(conn, path="S", title="S", language="en", start_chapter=0)
    sid = dbmod.get_series_by_path(conn, "S")["id"]

    theirs = _volume_cbz(str(root / "S" / "theirs.cbz"), cover=b"THEIRART")
    ours   = _volume_cbz(str(root / "S" / "ours.cbz"), cover=b"PLACEHOLDER")
    dbmod.upsert_volume(conn, sid, 1.0, path="S/theirs.cbz", cover_url="http://x")
    dbmod.upsert_volume(conn, sid, 2.0, path="S/ours.cbz", cover_url="http://x",
                        origin="merged")
    conn.commit()

    assert manga_sync._reembed_volume_covers(conn, sid, [1.0, 2.0], {}, "S") == 1
    assert zipfile.ZipFile(theirs).read("0000.jpg") == b"THEIRART"     # preserved
    assert zipfile.ZipFile(ours).read("0000.jpg") == b"REALCOVER"      # ours


def test_opting_in_allows_covering_imported_archives(tmp_path, monkeypatch):
    import zipfile
    import db as dbmod
    conn = dbmod.init_db(str(tmp_path))
    root = tmp_path / "manga"; (root / "S").mkdir(parents=True)
    monkeypatch.setattr(manga_sync, "MANGA_ROOT", str(root))
    monkeypatch.setattr(manga_sync, "_download_cover", lambda url: b"REALCOVER")
    dbmod.insert_series(conn, path="S", title="S", language="en", start_chapter=0)
    sid = dbmod.get_series_by_path(conn, "S")["id"]
    theirs = _volume_cbz(str(root / "S" / "theirs.cbz"), cover=b"THEIRART")
    dbmod.upsert_volume(conn, sid, 1.0, path="S/theirs.cbz", cover_url="http://x")
    conn.commit()

    assert manga_sync._reembed_volume_covers(
        conn, sid, [1.0], {}, "S", manage_all=True) == 1
    assert zipfile.ZipFile(theirs).read("0000.jpg") == b"REALCOVER"


def test_rename_clears_comicinfo_so_series_tag_is_rewritten(tmp_path, monkeypatch):
    """Renaming without this leaves the old <Series> in the file forever, since
    the ComicInfo pass only visits files flagged as missing it."""
    import db as dbmod
    conn, row, d = _mixed_library(tmp_path, monkeypatch)
    conn.execute("UPDATE volumes SET has_comicinfo=1")
    conn.execute("UPDATE chapters SET has_comicinfo=1")
    conn.commit()

    _rename_all(conn, row, _META)

    renamed_vols = conn.execute(
        "SELECT has_comicinfo FROM volumes WHERE path LIKE '%Solo Leveling%'"
    ).fetchall()
    renamed_chs = conn.execute(
        "SELECT has_comicinfo FROM chapters WHERE path LIKE '%Solo Leveling%'"
    ).fetchall()
    assert renamed_vols and all(r["has_comicinfo"] == 0 for r in renamed_vols)
    assert renamed_chs and all(r["has_comicinfo"] == 0 for r in renamed_chs)
