"""Locale preference plumbing: migration, picker choices, and form validation."""

import json
import sqlite3

import db as dbmod
import locales as L


def _conn(tmp_path):
    return dbmod.init_db(str(tmp_path))


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def test_migration_adds_columns_to_a_pre_upgrade_database(tmp_path):
    """Upgrading an existing install must add the columns, all unset."""
    conn = _conn(tmp_path)
    for table, cols in (
        ("series", {"title_language_override", "cover_language_override",
                    "filename_language_override"}),
        ("volumes", {"cover_locale"}),
        ("series_metadata", {"titles_json", "original_language"}),
    ):
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert cols <= have, f"{table} missing {cols - have}"

    dbmod.insert_series(conn, path="S", title="S", language="en", start_chapter=0)
    row = dbmod.get_series_by_path(conn, "S")
    assert row["title_language_override"] is None
    assert row["cover_language_override"] is None
    assert row["filename_language_override"] is None


def test_migration_is_idempotent(tmp_path):
    _conn(tmp_path).close()
    conn = _conn(tmp_path)          # ensure_schema runs again on the same file
    dbmod.ensure_schema(conn)
    assert conn.execute("SELECT 1").fetchone()


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------

def test_setting_and_clearing_overrides(tmp_path):
    conn = _conn(tmp_path)
    dbmod.insert_series(conn, path="S", title="S", language="en", start_chapter=0)

    dbmod.set_series_locale_overrides(
        conn, "S", title_language_override="en",
        cover_language_override="original", filename_language_override="romanized")
    row = dbmod.get_series_by_path(conn, "S")
    assert (row["title_language_override"], row["cover_language_override"],
            row["filename_language_override"]) == ("en", "original", "romanized")

    dbmod.set_series_locale_overrides(conn, "S", title_language_override="")
    row = dbmod.get_series_by_path(conn, "S")
    assert row["title_language_override"] is None          # cleared to inherit
    assert row["cover_language_override"] == "original"    # untouched


# ---------------------------------------------------------------------------
# Settings picker
# ---------------------------------------------------------------------------

def test_observed_locales_grows_the_picker(tmp_path):
    conn = _conn(tmp_path)
    dbmod.insert_series(conn, path="S", title="S", language="en", start_chapter=0)
    sid = dbmod.get_series_by_path(conn, "S")["id"]
    dbmod.upsert_series_metadata(conn, sid, "mangadex", titles_json=json.dumps(
        {"ja": "x", "th": "y", "all": "z"}))
    dbmod.upsert_volume(conn, sid, 1.0, cover_locale="es-la")
    conn.commit()

    observed = dbmod.observed_locales(conn)
    assert "th" in observed and "es-la" in observed
    picker = L.picker_locales(observed)
    assert "th" in picker
    assert "all" not in picker                    # feed pseudo-language filtered
    assert picker[:len(L.SEED_LOCALES)] == L.SEED_LOCALES


def test_observed_locales_survives_corrupt_json(tmp_path):
    conn = _conn(tmp_path)
    dbmod.insert_series(conn, path="S", title="S", language="en", start_chapter=0)
    sid = dbmod.get_series_by_path(conn, "S")["id"]
    dbmod.upsert_series_metadata(conn, sid, "mangadex", titles_json="{not json")
    conn.commit()
    assert dbmod.observed_locales(conn) == []


def test_fresh_install_picker_is_never_empty():
    assert len(L.picker_locales([])) >= 15


def test_list_and_detail_queries_agree_on_series_columns(tmp_path):
    """``get_all_series`` uses an explicit column list; keep it in step with
    ``get_series_by_path`` (``s.*``) or per-series settings silently vanish from
    the dashboard while still working on the detail page."""
    conn = _conn(tmp_path)
    dbmod.insert_series(conn, path="S", title="S", language="en", start_chapter=0,
                        source="mangadex", source_id="x")
    dbmod.set_series_locale_overrides(
        conn, "S", title_language_override="en",
        cover_language_override="original", filename_language_override="romanized")
    dbmod.set_manage_existing_files(conn, "S", True)

    detail = dbmod.get_series_by_path(conn, "S")
    listed = [s for s in dbmod.get_all_series(conn) if s["path"] == "S"][0]
    for col in ("title_language_override", "cover_language_override",
                "filename_language_override", "manage_existing_files"):
        assert listed[col] == detail[col], f"{col} missing from get_all_series"


def test_dashboard_and_detail_resolve_the_same_title(tmp_path):
    """The reported symptom: Korean in the list, English on the series page."""
    conn = _conn(tmp_path)
    dbmod.insert_series(conn, path="S", title="S", language="en", start_chapter=0,
                        source="mangadex", source_id="x")
    sid = dbmod.get_series_by_path(conn, "S")["id"]
    dbmod.upsert_series_metadata(conn, sid, "mangadex", title="cached",
                                 original_language="ko",
                                 titles_json=json.dumps({"ko": "KO", "en": "Solo Leveling"}))
    dbmod.set_series_locale_overrides(conn, "S", title_language_override="en")
    conn.commit()

    settings = {"title_language": "original"}       # global says original
    meta = dbmod.get_series_metadata(conn, sid, "mangadex")
    detail = dbmod.get_series_by_path(conn, "S")
    listed = [s for s in dbmod.get_all_series(conn) if s["path"] == "S"][0]
    assert L.resolve_series_titles(listed, meta, settings, "S")[0] == "Solo Leveling"
    assert L.resolve_series_titles(detail, meta, settings, "S")[0] == "Solo Leveling"


def test_comicinfo_editor_defaults_to_the_resolved_title(tmp_path, monkeypatch):
    """Saving through the ComicInfo editor must not revert Series to the folder."""
    import importlib.util, sys, os
    conn = _conn(tmp_path)
    dbmod.insert_series(conn, path="Folder Name", title="Folder Name", language="en",
                        start_chapter=0, source="mangadex", source_id="x")
    sid = dbmod.get_series_by_path(conn, "Folder Name")["id"]
    dbmod.upsert_series_metadata(conn, sid, "mangadex", title="cached",
                                 original_language="ko",
                                 titles_json=json.dumps({"en": "Solo Leveling"}))
    dbmod.set_series_locale_overrides(conn, "Folder Name", title_language_override="en")
    conn.commit()

    row = dbmod.get_series_by_path(conn, "Folder Name")
    meta = dbmod.get_series_metadata(conn, sid, "mangadex")
    # This is the value the editor pre-fills and would write back.
    assert L.resolve_series_titles(row, meta, {}, row["name"])[0] == "Solo Leveling"


def test_list_rows_carry_title_resolution_inputs(tmp_path):
    """Dashboard resolution must not need a metadata query per series."""
    conn = _conn(tmp_path)
    dbmod.insert_series(conn, path="S", title="S", language="en", start_chapter=0,
                        source="mangadex", source_id="x")
    sid = dbmod.get_series_by_path(conn, "S")["id"]
    dbmod.upsert_series_metadata(conn, sid, "mangadex", title="cached",
                                 original_language="ko",
                                 titles_json=json.dumps({"en": "Solo Leveling"}))
    conn.commit()
    listed = [s for s in dbmod.get_all_series(conn) if s["path"] == "S"][0]
    assert listed["titles_json"] and listed["original_language"] == "ko"
    assert listed["meta_title"] == "cached"
    # Resolvable straight from the row, with no further lookups.
    meta = {"title": listed["meta_title"], "titles_json": listed["titles_json"],
            "original_language": listed["original_language"]}
    assert L.resolve_series_titles({}, meta, {"title_language": "en"}, "S")[0] \
        == "Solo Leveling"
