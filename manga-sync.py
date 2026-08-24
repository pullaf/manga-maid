#!/usr/bin/env python3
"""manga-sync - download new chapters, merge volumes, inject ComicInfo.xml."""

import contextlib
import errno
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from urllib import parse, request

MANGA_ROOT = os.environ.get("MANGA_ROOT", "/manga")
DATA_DIR   = os.environ.get("DATA_DIR",   "/data")
SYNC_LOG   = os.environ.get("SYNC_LOG",   "/data/logs/sync.log")
SYNC_LOG_MAX_LINES = 5000
try:
    MIN_MDX_ARCHIVE_BYTES = max(512, int(os.environ.get("MIN_MDX_ARCHIVE_BYTES", "4096")))
except (TypeError, ValueError):
    MIN_MDX_ARCHIVE_BYTES = 4096

_here = os.path.dirname(os.path.abspath(__file__))

# Import manga-fix.py (hyphen prevents normal import)
_spec = importlib.util.spec_from_file_location(
    "manga_fix", os.path.join(_here, "manga-fix.py")
)
_fix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fix)

CH_RE            = _fix.CH_RE
MANGA_EXTENSIONS = _fix.MANGA_EXTENSIONS

sys.path.insert(0, _here)
from db import (                                        # noqa: E402
    init_db, migrate_json_configs, scan_disk_series,
    get_all_series, get_series_by_path, preferred_groups_list_from_row,
    get_primary_source, update_source_sync_time,
    get_series_metadata, upsert_series_metadata, is_metadata_stale,
    upsert_volume, upsert_chapter, get_volume,
    get_chapters_for_volume, get_chapters_to_download,
    get_volumes_needing_compact,
    get_files_missing_comicinfo,
    mark_chapter_downloaded, mark_chapter_comicinfo,
    mark_volume_merged, mark_volume_comicinfo,
    assign_chapter_to_volume, apply_aggregate_volume_mapping,
    weekly_mdx_aggregate_volume_remap_due, touch_series_aggregate_volume_remap_at,
    scan_disk_files, log_rename, mangadex_id_for_series,
    set_series_sync_error, clear_series_sync_error,
)
from comicinfo import (                                 # noqa: E402
    replace_volume_cover,
    build_comicinfo_xml, count_pages, inject_comicinfo,
    merge_chapters_into_volume, read_first_image_bytes,
)
from sync_config import load_settings, sanitize_volume_naming  # noqa: E402
from kavita import KavitaClient                        # noqa: E402
from file_permissions import apply_file_permission_mask # noqa: E402
from naming import apply_naming_template, floor_int_str, format_num, safe_filename_token # noqa: E402
import locales as loc                                   # noqa: E402
from sources.mangadex import (                          # noqa: E402
    MangaDexSource, _ChapterData, MDEX_COVERS,
)
from sources.suwayomi import SuwayomiSource             # noqa: E402


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _rotate_log():
    try:
        with open(SYNC_LOG) as f:
            lines = f.readlines()
        if len(lines) > SYNC_LOG_MAX_LINES:
            with open(SYNC_LOG, "w") as f:
                f.writelines(lines[-SYNC_LOG_MAX_LINES:])
    except FileNotFoundError:
        pass


def _log(msg: str):
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    try:
        with open(SYNC_LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Source adapter factory
# ---------------------------------------------------------------------------

def _get_source(source_name: str):
    """Return the appropriate source adapter for a series_sources.source string."""
    if source_name == "mangadex" or not source_name:
        return MangaDexSource()
    if source_name.startswith("suwayomi:"):
        from sync_config import get_suwayomi_client
        client = get_suwayomi_client()
        if client is None:
            raise RuntimeError("Suwayomi source configured but suwayomi_url is not set in settings")
        suwayomi_source_id = source_name.split(":", 1)[1]
        # Resolve display name + lang from installed sources (best-effort)
        try:
            installed = {str(s["id"]): s for s in client.list_sources()}
            info = installed.get(suwayomi_source_id, {})
            source_display = info.get("displayName") or info.get("name") or source_name
            lang = info.get("lang") or "unknown"
        except Exception:
            source_display = source_name
            lang = "unknown"
        return SuwayomiSource(client, suwayomi_source_id, source_display, lang)
    return MangaDexSource()


_series_locale_prefs   = loc.series_locale_prefs
_cached_title_pool     = loc.cached_title_pool
resolve_series_titles  = loc.resolve_series_titles


def _series_display_title(conn, series_row: dict, settings: dict) -> str:
    """Resolved title for this series, matching ComicInfo ``Series``."""
    try:
        meta = get_series_metadata(
            conn, series_row["id"], series_row.get("source_name") or "mangadex") or {}
    except Exception:
        meta = {}
    fallback = series_row.get("name") or os.path.basename(series_row.get("path") or "")
    return resolve_series_titles(series_row, meta, settings, fallback)[0] or fallback


def _cover_args(conn, series_row: dict, settings: dict) -> tuple[str, str | None]:
    """``(cover preference, original language)`` for the Kavita/embed cover paths."""
    meta = {}
    try:
        meta = get_series_metadata(
            conn, series_row["id"], series_row.get("source_name") or "mangadex"
        ) or {}
    except Exception:
        pass
    pref = loc.effective_preference(
        series_row.get("cover_language_override"),
        (settings or {}).get("cover_language"), loc.ORIGINAL,
    )
    # Without an original language, ``original`` degrades to English - wrong for
    # a Suwayomi series whose covers come from a MangaDex companion.
    return pref, meta.get("original_language")


def fetch_volume_covers(
    manga_id: str, preference: str | None = None, original_language: str | None = None,
) -> dict[str, tuple[str, str | None]]:
    """Map volume number string → ``(cover URL, locale)``, for tests and tooling."""
    return MangaDexSource().get_volume_covers(manga_id, preference, original_language)


def _ensure_volume_cover_urls(
    conn, series_id: int, manga_id: str,
    preference: str | None = None, original_language: str | None = None,
    settings: dict | None = None, label: str = "", explicit: bool = False,
    manage_all: bool = False,
) -> list[float]:
    """Cache per-volume cover URLs (and the locale each came from) in the DB.

    Returns the volumes whose cover changed, and re-embeds them when ``settings``
    is supplied so archives already on disk do not keep a stale cover.
    """
    try:
        placeholders = _volumes_with_placeholder_covers(conn, series_id)
        covers  = fetch_volume_covers(manga_id, preference, original_language)
        changed = _store_volume_covers(conn, series_id, covers)
    except Exception:
        return []
    # Replacing our own placeholder needs no permission - real cover art simply
    # arrived after the volume was built. Swapping real art for other real art
    # is a user decision and stays gated.
    targets = changed if explicit else [v for v in changed if v in placeholders]
    if targets and settings is not None:
        try:
            _reembed_volume_covers(conn, series_id, targets, settings,
                                   label or "covers", manage_all)
        except Exception:
            pass
    return changed


def _volumes_with_placeholder_covers(conn, series_id: int) -> set[float]:
    """Volumes whose cover is the provisional first-page fallback."""
    return {
        r["volume_num"] for r in conn.execute(
            "SELECT volume_num FROM volumes WHERE series_id=? AND cover_locale=?",
            (series_id, loc.PLACEHOLDER_COVER),
        )
    }


def _store_volume_covers(conn, series_id: int, covers: dict) -> list[float]:
    """Persist cover URL + locale per volume; returns volumes whose cover changed.

    A volume whose cover only existed in a fallback locale is upgraded here once
    the preferred locale is uploaded - the stored ``cover_locale`` is what makes
    that detectable without re-downloading every cover. The returned list drives
    re-embedding, so an archive already on disk does not keep a stale cover.
    """
    changed: list[float] = []
    for vol_str, entry in (covers or {}).items():
        url, locale = entry if isinstance(entry, tuple) else (entry, None)
        try:
            vol_num = float(vol_str)
        except (ValueError, TypeError):
            continue
        existing = get_volume(conn, series_id, vol_num) or {}
        # Includes the first-URL case: an imported archive has no stored cover
        # yet, and is exactly what a cover-language choice should fix.
        if existing.get("cover_url") != url:
            changed.append(vol_num)
        upsert_volume(conn, series_id, vol_num, cover_url=url, cover_locale=locale)
    conn.commit()
    return changed


def _download_cover(url: str) -> bytes | None:
    """Fetch a MangaDex cover; the CDN requires a Referer."""
    try:
        req = request.Request(url, headers={
            "Referer": "https://mangadex.org/", "User-Agent": "Mozilla/5.0",
        })
        return request.urlopen(req, timeout=15).read()
    except Exception:
        return None


def _reembed_volume_covers(
    conn, series_id: int, vol_nums, settings: dict, label: str,
    manage_all: bool = False,
) -> int:
    """Rewrite the embedded cover of volumes whose cover art changed.

    Covers are otherwise only written when a volume is first merged, so a
    language change or a fallback upgrade would update the DB and Kavita while
    the archive on disk kept the old image indefinitely.
    """
    n = 0
    for vol_num in vol_nums or []:
        row = get_volume(conn, series_id, vol_num) or {}
        rel, url = row.get("path"), row.get("cover_url")
        if not rel or not url:
            continue
        # Only ever overwrite cover bytes in archives we built. A placeholder
        # is a copy of page 0001, still present, and real cover art can be
        # re-downloaded - so nothing is lost. The original image inside an
        # imported archive cannot be recovered, so that needs explicit consent.
        if not manage_all and row.get("origin") != "merged":
            continue
        abs_path = os.path.join(MANGA_ROOT, rel)
        if not os.path.isfile(abs_path):
            continue
        data = _download_cover(url)
        if not data:
            continue
        # Archives we did not build nearly always carry their cover as page 1
        # already, so inserting one would duplicate it. Only add a cover slot
        # when the user has explicitly asked us to manage their existing files.
        if replace_volume_cover(abs_path, data,
                                file_permission_mask=settings.get("file_permission_mask"),
                                insert_if_missing=manage_all):
            try:
                upsert_volume(conn, series_id, vol_num,
                              file_size=os.path.getsize(abs_path))
                conn.commit()
            except OSError:
                pass
            n += 1
            _log(f"[{label}] cover re-embedded: vol.{format_num(vol_num)} "
                 f"({row.get('cover_locale') or 'unknown'})")
    return n


# ---------------------------------------------------------------------------
# Metadata fetch & cache
# ---------------------------------------------------------------------------

def _fetch_and_cache_meta(
    source: MangaDexSource, manga_id: str, series_id: int,
    source_key: str, language: str, conn,
    series_row: dict | None = None, settings: dict | None = None,
) -> dict:
    try:
        meta = source.get_metadata(manga_id)
        orig_lang = meta.get("original_language")

        # MangaDex: also cache per-volume cover URLs for the merge step
        if isinstance(source, MangaDexSource):
            try:
                cover_pref = loc.effective_preference(
                    (series_row or {}).get("cover_language_override"),
                    (settings or {}).get("cover_language"), loc.ORIGINAL,
                )
                changed = _store_volume_covers(conn, series_id, source.get_volume_covers(
                    manga_id, cover_pref, orig_lang))
                if changed and settings is not None and \
                        _has_explicit_cover_locale(series_row, settings):
                    _reembed_volume_covers(
                        conn, series_id, changed, settings,
                        (series_row or {}).get("name") or "covers",
                        _manages_existing_files(series_row))
            except Exception:
                pass

        # Non-MangaDex sources expose one title and no locale info. When a
        # MangaDex companion is linked for metadata, take the locale fields from
        # there so title/filename preferences work for Suwayomi series too.
        titles = meta.get("titles") or {}
        if not isinstance(source, MangaDexSource):
            companion = mangadex_id_for_series(series_row or {})
            if companion:
                try:
                    mdx_meta = MangaDexSource().get_metadata(companion)
                    if mdx_meta.get("titles"):
                        titles    = mdx_meta["titles"]
                        orig_lang = mdx_meta.get("original_language") or orig_lang
                except Exception as e:
                    _log(f"  [warn] companion metadata fetch failed: {e}")

        upsert_series_metadata(conn, series_id, source_key,
            titles_json=json.dumps(titles, ensure_ascii=False),
            original_language=orig_lang,
            title=meta["title"],
            description=meta["description"],
            tags=meta["tags"],
            authors=meta["authors"],
            artists=meta["artists"],
            year=meta["year"],
            status=meta["status"],
            content_rating=meta["content_rating"],
            total_volumes=meta["total_volumes"],
            cover_filename=meta["cover_filename"],
        )
    except Exception as e:
        _log(f"  [warn] metadata fetch failed: {e}")

    return get_series_metadata(conn, series_id, source_key) or {}


# ---------------------------------------------------------------------------
# Chapter feed → DB
# ---------------------------------------------------------------------------

def _series_preferred_groups(series_row: dict) -> list[str]:
    pg = series_row.get("preferred_groups")
    if pg is not None:
        return list(pg)
    return preferred_groups_list_from_row(series_row)


class SeriesSyncError(RuntimeError):
    """An actionable failure for one series; safe to show in the UI."""


def _friendly_sync_error(exc: Exception) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ")
    message = " ".join(message.split())
    if "Collection is empty" in message and "fetchChapters" in message:
        return (
            "Suwayomi could not fetch chapters because the source returned an empty manga "
            "collection. The extension/title may be unavailable or Suwayomi's local entry "
            "may need to be refreshed or re-added."
        )
    match = re.search(r"'message':\s*'([^']+)'", message)
    if match:
        message = match.group(1)
    message = re.sub(r"^Suwayomi GQL error:\s*", "", message).strip()
    return message[:500] or exc.__class__.__name__


def _sync_chapters_to_db(
    source, manga_id: str, language: str,
    preferred_groups: list[str], series_id: int, conn,
):
    """Fetch full chapter feed, pick best per chapter_num, upsert to DB."""
    if isinstance(source, MangaDexSource):
        _sync_chapters_mdx(source, manga_id, language, preferred_groups, series_id, conn)
    else:
        _sync_chapters_suwayomi(source, manga_id, preferred_groups, series_id, conn)


def _sync_chapters_mdx(
    source: MangaDexSource, manga_id: str, language: str,
    preferred_groups: list[str], series_id: int, conn,
):
    all_candidates: dict[float, list] = {}
    for item in source.iter_feed(manga_id, language, {"order[chapter]": "asc"}):
        ch_str = item["attributes"].get("chapter")
        if not ch_str:
            continue
        try:
            ch_num = float(ch_str)
        except ValueError:
            continue
        all_candidates.setdefault(ch_num, []).append((item["id"], _ChapterData(item)))

    for ch_num in sorted(all_candidates):
        candidates = all_candidates[ch_num]
        picked_id, picked = candidates[0]
        for pref in preferred_groups:
            low = pref.lower()
            found = None
            for cid, ch in candidates:
                if low in ch.group.lower():
                    found = (cid, ch)
                    break
            if found:
                picked_id, picked = found
                break

        # Volume is one per manga on MangaDex, but each chapter *entity* carries
        # ``attributes.volume``; the preferred scanlator upload can lag null while
        # another upload for the same chapter number already has it—reuse any.
        vol_str = picked.volume
        if not vol_str or str(vol_str).lower() in ("none", ""):
            for _cid, ch in candidates:
                vs = ch.volume
                if vs and str(vs).lower() not in ("none", ""):
                    vol_str = vs
                    break

        vol_id = None
        if vol_str and str(vol_str).lower() not in ("none", ""):
            try:
                vol_id = upsert_volume(conn, series_id, float(vol_str))
            except (ValueError, TypeError):
                pass

        # Never propagate per-chapter feed metadata onto a row whose file is
        # already on disk - ComicInfo for that file uses series-level info only.
        existing = conn.execute(
            "SELECT id, path, status FROM chapters"
            " WHERE series_id=? AND chapter_num=?",
            (series_id, ch_num),
        ).fetchone()
        if existing and (existing["path"] or existing["status"] == "on_disk"):
            if _junk_chapter_file_cleared(conn, existing):
                pass
            else:
                if vol_id is not None:
                    assign_chapter_to_volume(conn, existing["id"], vol_id)
                continue

        chapter_id = upsert_chapter(
            conn, series_id, ch_num,
            source="mangadex",
            source_chapter_id=picked_id,
            title=picked.title,
            group_name=picked.group,
            publish_date=picked.publish_date,
        )

        if vol_id is not None:
            assign_chapter_to_volume(conn, chapter_id, vol_id)

    conn.commit()


def _sync_chapters_suwayomi(
    source: SuwayomiSource, manga_id: str,
    preferred_groups: list[str], series_id: int, conn,
):
    chapters = source.iter_chapters(manga_id)

    for ch_info in sorted(chapters, key=lambda c: c["chapter_num"]):
        ch_num = ch_info["chapter_num"]

        vol_id = None
        if ch_info.get("volume_num") is not None:
            try:
                vol_id = upsert_volume(conn, series_id, ch_info["volume_num"])
            except (ValueError, TypeError):
                pass

        existing = conn.execute(
            "SELECT id, path, status FROM chapters"
            " WHERE series_id=? AND chapter_num=?",
            (series_id, ch_num),
        ).fetchone()
        if existing and (existing["path"] or existing["status"] == "on_disk"):
            if _junk_chapter_file_cleared(conn, existing):
                pass
            else:
                if vol_id is not None:
                    assign_chapter_to_volume(conn, existing["id"], vol_id)
                continue

        chapter_id = upsert_chapter(
            conn, series_id, ch_num,
            source=source.key,
            source_chapter_id=ch_info["chapter_id"],
            title=ch_info.get("title"),
            group_name=ch_info.get("group_name"),
            publish_date=ch_info.get("publish_date"),
        )

        if vol_id is not None:
            assign_chapter_to_volume(conn, chapter_id, vol_id)

    conn.commit()


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

_ARCHIVE_IMAGE_SUFFIX_RE = re.compile(
    r"\.(jpe?g|png|webp|gif|bmp|tif|tiff)$",
    re.IGNORECASE,
)
# Smallest single member we bother reading magic from (avoid huge reads on corrupt zips).
_MIN_COMIC_PAGE_UNCOMPRESSED = 400


def _bytes_look_like_raster_image(head: bytes) -> bool:
    """True if ``head`` starts like a common raster format (not HTML/JSON/text)."""
    if len(head) < 6:
        return False
    if head[:2] == b"\xff\xd8":
        return True
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if head[:2] == b"BM":
        return True
    return False


def _cbz_zip_archive_has_plausible_pages(path: str) -> bool:
    """CBZ/CBR-style zip: at least one image member, total payload not trivial, first page has image magic."""
    try:
        with zipfile.ZipFile(path, "r") as zf:
            infos = [
                i
                for i in zf.infolist()
                if not i.is_dir()
                and _ARCHIVE_IMAGE_SUFFIX_RE.search(i.filename)
                and "__MACOSX/" not in i.filename
            ]
            if not infos:
                return False
            infos.sort(key=lambda x: x.filename)
            total_img = sum(i.file_size for i in infos)
            if total_img < MIN_MDX_ARCHIVE_BYTES:
                return False
            first = infos[0]
            if first.file_size < _MIN_COMIC_PAGE_UNCOMPRESSED:
                return False
            with zf.open(first.filename, "r") as rf:
                head = rf.read(32)
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return False
    return _bytes_look_like_raster_image(head)


def _manga_archive_passes_sanity_check(path: str) -> bool:
    """Reject tiny or non-comic archives (bad ``mdx`` runs, empty Suwayomi zips, HTML-as-.jpg, etc.)."""
    try:
        sz = os.path.getsize(path)
    except OSError:
        return False
    if sz < MIN_MDX_ARCHIVE_BYTES:
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext in (".cbz", ".zip"):
        if not zipfile.is_zipfile(path):
            return False
        return _cbz_zip_archive_has_plausible_pages(path)
    if ext == ".epub":
        if not zipfile.is_zipfile(path):
            return False
        try:
            with zipfile.ZipFile(path, "r") as zf:
                names = set(zf.namelist())
                if "mimetype" not in names and "META-INF/container.xml" not in names:
                    return False
        except (zipfile.BadZipFile, OSError):
            return False
        return True
    if ext == ".pdf":
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    return True


def _junk_chapter_file_cleared(conn, existing) -> bool:
    """If ``existing.path`` points to a bogus archive, delete it and reset the DB row.

    Returns True when the caller should fall through and run ``upsert_chapter`` again.
    """
    abs_p = (
        os.path.join(MANGA_ROOT, existing["path"])
        if existing["path"]
        else ""
    )
    junk = (
        bool(existing["path"])
        and os.path.isfile(abs_p)
        and not _manga_archive_passes_sanity_check(abs_p)
    )
    if not junk:
        return False
    with contextlib.suppress(OSError):
        os.remove(abs_p)
    conn.execute(
        """
        UPDATE chapters SET path=NULL, file_size=NULL,
            has_comicinfo=0, status='known'
        WHERE id=?
        """,
        (existing["id"],),
    )
    return True


def _cbz_files_in(series_dir: str) -> set[str]:
    try:
        return {
            f for f in os.listdir(series_dir)
            if os.path.splitext(f)[1].lower() in MANGA_EXTENSIONS
        }
    except OSError:
        return set()


def _find_new_file(
    series_dir: str, before: set[str], chapter_num: float
) -> str | None:
    after    = _cbz_files_in(series_dir)
    new_files = after - before
    if not new_files:
        return None
    # Among new files, prefer one whose filename matches the chapter number
    for f in sorted(new_files):
        m = CH_RE.search(f)
        if m and abs(float(m.group(1)) - chapter_num) < 0.01:
            return os.path.join(series_dir, f)
    return os.path.join(series_dir, sorted(new_files)[0])


def _mdx_download(
    ch_row: dict, series_dir: str, file_format: str, chapter_naming: str,
) -> bool:
    chapter_url = f"https://mangadex.org/chapter/{ch_row['source_chapter_id']}"
    cmd = [
        "mdx", "dl",
        "-e", file_format,
        "-s", chapter_url,
        "-o", series_dir,
        "--file-name", chapter_naming,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        _log(f"  [warn] mdx ch.{ch_row['chapter_num']}: "
             f"{(result.stderr or result.stdout).strip()}")
        return False
    return True


# ---------------------------------------------------------------------------
# Volume merging
# ---------------------------------------------------------------------------

def _volume_cbz_comicinfo_xml(
    *,
    series_title: str,
    volume_num,
    chapter_lo=None,
    chapter_hi=None,
    group_name: str | None = None,
    meta: dict,
    language: str,
    series_web: str | None = None,
    page_count: int | None = None,
) -> str:
    """ComicInfo for a merged volume CBZ.

    Kavita maps ``Number`` to one issue; omit it for omnibus archives. Avoid
    putting ``ch.1-7``-style text in ``Title`` - combined with a filename that
    also contains ``ch.*``, Kavita may show duplicate pseudo-chapters. Put the
    span in ``Summary`` only and use a filename without ``ch.*`` (see
    ``volume_naming`` in settings).
    """
    desc = (meta or {}).get("description")
    if chapter_lo is not None and chapter_hi is not None:
        ch_range = (
            floor_int_str(chapter_lo)
            if chapter_lo == chapter_hi
            else f"{floor_int_str(chapter_lo)}-{floor_int_str(chapter_hi)}"
        )
        prefix = f"This volume contains chapters {ch_range}."
        desc = f"{prefix}\n\n{desc}" if desc else prefix
    gn = (group_name or "").strip() or None
    return build_comicinfo_xml(
        series_title=series_title,
        number=None,
        volume_num=volume_num,
        chapter_title=None,
        description=desc,
        authors=(meta or {}).get("authors"),
        artists=(meta or {}).get("artists"),
        group_name=gn,
        language=language,
        year=(meta or {}).get("year"),
        tags=(meta or {}).get("tags"),
        content_rating=(meta or {}).get("content_rating"),
        page_count=page_count,
        web=series_web,
        count=(meta or {}).get("total_volumes"),
    )


# Strip `` ch.1-7`` (or `` ch.3``) from merged volume basenames so Kavita does not
# treat ``vol.N`` and ``ch.*`` as two separate issues in one file.
_VOL_CH_SUFFIX_RE = re.compile(
    r"(\s+ch\.\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?)(\.[^.]+)$",
    re.IGNORECASE,
)


def normalize_volume_filenames_for_kavita(series_row: dict, conn) -> int:
    """Rename volume CBZs that still have a ``ch.X-Y`` suffix after ``vol.N``."""
    series_id  = series_row["id"]
    series_dir = os.path.join(MANGA_ROOT, series_row["path"])
    label      = series_row.get("name", series_row["path"])

    rows = conn.execute(
        "SELECT * FROM volumes WHERE series_id=? AND path IS NOT NULL",
        (series_id,),
    ).fetchall()
    n = 0
    for vol in rows:
        vol = dict(vol)
        old_rel  = vol["path"]
        old_base = os.path.basename(old_rel)
        m = _VOL_CH_SUFFIX_RE.search(old_base)
        if not m:
            continue
        new_base = old_base[: m.start(1)] + m.group(2)
        old_abs  = os.path.join(MANGA_ROOT, old_rel)
        new_abs  = os.path.join(series_dir, new_base)
        new_rel  = os.path.relpath(new_abs, MANGA_ROOT)
        if not os.path.isfile(old_abs):
            continue
        if os.path.exists(new_abs):
            _log(f"[{label}] kavita-rename skip (exists): {new_base}")
            continue
        os.rename(old_abs, new_abs)
        sz = os.path.getsize(new_abs)
        conn.execute(
            "UPDATE volumes SET path=?, file_size=? WHERE id=?",
            (new_rel, sz, vol["id"]),
        )
        log_rename(
            conn,
            old_rel,
            new_rel,
            "rename",
            "strip ch range from merged volume stem (Kavita)",
            series_id=series_id,
            volume_id=vol["id"],
        )
        n += 1
    conn.commit()
    scan_disk_files(series_dir, series_id, conn)
    _log(f"[{label}] kavita-rename: stripped ch.* suffix from {n} volume file(s)")
    return n


def refresh_volume_comicinfo_embeds(series_row: dict, conn, settings: dict | None = None) -> int:
    """Rewrite ComicInfo in volume CBZs (fixes Kavita showing Number == volume)."""
    series_id   = series_row["id"]
    series_path = series_row["path"]
    series_dir  = os.path.join(MANGA_ROOT, series_path)
    source_name = series_row.get("source_name") or "mangadex"
    meta        = get_series_metadata(conn, series_id, source_name) or {}
    language    = series_row.get("language", "en")
    manga_id    = series_row.get("source_id")
    series_web  = _get_source(source_name).get_web_url(manga_id) if manga_id else None
    series_title, filename_title = resolve_series_titles(
        series_row, meta, settings, os.path.basename(series_dir))
    label       = series_row.get("name", series_path)

    # Bring existing filenames in line first, so the rows read below carry the
    # post-rename paths and Kavita sees one consistently-named series.
    _rename_volume_stems_for_locale(conn, series_row, settings or {}, meta, label)

    file_permission_mask = (settings or {}).get("file_permission_mask")
    rows = conn.execute(
        "SELECT * FROM volumes WHERE series_id=? AND path IS NOT NULL",
        (series_id,),
    ).fetchall()
    done = 0
    for vol in rows:
        vol = dict(vol)
        cbz_path = os.path.join(MANGA_ROOT, vol["path"])
        if not os.path.exists(cbz_path):
            continue
        bounds = conn.execute(
            "SELECT MIN(chapter_num) AS lo, MAX(chapter_num) AS hi "
            "FROM chapters WHERE volume_id=?",
            (vol["id"],),
        ).fetchone()
        lo, hi = bounds["lo"], bounds["hi"]
        grp = conn.execute(
            "SELECT group_name FROM chapters WHERE volume_id=? "
            "AND group_name IS NOT NULL AND TRIM(group_name) != '' LIMIT 1",
            (vol["id"],),
        ).fetchone()
        prefs = _series_preferred_groups(series_row)
        group_name = (
            grp["group_name"] if grp else (prefs[0] if prefs else None)
        )
        xml = _volume_cbz_comicinfo_xml(
            series_title=series_title,
            volume_num=vol["volume_num"],
            chapter_lo=lo,
            chapter_hi=hi,
            group_name=group_name,
            meta=meta,
            language=language,
            series_web=series_web,
            page_count=count_pages(cbz_path),
        )
        if inject_comicinfo(
            cbz_path,
            xml,
            overwrite=True,
            file_permission_mask=file_permission_mask,
        ):
            mark_volume_comicinfo(conn, vol["id"])
            done += 1
    _log(f"[{label}] refreshed ComicInfo in {done} volume file(s) (Kavita-friendly)")
    return done


def _merge_volume_batch(
    series_id: int,
    series_dir: str,
    conn,
    vol_nums: list[float],
    meta: dict,
    language: str,
    preferred_group: str | None,
    settings: dict,
    series_web: str | None = None,
    series_row: dict | None = None,
) -> tuple[int, list[str]]:
    """Merge listed volumes; returns (success_count, error_messages)."""
    volume_naming = sanitize_volume_naming(settings.get("volume_naming"))
    file_format   = settings.get("file_format", "cbz")
    series_title, filename_title = resolve_series_titles(
        series_row, meta, settings, os.path.basename(series_dir))
    merged = 0
    errors: list[str] = []

    for vol_num in vol_nums:
        vol_row = get_volume(conn, series_id, vol_num)
        if not vol_row:
            errors.append(f"vol.{int(vol_num)}: missing volume row")
            continue
        ch_rows = get_chapters_for_volume(conn, series_id, vol_num)
        if not ch_rows:
            continue

        ch_rows  = sorted(ch_rows, key=lambda c: c["chapter_num"])
        ch_rows  = [c for c in ch_rows if c.get("path")]
        if not ch_rows:
            continue

        ch_paths = [os.path.join(MANGA_ROOT, c["path"]) for c in ch_rows]

        if not all(os.path.exists(p) for p in ch_paths):
            msg = f"vol.{int(vol_num)}: some chapter files missing, skipping merge"
            _log(f"  {msg}")
            errors.append(msg)
            continue

        first_ch = ch_rows[0]["chapter_num"]
        last_ch  = ch_rows[-1]["chapter_num"]

        group_name = ch_rows[0].get("group_name") or preferred_group or ""
        # Never pass chapter span into the filename template (Kavita); span is
        # only in ComicInfo summary.
        base_name  = apply_naming_template(volume_naming, language=language or "en",
                                          group=group_name, title=filename_title,
                                          volume_num=vol_num)
        out_name   = base_name if base_name.endswith(f".{file_format}") \
                     else f"{base_name}.{file_format}"
        out_path   = os.path.join(series_dir, out_name)
        out_abs    = os.path.abspath(out_path)
        ch_abs_set = {os.path.abspath(p) for p in ch_paths}

        if os.path.exists(out_path) and out_abs not in ch_abs_set:
            msg = f"vol.{int(vol_num)}: output file already exists ({out_name})"
            _log(f"  {msg}")
            errors.append(msg)
            continue

        xml = _volume_cbz_comicinfo_xml(
            series_title=series_title,
            volume_num=vol_num,
            chapter_lo=first_ch,
            chapter_hi=last_ch,
            group_name=group_name,
            meta=meta,
            language=language or "en",
            series_web=series_web,
            page_count=None,
        )

        cover_url    = vol_row.get("cover_url")
        cover_bytes  = _download_cover(cover_url) if cover_url else None
        # No cover art published for this volume yet: use the first page so the
        # archive is not coverless, and mark it as provisional.
        used_placeholder = False
        if not cover_bytes and ch_paths:
            cover_bytes = read_first_image_bytes(ch_paths[0])
            used_placeholder = bool(cover_bytes)

        _log(f"  merging vol.{int(vol_num)} "
             f"({len(ch_paths)} ch) → {out_name}"
             + (" [+cover]" if cover_bytes else ""))

        if merge_chapters_into_volume(
            ch_paths,
            out_path,
            xml,
            cover_bytes,
            file_permission_mask=settings.get("file_permission_mask"),
        ):
            rel_out  = os.path.relpath(out_path, MANGA_ROOT)
            vol_id   = vol_row["id"]
            mark_volume_merged(conn, vol_id, rel_out, os.path.getsize(out_path))
            if used_placeholder:
                upsert_volume(conn, series_id, vol_num,
                              cover_locale=loc.PLACEHOLDER_COVER)
            mark_volume_comicinfo(conn, vol_id)

            for ch_row, ch_path in zip(ch_rows, ch_paths):
                conn.execute(
                    "UPDATE chapters SET path=NULL, status='known' WHERE id=?",
                    (ch_row["id"],),
                )
                log_rename(conn, ch_path, out_path, "merge", "volume_merge",
                           series_id=series_id, volume_id=vol_id,
                           chapter_id=ch_row["id"])
                try:
                    os.remove(ch_path)
                except OSError:
                    pass
            conn.commit()
            merged += 1
            _log(f"  vol.{int(vol_num)}: done")
        else:
            msg = f"vol.{int(vol_num)}: merge failed"
            _log(f"  {msg}")
            errors.append(msg)

    return merged, errors


def compact_series_volumes(series_row: dict, conn, settings: dict) -> tuple[int, list[str]]:
    """Manual/UI: merge every volume that has chapter files but no merged CBZ yet."""
    series_id   = series_row["id"]
    series_path = series_row["path"]
    series_dir  = os.path.join(MANGA_ROOT, series_path)
    manga_id    = series_row.get("source_id")
    source_name = series_row.get("source_name") or "mangadex"
    source      = _get_source(source_name)
    label       = series_row.get("name") or os.path.basename(series_path)

    scan_disk_files(series_dir, series_id, conn)
    if manga_id and isinstance(source, MangaDexSource):
        cpref, colang = _cover_args(conn, series_row, settings)
        _ensure_volume_cover_urls(conn, series_id, manga_id, cpref, colang,
                                  settings, label,
                                  _has_explicit_cover_locale(series_row, settings),
                                  _manages_existing_files(series_row))

    meta = get_series_metadata(conn, series_id, source_name) or {}
    vol_nums = get_volumes_needing_compact(conn, series_id)
    if not vol_nums:
        return 0, []

    series_web = source.get_web_url(manga_id) if manga_id else None
    _log(f"[{label}] compact into volumes: {len(vol_nums)} volume(s)…")
    _pg = _series_preferred_groups(series_row)
    merged, errors = _merge_volume_batch(
        series_id, series_dir, conn, vol_nums,
        meta,
        series_row.get("language", "en"),
        _pg[0] if _pg else None,
        settings,
        series_web=series_web,
        series_row=series_row,
    )
    scan_disk_files(series_dir, series_id, conn)
    return merged, errors


# ---------------------------------------------------------------------------
# ComicInfo injection
# ---------------------------------------------------------------------------

def _ensure_comicinfo_all(
    series_id: int, series_dir: str, conn, meta: dict, language: str,
    series_web: str | None = None,
    series_label: str | None = None,
    force_overwrite: bool = False,
    file_permission_mask: str | None = None,
    side_fx: dict | None = None,
    series_row: dict | None = None,
    settings: dict | None = None,
):
    ch_rows, vol_rows = get_files_missing_comicinfo(conn, series_id)
    if not ch_rows and not vol_rows:
        return
    label = series_label or os.path.basename(series_dir)
    _log(f"[{label}] ComicInfo pass: {len(ch_rows)} chapter file(s), {len(vol_rows)} volume file(s)")

    series_title, filename_title = resolve_series_titles(
        series_row, meta, settings, os.path.basename(series_dir))
    description    = (meta or {}).get("description")
    authors        = (meta or {}).get("authors") or []
    artists        = (meta or {}).get("artists") or []
    tags           = (meta or {}).get("tags") or []
    year           = (meta or {}).get("year")
    content_rating = (meta or {}).get("content_rating")

    # Resolve volume_num for each chapter (needed for the Volume field)
    vol_num_by_vol_id: dict[int, float] = {}
    for row in conn.execute(
        "SELECT id, volume_num FROM volumes WHERE series_id=?", (series_id,)
    ).fetchall():
        vol_num_by_vol_id[row["id"]] = row["volume_num"]

    injected_ch = 0
    total_ch    = len(ch_rows)

    for idx, ch in enumerate(ch_rows, start=1):
        cbz_path = os.path.join(MANGA_ROOT, ch["path"])
        if not os.path.exists(cbz_path):
            continue
        vol_num = vol_num_by_vol_id.get(ch.get("volume_id")) if ch.get("volume_id") else None
        # ``on_disk`` rows never carry trustworthy per-chapter source info -
        # the file got there outside the sync pipeline (user import, manual
        # rip, etc.). Use only series-level fields for ComicInfo so we do
        # not stamp foreign translator names / chapter titles / chapter URLs
        # onto archives we did not download ourselves.
        if ch.get("status") == "on_disk":
            ch_title = None
            group_name = None
            ch_web = series_web
        else:
            ch_title = (ch.get("title") or "").strip() or None
            group_name = ch.get("group_name")
            ch_id = ch.get("source_chapter_id")
            ch_web = (
                f"https://mangadex.org/chapter/{ch_id}" if ch_id else series_web
            )
        xml = build_comicinfo_xml(
            series_title=series_title,
            number=ch["chapter_num"],
            volume_num=vol_num,
            chapter_title=ch_title,
            description=description,
            authors=authors,
            artists=artists,
            group_name=group_name,
            language=language,
            year=year,
            tags=tags,
            content_rating=content_rating,
            page_count=count_pages(cbz_path),
            web=ch_web,
        )
        if inject_comicinfo(
            cbz_path,
            xml,
            overwrite=True,
            file_permission_mask=file_permission_mask,
        ):
            mark_chapter_comicinfo(conn, ch["id"])
            injected_ch += 1
            _log(f"[{label}] ComicInfo {idx}/{total_ch}: {os.path.basename(cbz_path)}")
        elif not os.access(cbz_path, os.W_OK):
            _bump_library_write_denied(side_fx)
            _log(
                f"[{label}] ComicInfo not applied (no write permission to file): "
                f"{os.path.basename(cbz_path)}"
            )

    injected_vol = 0
    total_vol    = len(vol_rows)
    for idx, vol in enumerate(vol_rows, start=1):
        cbz_path = os.path.join(MANGA_ROOT, vol["path"])
        if not os.path.exists(cbz_path):
            continue
        bounds = conn.execute(
            "SELECT MIN(chapter_num) AS lo, MAX(chapter_num) AS hi "
            "FROM chapters WHERE volume_id=?",
            (vol["id"],),
        ).fetchone()
        lo, hi = bounds["lo"], bounds["hi"]
        grp = conn.execute(
            "SELECT group_name FROM chapters WHERE volume_id=? "
            "AND group_name IS NOT NULL AND TRIM(group_name) != '' LIMIT 1",
            (vol["id"],),
        ).fetchone()
        group_name = grp["group_name"] if grp else None
        xml = _volume_cbz_comicinfo_xml(
            series_title=series_title,
            volume_num=vol["volume_num"],
            chapter_lo=lo,
            chapter_hi=hi,
            group_name=group_name,
            meta=meta,
            language=language,
            series_web=series_web,
            page_count=count_pages(cbz_path),
        )
        if inject_comicinfo(
            cbz_path,
            xml,
            overwrite=True,
            file_permission_mask=file_permission_mask,
        ):
            mark_volume_comicinfo(conn, vol["id"])
            injected_vol += 1
            _log(f"[{label}] ComicInfo vol {idx}/{total_vol}: {os.path.basename(cbz_path)}")
        elif not os.access(cbz_path, os.W_OK):
            _bump_library_write_denied(side_fx)
            _log(
                f"[{label}] ComicInfo not applied (no write permission to file): "
                f"{os.path.basename(cbz_path)}"
            )
    _log(f"[{label}] ComicInfo done: {injected_ch} chapter(s), {injected_vol} volume(s) updated")


# ---------------------------------------------------------------------------
# Fix pass + Kavita
# ---------------------------------------------------------------------------

def _run_fix_pass(series_dir: str):
    log_path = os.path.join(MANGA_ROOT, _fix.LOG_FILENAME)
    log_data = _fix.load_log(log_path)
    for fpath, issue_name, new_name in _fix.scan(series_dir):
        _fix.do_rename(fpath, new_name, issue_name, log_data, log_path)


def _normalize_volume_cover_key(v) -> str:
    """Canonical key for matching Kavita volume numbers to MangaDex ``/cover`` volume strings."""
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    try:
        n = float(s)
        if math.isfinite(n) and n == int(n):
            return str(int(n))
    except ValueError:
        pass
    return s


def _aggregate_remap_mdx_id(
    series_row: dict,
    to_download: list,
    conn,
    interval_days: int,
) -> str | None:
    """Return the MD UUID when this series' canonical volume map is due."""
    manga_id = mangadex_id_for_series(series_row)
    if not manga_id:
        return None
    if to_download or weekly_mdx_aggregate_volume_remap_due(
        conn, series_row["id"], interval_days
    ):
        return manga_id
    return None


def _volume_cover_urls_by_canonical_key(raw: dict) -> dict[str, str]:
    """Re-key MD cover map so ``17`` and ``17.0`` (and JSON float quirks) resolve the same URL."""
    out: dict[str, str] = {}
    for k, entry in (raw or {}).items():
        url = entry[0] if isinstance(entry, tuple) else entry
        ck  = _normalize_volume_cover_key(k)
        if ck and url:
            out[ck] = url
    return out


def _missing_mdx_cover_is_notable(vol_key: str) -> bool:
    """Whether to log ``cover skip`` — omit Kavita placeholders (e.g. ``-100000`` loose chapters)."""
    if not vol_key:
        return False
    try:
        n = float(vol_key)
    except ValueError:
        return False
    return math.isfinite(n) and n >= 1


def _kavita_set_covers(
    client: KavitaClient,
    series_dir: str,
    manga_id: str,
    conn=None,
    series_id: int | None = None,
    force: bool = False,
    cover_preference: str | None = None,
    original_language: str | None = None,
    display_title: str | None = None,
):
    series_name = os.path.basename(series_dir)
    try:
        covers = _volume_cover_urls_by_canonical_key(
            MangaDexSource().get_volume_covers(manga_id, cover_preference,
                                               original_language)
        )
        if not covers:
            return
        # Kavita names a series from ComicInfo ``Series``, which a title
        # preference changes, while the folder keeps its original name. Search
        # matches exactly, so try the resolved title before the folder name.
        series = None
        tried: list[str] = []
        for candidate in (display_title, series_name):
            candidate = (candidate or "").strip()
            if not candidate or candidate in tried:
                continue
            tried.append(candidate)
            series = client.search_series(candidate)
            if series:
                break
        if not series:
            _log(f"[kavita] series not found: {' / '.join(tried) or series_name}")
            return
        for vol in client.get_volumes(series["id"]):
            vol_raw = vol.get("number", "")
            vol_num = str(vol_raw).strip() or "(unnumbered)"
            vol_key = _normalize_volume_cover_key(vol_raw)
            url     = covers.get(vol_key) if vol_key else None
            if not url:
                if vol_key and _missing_mdx_cover_is_notable(vol_key):
                    _log(
                        f"[kavita] cover skip (no MangaDex volume cover): "
                        f"{series_name} vol.{vol_num}"
                    )
                continue
            if not force and conn is not None and series_id is not None:
                try:
                    local_vol = get_volume(conn, series_id, float(vol_raw))
                    if local_vol and local_vol.get("kavita_cover_url") == url:
                        continue
                except Exception:
                    pass
            try:
                client.set_volume_cover(vol["id"], url)
                _log(f"[kavita] cover set: {series_name} vol.{vol_num}")
                if conn is not None and series_id is not None:
                    try:
                        upsert_volume(conn, series_id, float(vol_raw), kavita_cover_url=url)
                        conn.commit()
                    except Exception:
                        pass
            except Exception as e:
                _log(f"[kavita] cover failed vol.{vol_num}: {e}")
    except Exception as e:
        _log(f"[kavita] covers error for {series_name}: {e}")


# ---------------------------------------------------------------------------
# Per-series sync
# ---------------------------------------------------------------------------

def _bump_library_write_denied(side_fx: dict | None) -> None:
    """Count permission-related skips so the main sync can summarize once at the end."""
    if side_fx is not None:
        side_fx["library_write_denied"] = side_fx.get("library_write_denied", 0) + 1


def _is_permission_errno(err: OSError) -> bool:
    no = getattr(err, "errno", None)
    return no in (errno.EACCES, errno.EPERM, errno.EROFS)


def _stem_shows_volume_num(stem: str, volume_num) -> bool:
    """True if the basename already embeds ``vol.N`` matching ``volume_num`` (from MD)."""
    if volume_num is None:
        return False
    try:
        want = float(volume_num)
    except (TypeError, ValueError):
        return False
    m = re.search(r"(?i)\bvol\.?\s*(\d+(?:\.\d+)?)\b", stem)
    if not m:
        return False
    try:
        got = float(m.group(1))
    except ValueError:
        return False
    return abs(got - want) < 1e-6


def _stem_declares_any_volume(stem: str) -> bool:
    """True if the basename already embeds a ``vol.N`` token (user or legacy layout)."""
    return bool(re.search(r"(?i)\bvol\.?\s*\d", stem))


def _rename_tracked_chapter_stems_for_template(
    conn,
    series_row: dict,
    settings: dict,
    label: str,
    side_fx: dict | None = None,
) -> int:
    """Rename chapter CBZs on disk so basenames match ``chapter_naming`` (incl. vol).

    Does not merge archives — only filesystem renames + DB path updates so
    volume numbers picked up from MangaDex appear in filenames (e.g. ``vol.17``).

    Skips stems that already embed the correct ``vol.N`` for the DB bucket so we
    only touch files that actually need a volume tag (sync-time behaviour; Fix
    Files / manga-fix is unchanged and still proposes full template alignment).
    """
    if series_row.get("exclude_from_fix"):
        return 0
    series_id = series_row["id"]
    series_dir = os.path.join(MANGA_ROOT, series_row["path"])
    language = series_row.get("language", "en")
    ch_naming = settings.get("chapter_naming", "%3 ch.%5")
    name = series_row.get("name") or os.path.basename(series_row["path"])
    _meta = get_series_metadata(
        conn, series_id, series_row.get("source_name") or "mangadex") or {}
    name = _filename_title_for(series_row, _meta, settings, name)
    rows = conn.execute(
        """
        SELECT c.id, c.path, c.chapter_num, c.title, c.group_name, v.volume_num
        FROM chapters c
        LEFT JOIN volumes v ON v.id = c.volume_id
        WHERE c.series_id = ? AND c.path IS NOT NULL
        ORDER BY c.chapter_num
        """,
        (series_id,),
    ).fetchall()
    n_done = 0
    for r in rows:
        old_rel = (r["path"] or "").strip()
        if not old_rel:
            continue
        old_abs = os.path.join(MANGA_ROOT, old_rel)
        if not os.path.isfile(old_abs):
            continue
        desired = apply_naming_template(
            ch_naming,
            language=language,
            group=(r["group_name"] or ""),
            title=name,
            volume_num=r["volume_num"],
            chapter_num=r["chapter_num"],
            chapter_title=(r["title"] or "").strip() or "",
        )
        if not desired:
            continue
        ext = os.path.splitext(old_abs)[1]
        cur_stem = os.path.splitext(os.path.basename(old_abs))[0]
        if cur_stem == desired:
            continue
        # Already carries the correct tankōbon volume in the filename (layout may
        # differ slightly from ``chapter_naming``, e.g. scanlator suffix) — do not touch.
        if r["volume_num"] is not None and _stem_shows_volume_num(cur_stem, r["volume_num"]):
            continue
        # DB has no volume yet but filename already carries vol.N — avoid stripping it.
        if r["volume_num"] is None and _stem_declares_any_volume(cur_stem):
            continue
        new_abs = os.path.join(series_dir, desired + ext)
        if os.path.abspath(old_abs) == os.path.abspath(new_abs):
            continue
        if os.path.exists(new_abs):
            _log(f"[{label}] chapter stem skip (target exists): {desired}{ext}")
            continue
        try:
            os.rename(old_abs, new_abs)
        except OSError as e:
            if _is_permission_errno(e):
                _bump_library_write_denied(side_fx)
                _log(
                    f"[{label}] chapter stem rename blocked by filesystem permissions "
                    f"(ch.{r['chapter_num']}): {e} — file: {old_abs}"
                )
            else:
                _log(f"[{label}] chapter stem rename failed ch.{r['chapter_num']}: {e}")
            continue
        rel_new = os.path.relpath(new_abs, MANGA_ROOT)
        sz = os.path.getsize(new_abs)
        conn.execute(
            "UPDATE chapters SET path=?, file_size=?, has_comicinfo=0 WHERE id=?",
            (rel_new, sz, r["id"]),
        )
        conn.commit()
        log_rename(
            conn,
            old_rel,
            rel_new,
            "rename",
            "chapter_stem_template",
            series_id=series_id,
            chapter_id=r["id"],
        )
        apply_file_permission_mask(new_abs, settings.get("file_permission_mask"))
        n_done += 1
        _log(
            f"[{label}] chapter stem: ch.{r['chapter_num']} "
            f"{os.path.basename(old_rel)} → {desired}{ext}"
        )
    return n_done


def _manages_existing_files(series_row: dict) -> bool:
    """Whether pre-existing files in this folder may be rewritten."""
    return bool((series_row or {}).get("manage_existing_files"))


def _row_is_ours(row, kind: str) -> bool:
    """Whether Manga Maid created this file.

    Volumes we merged carry ``origin='merged'``; chapters we fetched carry
    ``status='downloaded'``. Anything else was in the folder already - the
    common case being a user who owns the first volumes in print and syncs the
    rest. Rows predating these columns read as not-ours, which is the safe way
    round: we decline to rewrite rather than guess.

    Only cover writes consult this. Renames are reversible and recorded in
    ``rename_log``; overwriting cover bytes inside an archive we did not build
    is not, because the original image cannot be recovered.
    """
    if kind == "volume":
        return (row["origin"] if "origin" in row.keys() else None) == "merged"
    return (row["status"] if "status" in row.keys() else None) == "downloaded"


def _has_explicit_cover_locale(series_row: dict, settings: dict) -> bool:
    """Whether the user actually chose a cover language for this series.

    Gates rewriting archives already on disk. Unset still resolves to
    ``original`` when a volume is merged, but nothing existing is touched -
    inserting cover art into imported files nobody asked about would be a
    surprising thing for an update to do.
    """
    return bool(
        (series_row or {}).get("cover_language_override")
        or (settings or {}).get("cover_language")
    )


def _has_explicit_filename_locale(series_row: dict, settings: dict) -> bool:
    """Whether the user actually chose a filename language for this series.

    Gates the rename pass: on upgrade nobody has chosen anything, so no library
    gets renamed as a side effect of installing this. Renaming only happens once
    someone picks a locale, which is exactly when they expect their existing
    files to be brought in line.
    """
    pref = loc.filename_preference(
        series_row.get("filename_language_override"),
        (settings or {}).get("filename_language"),
        loc.effective_preference(
            series_row.get("title_language_override"),
            (settings or {}).get("title_language"), loc.LEGACY,
        ),
    )
    return pref != loc.LEGACY


def _filename_title_for(series_row: dict, meta: dict, settings: dict, fallback: str) -> str:
    """Title to substitute for ``%3``.

    Falls back to the folder name unless a filename language was actually
    chosen, preserving the long-standing behaviour for anyone who has not opted
    in - chapter stems have always been named from the folder.
    """
    if not _has_explicit_filename_locale(series_row, settings or {}):
        return fallback
    _, filename_title = resolve_series_titles(series_row, meta, settings, fallback)
    return filename_title or fallback


def _known_title_variants(series_row: dict, meta: dict, series_dir: str) -> list[str]:
    """Every title this series may currently be filed under, longest first.

    Used to locate the title inside an existing filename. Longest-first matters
    so ``Made in Abyss Official`` is not half-replaced by ``Made in Abyss``.
    """
    variants = {os.path.basename(series_dir), (meta or {}).get("title") or ""}
    variants.update(_cached_title_pool(meta).values())
    return sorted((v.strip() for v in variants if v and v.strip()),
                  key=len, reverse=True)


def _restem_with_title(stem: str, variants: list[str], new_title: str) -> str | None:
    """Swap the series title inside ``stem``, leaving every other token alone.

    Returns ``None`` when no known title is present - a stem we cannot parse is
    left untouched rather than rebuilt from the template, which would silently
    drop scanlation groups or any manual naming the user applied.
    """
    safe_new = safe_filename_token(new_title)
    for variant in variants:
        safe_old = safe_filename_token(variant)
        if safe_old and safe_old in stem:
            if safe_old == safe_new:
                return None
            return stem.replace(safe_old, safe_new)
    return None


def _manages_existing_files(series_row: dict) -> bool:
    """Whether pre-existing files in this folder may be rewritten."""
    return bool((series_row or {}).get("manage_existing_files"))


def _row_is_ours(row, kind: str) -> bool:
    """Whether Manga Maid created this file.

    Volumes we merged carry ``origin='merged'``; chapters we fetched carry
    ``status='downloaded'``. Anything else was in the folder already - the
    common case being a user who owns the first volumes in print and syncs the
    rest. Rows predating these columns read as not-ours, which is the safe way
    round: we decline to rewrite rather than guess.

    Only cover writes consult this. Renames are reversible and recorded in
    ``rename_log``; overwriting cover bytes inside an archive we did not build
    is not, because the original image cannot be recovered.
    """
    if kind == "volume":
        return (row["origin"] if "origin" in row.keys() else None) == "merged"
    return (row["status"] if "status" in row.keys() else None) == "downloaded"


def _has_explicit_cover_locale(series_row: dict, settings: dict) -> bool:
    """Whether the user actually chose a cover language for this series.

    Gates rewriting archives already on disk. Unset still resolves to
    ``original`` when a volume is merged, but nothing existing is touched -
    inserting cover art into imported files nobody asked about would be a
    surprising thing for an update to do.
    """
    return bool(
        (series_row or {}).get("cover_language_override")
        or (settings or {}).get("cover_language")
    )


def _has_explicit_filename_locale(series_row: dict, settings: dict) -> bool:
    """Whether the user actually chose a filename language for this series.

    Gates the rename pass: on upgrade nobody has chosen anything, so no library
    gets renamed as a side effect of installing this. Renaming only happens once
    someone picks a locale, which is exactly when they expect their existing
    files to be brought in line.
    """
    pref = loc.filename_preference(
        series_row.get("filename_language_override"),
        (settings or {}).get("filename_language"),
        loc.effective_preference(
            series_row.get("title_language_override"),
            (settings or {}).get("title_language"), loc.LEGACY,
        ),
    )
    return pref != loc.LEGACY


def _rename_stems_for_locale(
    conn,
    series_row: dict,
    settings: dict,
    meta: dict,
    label: str,
    side_fx: dict | None = None,
    kind: str = "volume",
) -> int:
    """Rename existing CBZs to the chosen filename language.

    Covers both merged volumes and loose chapters: the filename setting names a
    title, and both templates substitute one, so honouring it for only half the
    library would leave a series filed under two different names.

    Applies to every file in the series, including ones that were already in the
    folder: a rename is reversible and recorded in ``rename_log``, so the cost of
    getting it wrong is low and a half-renamed series is worse.
    """
    if series_row.get("exclude_from_fix"):
        return 0
    if not _has_explicit_filename_locale(series_row, settings):
        return 0

    series_id  = series_row["id"]
    series_dir = os.path.join(MANGA_ROOT, series_row["path"])
    _, filename_title = resolve_series_titles(
        series_row, meta, settings, os.path.basename(series_dir))
    if not filename_title:
        return 0
    variants = _known_title_variants(series_row, meta, series_dir)

    is_volume = kind == "volume"
    unit = "vol" if is_volume else "ch"
    rows = conn.execute(
        "SELECT id, volume_num AS num, path, origin FROM volumes "
        "WHERE series_id=? AND path IS NOT NULL ORDER BY volume_num"
        if is_volume else
        "SELECT id, chapter_num AS num, path, status FROM chapters "
        "WHERE series_id=? AND path IS NOT NULL ORDER BY chapter_num",
        (series_id,),
    ).fetchall()
    n_done = 0
    for r in rows:
        old_rel = (r["path"] or "").strip()
        if not old_rel:
            continue
        old_abs = os.path.join(MANGA_ROOT, old_rel)
        if not os.path.isfile(old_abs):
            continue
        cur_stem = os.path.splitext(os.path.basename(old_abs))[0]
        desired  = _restem_with_title(cur_stem, variants, filename_title)
        if not desired or desired == cur_stem:
            continue
        ext     = os.path.splitext(old_abs)[1]
        new_abs = os.path.join(os.path.dirname(old_abs), desired + ext)
        if os.path.abspath(old_abs) == os.path.abspath(new_abs):
            continue
        if os.path.exists(new_abs):
            _log(f"[{label}] {kind} stem skip (target exists): {desired}{ext}")
            continue
        try:
            os.rename(old_abs, new_abs)
        except OSError as e:
            if _is_permission_errno(e):
                _bump_library_write_denied(side_fx)
                _log(
                    f"[{label}] {kind} stem rename blocked by filesystem permissions "
                    f"({unit}.{format_num(r['num'])}): {e} — file: {old_abs}"
                )
            else:
                _log(f"[{label}] {kind} stem rename failed "
                     f"{unit}.{format_num(r['num'])}: {e}")
            continue
        rel_new = os.path.relpath(new_abs, MANGA_ROOT)
        if is_volume:
            # Clear has_comicinfo so the ComicInfo pass rewrites <Series> with
            # the new title, the way the chapter stem rename already does.
            conn.execute("UPDATE volumes SET path=?, file_size=?, has_comicinfo=0 "
                         "WHERE id=?",
                         (rel_new, os.path.getsize(new_abs), r["id"]))
            log_rename(conn, old_rel, rel_new, "rename", "volume_stem_locale",
                       series_id=series_id, volume_id=r["id"])
        else:
            conn.execute("UPDATE chapters SET path=?, file_size=?, has_comicinfo=0 "
                         "WHERE id=?",
                         (rel_new, os.path.getsize(new_abs), r["id"]))
            log_rename(conn, old_rel, rel_new, "rename", "chapter_stem_locale",
                       series_id=series_id, chapter_id=r["id"])
        conn.commit()
        apply_file_permission_mask(new_abs, settings.get("file_permission_mask"))
        n_done += 1
        _log(f"[{label}] {kind} stem: {unit}.{format_num(r['num'])} "
             f"{os.path.basename(old_rel)} → {desired}{ext}")
    if n_done and side_fx is not None:
        key = "volume_stems_renamed" if is_volume else "chapter_stems_renamed"
        side_fx[key] = side_fx.get(key, 0) + n_done
    return n_done


def _rename_volume_stems_for_locale(conn, series_row, settings, meta, label,
                                    side_fx=None) -> int:
    return _rename_stems_for_locale(conn, series_row, settings, meta, label,
                                    side_fx, kind="volume")


def _sync_one_series(
    series_row: dict,
    conn,
    settings: dict,
    kavita_client: KavitaClient | None,
    skip_feed_counts: dict[str, int] | None = None,
    side_fx: dict | None = None,
) -> int | bool:
    series_id   = series_row["id"]
    series_path = series_row["path"]
    series_dir  = os.path.join(MANGA_ROOT, series_path)
    language    = series_row.get("language", "en")
    prefs       = _series_preferred_groups(series_row)
    group       = prefs[0] if prefs else None
    start_chapter = float(series_row.get("start_chapter") or 0)
    manga_id    = series_row.get("source_id")
    source_name = series_row.get("source_name") or "mangadex"
    source      = _get_source(source_name)
    name        = series_row.get("name") or os.path.basename(series_path)
    series_web  = source.get_web_url(manga_id) if manga_id else None
    file_permission_mask = settings.get("file_permission_mask")

    # Always reconcile disk state first
    scan_disk_files(series_dir, series_id, conn)

    # Fetch & cache series metadata (7-day TTL)
    meta = get_series_metadata(conn, series_id, source_name)
    if is_metadata_stale(meta and meta.get("fetched_at")):
        meta = _fetch_and_cache_meta(source, manga_id, series_id, source_name, language, conn)

    # Linked-only (no download options chosen yet): refresh metadata+ComicInfo,
    # do not pull the chapter feed or queue downloads.
    if not series_row.get("sync_configured"):
        if skip_feed_counts is not None:
            skip_feed_counts["not_configured"] = (
                skip_feed_counts.get("not_configured", 0) + 1
            )
        _ensure_comicinfo_all(
            series_id, series_dir, conn, meta, language,
            series_web=series_web, series_label=name,
            file_permission_mask=file_permission_mask,
            side_fx=side_fx,
            series_row=series_row, settings=settings,
        )
        update_source_sync_time(conn, series_id, source_name)
        return 0

    # User-paused: skip feed polling until manually resumed.
    if series_row.get("sync_paused"):
        if skip_feed_counts is not None:
            skip_feed_counts["paused"] = skip_feed_counts.get("paused", 0) + 1
        _ensure_comicinfo_all(
            series_id, series_dir, conn, meta, language,
            series_web=series_web, series_label=name,
            file_permission_mask=file_permission_mask,
            side_fx=side_fx,
            series_row=series_row, settings=settings,
        )
        update_source_sync_time(conn, series_id, source_name)
        return 0

    _log(f"[{name}] checking for updates…")

    # Sync chapter feed → DB
    try:
        _sync_chapters_to_db(source, manga_id, language, prefs, series_id, conn)
    except Exception as e:
        summary = _friendly_sync_error(e)
        _log(f"[{name}] feed error: {summary}")
        raise SeriesSyncError(summary) from e
    clear_series_sync_error(conn, series_id)

    to_download = get_chapters_to_download(conn, series_id, start_chapter)

    try:
        interval_days = int(settings.get("aggregate_volume_remap_interval_days") or 7)
    except (TypeError, ValueError):
        interval_days = 7
    interval_days = max(1, min(interval_days, 90))

    aggregate_mdx_id = _aggregate_remap_mdx_id(
        series_row, to_download, conn, interval_days
    )

    # Canonical volume buckets from /aggregate (language-scoped). Runs when we
    # are about to download, or on a debounced schedule while materialized rows
    # still lack volume_id (e.g. finished series MD fills tankōbon later).
    if aggregate_mdx_id:
        try:
            mdx_source = source if isinstance(source, MangaDexSource) else MangaDexSource()
            agg = mdx_source.get_aggregate_with_language_fallback(
                aggregate_mdx_id, language
            )
            apply_aggregate_volume_mapping(conn, series_id, agg)
            touch_series_aggregate_volume_remap_at(conn, series_id)
            _log(f"[{name}] MD aggregate volume remap applied")
        except Exception as e:
            _log(f"[{name}] aggregate volume remap skipped: {e}")

    def _chapter_stem_sync_pass() -> int:
        m = _rename_tracked_chapter_stems_for_template(
            conn, series_row, settings, name, side_fx
        )
        if side_fx is not None and m:
            side_fx["chapter_stems_renamed"] = side_fx.get("chapter_stems_renamed", 0) + m
        for _kind in ("volume", "chapter"):
            _rename_stems_for_locale(conn, series_row, settings, meta, name,
                                     side_fx, kind=_kind)
        return m

    stem_renames = _chapter_stem_sync_pass()

    if not to_download:
        _log(f"[{name}] up-to-date")
        _ensure_comicinfo_all(
            series_id, series_dir, conn, meta, language,
            series_web=series_web, series_label=name,
            file_permission_mask=file_permission_mask,
            side_fx=side_fx,
            series_row=series_row, settings=settings,
        )
        if kavita_client and settings.get("auto_covers"):
            mdx_id = mangadex_id_for_series(series_row)
            if mdx_id:
                try:
                    cpref, colang = _cover_args(conn, series_row, settings)
                    _kavita_set_covers(kavita_client, series_dir, mdx_id,
                                       conn=conn, series_id=series_id,
                                       cover_preference=cpref,
                                       original_language=colang,
                                       display_title=_series_display_title(
                                           conn, series_row, settings))
                except Exception as e:
                    _log(f"[{name}] kavita covers: {e}")
        return False

    delay         = float(settings.get("download_delay", 1.0))
    file_format   = settings.get("file_format", "cbz")
    ch_naming     = settings.get("chapter_naming", "%3 ch.%5")
    downloaded    = 0
    _COMMIT_BATCH = int(os.environ.get("SYNC_COMMIT_BATCH", "1"))
    _uncommitted  = 0

    for ch_row in to_download:
        # Re-check in case scan_disk_files already found it
        fresh = conn.execute(
            "SELECT path FROM chapters WHERE id=?", (ch_row["id"],)
        ).fetchone()
        if fresh and fresh["path"]:
            downloaded += 1
            continue

        _log(f"[{name}] ch.{ch_row['chapter_num']}: downloading…")

        vol_num = None
        if ch_row.get("volume_id"):
            vr = conn.execute(
                "SELECT volume_num FROM volumes WHERE id=?",
                (ch_row["volume_id"],),
            ).fetchone()
            if vr:
                vol_num = vr["volume_num"]
        desired_stem = apply_naming_template(
            ch_naming,
            language=language,
            group=ch_row.get("group_name") or "",
            title=_filename_title_for(series_row, meta, settings, name),
            volume_num=vol_num,
            chapter_num=ch_row["chapter_num"],
            chapter_title=ch_row.get("title") or "",
        )

        if isinstance(source, SuwayomiSource):
            # Suwayomi (e.g. MANGA Plus extension): pages via Suwayomi REST — not ``mdx``.
            ok = source.download_chapter(
                ch_row["source_chapter_id"],
                series_dir,
                file_format,
                desired_stem or f"ch.{ch_row['chapter_num']}",
                delay=0.0,
            )
            if ok:
                fpath = os.path.join(series_dir, f"{desired_stem or 'ch.' + str(ch_row['chapter_num'])}.{file_format}")
                if os.path.exists(fpath):
                    if not _manga_archive_passes_sanity_check(fpath):
                        try:
                            bad_sz = os.path.getsize(fpath)
                        except OSError:
                            bad_sz = 0
                        _log(
                            f"[{name}] ch.{ch_row['chapter_num']}: Suwayomi download produced unusable output "
                            f"({bad_sz} bytes). Removed file. Path: {fpath}"
                        )
                        with contextlib.suppress(OSError):
                            os.remove(fpath)
                    else:
                        rel = os.path.relpath(fpath, MANGA_ROOT)
                        apply_file_permission_mask(fpath, file_permission_mask)
                        mark_chapter_downloaded(conn, ch_row["id"], rel, os.path.getsize(fpath), commit=False)
                        downloaded += 1
                        _uncommitted += 1
                else:
                    _log(f"[{name}] ch.{ch_row['chapter_num']}: file not found after download")
            else:
                _log(f"[{name}] ch.{ch_row['chapter_num']}: failed")
        else:
            # MangaDex: mdx CLI writes its own filename, then we rename
            before = _cbz_files_in(series_dir)
            ok     = _mdx_download(ch_row, series_dir, file_format, ch_naming)
            if ok:
                fpath = _find_new_file(series_dir, before, ch_row["chapter_num"])
                if fpath:
                    if not _manga_archive_passes_sanity_check(fpath):
                        try:
                            bad_sz = os.path.getsize(fpath)
                        except OSError:
                            bad_sz = 0
                        _log(
                            f"[{name}] ch.{ch_row['chapter_num']}: download produced unusable output "
                            f"({bad_sz} bytes; expected a real archive). Removed junk file — chapter stays "
                            f"queued for retry. Path: {fpath}"
                        )
                        with contextlib.suppress(OSError):
                            os.remove(fpath)
                    else:
                        if desired_stem:
                            ext = os.path.splitext(fpath)[1]
                            desired_path = os.path.join(series_dir, desired_stem + ext)
                            if os.path.abspath(desired_path) != os.path.abspath(fpath):
                                if not os.path.exists(desired_path):
                                    os.rename(fpath, desired_path)
                                    fpath = desired_path
                                else:
                                    _log(f"[{name}] ch.{ch_row['chapter_num']}: naming target exists, keeping existing file")
                                    with contextlib.suppress(OSError):
                                        os.remove(fpath)
                                    fpath = desired_path
                        rel = os.path.relpath(fpath, MANGA_ROOT)
                        apply_file_permission_mask(fpath, file_permission_mask)
                        mark_chapter_downloaded(conn, ch_row["id"], rel, os.path.getsize(fpath), commit=False)
                        downloaded += 1
                        _uncommitted += 1
                else:
                    _log(f"[{name}] ch.{ch_row['chapter_num']}: file not found after download")
            else:
                _log(f"[{name}] ch.{ch_row['chapter_num']}: failed")

        if _uncommitted >= _COMMIT_BATCH:
            conn.commit()
            _uncommitted = 0
        time.sleep(delay)

    conn.commit()  # flush any remaining uncommitted chapter marks
    # Filename cleanup (skip series excluded from Fix Files / manual filenames)
    if not series_row.get("exclude_from_fix"):
        _run_fix_pass(series_dir)
    # Re-scan after rename pass (or after download when fix pass skipped)
    scan_disk_files(series_dir, series_id, conn)

    _chapter_stem_sync_pass()

    # ComicInfo injection for all remaining files
    _ensure_comicinfo_all(
        series_id, series_dir, conn, meta, language,
        series_web=series_web, series_label=name,
        file_permission_mask=file_permission_mask,
        side_fx=side_fx,
        series_row=series_row, settings=settings,
    )

    # Kavita covers
    if kavita_client and settings.get("auto_covers"):
        mdx_id_for_covers = mangadex_id_for_series(series_row)
        if mdx_id_for_covers:
            cpref, colang = _cover_args(conn, series_row, settings)
            _kavita_set_covers(kavita_client, series_dir, mdx_id_for_covers,
                               conn=conn, series_id=series_id,
                               cover_preference=cpref, original_language=colang,
                               display_title=_series_display_title(
                                   conn, series_row, settings))

    update_source_sync_time(conn, series_id, source_name)
    _log(f"[{name}] done - {downloaded}/{len(to_download)} downloaded")
    return downloaded


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _send_webhook(url: str, platform: str, items: list[tuple[str, int]]):
    lines = [
        f"• {name} - {count} new chapter{'s' if count != 1 else ''}"
        for name, count in items
    ]
    text = "New chapters downloaded:\n" + "\n".join(lines)
    try:
        if platform == "ntfy":
            body = text.encode()
            req = request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", "text/plain")
        else:  # discord, generic, or anything else
            body = json.dumps({"content": text}).encode()
            req = request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "manga-sync/1.0")
        with request.urlopen(req, timeout=10):
            pass
        _log("[webhook] notification sent")
    except Exception as e:
        _log(f"[webhook] failed: {e}")


def main(
    series_filter: str = None,
    covers_only: bool = False,
    compact_volumes: bool = False,
    refresh_volume_comicinfo: bool = False,
    normalize_volume_filenames: bool = False,
    regenerate_comicinfo: bool = False,
    notify: bool = False,
):
    _rotate_log()
    _job_start = time.monotonic()

    conn = init_db(DATA_DIR)
    migrated = migrate_json_configs(MANGA_ROOT, conn)
    settings = load_settings()
    roots = [rf for rf in (settings.get("root_folders") or []) if rf is not None]
    added = scan_disk_series(MANGA_ROOT, conn, allowed_roots=roots)
    if migrated:
        _log(f"[startup] migrated {migrated} .mangadex.json config(s) to DB")
    if added:
        _log(f"[startup] catalogued {added} unlinked series from disk")

    kavita_client = None
    kurl = settings.get("kavita_url", "").strip()
    kkey = settings.get("kavita_api_key", "").strip()
    if kurl and kkey and (settings.get("auto_scan") or settings.get("auto_covers") or covers_only):
        kavita_client = KavitaClient(kurl, kkey)

    if series_filter:
        rel_path  = os.path.relpath(series_filter, MANGA_ROOT)
        series_list = [s for s in [get_series_by_path(conn, rel_path)] if s]
    else:
        series_list = get_all_series(conn)
        series_list = [s for s in series_list if not s.get("ignored")]
        if roots and "" not in roots:
            filtered: list[dict] = []
            for s in series_list:
                p = (s.get("path") or "").replace("\\", "/").strip()
                if any(p == rf or p.startswith(rf + "/") for rf in roots):
                    filtered.append(s)
            series_list = filtered

    if covers_only:
        for s in series_list:
            mdx_id = mangadex_id_for_series(s)
            if mdx_id and kavita_client:
                cpref, colang = _cover_args(conn, s, settings)
                _kavita_set_covers(kavita_client,
                                   os.path.join(MANGA_ROOT, s["path"]),
                                   mdx_id,
                                   conn=conn, series_id=s["id"], force=True,
                                   cover_preference=cpref,
                                   original_language=colang,
                                   display_title=_series_display_title(
                                       conn, s, settings))
        conn.close()
        return

    if compact_volumes:
        if not series_filter:
            _log("[compact] --series is required")
            conn.close()
            return
        series_dir = os.path.abspath(series_filter)
        try:
            rel_path = os.path.relpath(series_dir, os.path.abspath(MANGA_ROOT))
        except ValueError:
            _log("[compact] series path must be under MANGA_ROOT")
            conn.close()
            return
        row = get_series_by_path(conn, rel_path)
        if not row:
            _log(f"[compact] unknown series: {rel_path}")
            conn.close()
            return
        merged, errs = compact_series_volumes(row, conn, settings)
        for e in errs:
            _log(f"[compact] {e}")
        _log(f"[compact] finished - {merged} volume(s) merged")
        conn.close()
        return

    if refresh_volume_comicinfo:
        if not series_filter:
            _log("[comicinfo-refresh] --series is required")
            conn.close()
            return
        series_dir = os.path.abspath(series_filter)
        try:
            rel_path = os.path.relpath(series_dir, os.path.abspath(MANGA_ROOT))
        except ValueError:
            _log("[comicinfo-refresh] series path must be under MANGA_ROOT")
            conn.close()
            return
        row = get_series_by_path(conn, rel_path)
        if not row:
            _log(f"[comicinfo-refresh] unknown series: {rel_path}")
            conn.close()
            return
        refresh_volume_comicinfo_embeds(row, conn, settings=settings)
        conn.close()
        return

    if normalize_volume_filenames:
        if not series_filter:
            _log("[kavita-rename] --series is required")
            conn.close()
            return
        series_dir = os.path.abspath(series_filter)
        try:
            rel_path = os.path.relpath(series_dir, os.path.abspath(MANGA_ROOT))
        except ValueError:
            _log("[kavita-rename] series path must be under MANGA_ROOT")
            conn.close()
            return
        row = get_series_by_path(conn, rel_path)
        if not row:
            _log(f"[kavita-rename] unknown series: {rel_path}")
            conn.close()
            return
        normalize_volume_filenames_for_kavita(row, conn)
        conn.close()
        return

    if regenerate_comicinfo:
        if not series_filter:
            _log("[comicinfo-regenerate] --series is required")
            conn.close()
            return
        series_dir = os.path.abspath(series_filter)
        try:
            rel_path = os.path.relpath(series_dir, os.path.abspath(MANGA_ROOT))
        except ValueError:
            _log("[comicinfo-regenerate] series path must be under MANGA_ROOT")
            conn.close()
            return
        row = get_series_by_path(conn, rel_path)
        if not row:
            _log(f"[comicinfo-regenerate] unknown series: {rel_path}")
            conn.close()
            return
        _log(f"[comicinfo-regenerate] scanning files for {row.get('name', row['path'])}…")
        scan_disk_files(series_dir, row["id"], conn)
        # Force all tracked on-disk files through ComicInfo rewrite, even if
        # they previously had ComicInfo. This is an explicit reset action.
        conn.execute(
            "UPDATE chapters SET has_comicinfo=0 WHERE series_id=? AND path IS NOT NULL",
            (row["id"],),
        )
        conn.execute(
            "UPDATE volumes SET has_comicinfo=0 WHERE series_id=? AND path IS NOT NULL",
            (row["id"],),
        )
        conn.commit()
        meta = get_series_metadata(conn, row["id"]) or {}
        _src = _get_source(row.get("source_name") or "mangadex")
        _ensure_comicinfo_all(
            row["id"],
            series_dir,
            conn,
            meta,
            row.get("language", "en"),
            series_web=(_src.get_web_url(row["source_id"]) if row.get("source_id") else None),
            series_label=row.get("name", row["path"]),
            series_row=row, settings=settings,
            force_overwrite=True,
            file_permission_mask=settings.get("file_permission_mask"),
        )
        _log("[comicinfo-regenerate] done")
        conn.close()
        return

    any_downloaded = False
    failures: list[tuple[str, str]] = []
    processed = 0
    downloads: list[tuple[str, int]] = []
    skip_feed_counts: dict[str, int] = {"not_configured": 0, "paused": 0}
    side_fx: dict[str, int] = {"chapter_stems_renamed": 0, "library_write_denied": 0}

    for series_row in series_list:
        processed += 1
        if not series_row.get("source_id"):
            # Unlinked: reconcile disk and inject ComicInfo where possible
            series_name = series_row.get("name", series_row["path"])
            _log(f"[{series_name}] local-only series (not linked)")
            series_dir = os.path.join(MANGA_ROOT, series_row["path"])
            _log(f"[{series_name}] scanning files…")
            scan_disk_files(series_dir, series_row["id"], conn)
            _log(f"[{series_name}] scan done, checking ComicInfo…")
            _ensure_comicinfo_all(
                series_row["id"], series_dir, conn,
                get_series_metadata(conn, series_row["id"]) or {},
                series_row.get("language", "en"),
                series_web=None,
                series_label=series_name,
                file_permission_mask=settings.get("file_permission_mask"),
                series_row=series_row, settings=settings,
            )
            continue

        try:
            count = _sync_one_series(
                series_row, conn, settings, kavita_client, skip_feed_counts, side_fx
            )
            if count:
                any_downloaded = True
                downloads.append(
                    (_series_display_title(conn, series_row, settings), count))
        except Exception as e:
            name = series_row.get("name") or os.path.basename(series_row["path"])
            summary = _friendly_sync_error(e)
            set_series_sync_error(conn, series_row["id"], summary)
            failures.append((name, summary))
            _log(f"[{name}] error: {summary}")

    stems_renamed = int(side_fx.get("chapter_stems_renamed") or 0)
    if kavita_client and settings.get("auto_scan") and (any_downloaded or stems_renamed):
        try:
            kavita_client.scan_all()
            bits: list[str] = []
            if any_downloaded:
                bits.append("new downloads")
            if stems_renamed:
                bits.append(f"{stems_renamed} chapter stem rename(s)")
            _log("[kavita] library scan triggered (" + ", ".join(bits) + ")")
        except Exception as e:
            _log(f"[kavita] scan failed: {e}")

    denied = int(side_fx.get("library_write_denied") or 0)
    if denied:
        _log(
            "[sync] Library writes: "
            f"{denied} operation(s) could not complete because of file or folder permissions "
            "(see the per-file lines above). Renames and ComicInfo injection need write access "
            "to the archive and its directory. If that is intentional, you can ignore this; "
            "otherwise fix ownership (e.g. host chown/chmod or Docker PUID/PGID) on the bind mount."
        )

    if notify and downloads:
        wurl = settings.get("webhook_url", "").strip()
        if wurl:
            _send_webhook(wurl, settings.get("webhook_platform", "generic"), downloads)

    segments: list[str] = [f"processed {processed} series"]
    if downloads:
        n_ch = sum(c for _, c in downloads)
        n_sr = len(downloads)
        ch_word = "chapter" if n_ch == 1 else "chapters"
        segments.append(f"{n_sr} series with {n_ch} new {ch_word}")
    nc = skip_feed_counts.get("not_configured", 0)
    pz = skip_feed_counts.get("paused", 0)
    if nc or pz:
        parts: list[str] = []
        if nc:
            parts.append(f"{nc} not configured")
        if pz:
            parts.append(f"{pz} paused")
        segments.append(f"feed skipped: {', '.join(parts)}")
    if failures:
        segments.append(f"{len(failures)} failed")
    outcome = "failed" if failures else "completed"
    msg = f"[sync] {outcome} - " + "; ".join(segments)
    _log(msg)
    try:
        import db as _db_mod
        _db_mod.record_job_timing(conn, "sync", time.monotonic() - _job_start)
    except Exception:
        pass
    conn.close()
    return 1 if failures else 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--series", help="Absolute path to a single series directory")
    parser.add_argument("--covers-only", action="store_true")
    parser.add_argument(
        "--compact-volumes",
        action="store_true",
        help="Merge chapter CBZs into volume files (requires --series); irreversible",
    )
    parser.add_argument(
        "--refresh-volume-comicinfo",
        action="store_true",
        help="Rewrite ComicInfo.xml in volume CBZs for Kavita (requires --series)",
    )
    parser.add_argument(
        "--normalize-volume-filenames",
        action="store_true",
        help="Strip ch.X-Y suffix from merged volume stems (requires --series)",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Send webhook notification if new chapters were downloaded",
    )
    parser.add_argument(
        "--regenerate-comicinfo",
        action="store_true",
        help="Force rewrite ComicInfo.xml for all chapter/volume files in a series (requires --series)",
    )
    args = parser.parse_args()
    raise SystemExit(main(
        series_filter=args.series,
        covers_only=args.covers_only,
        compact_volumes=args.compact_volumes,
        refresh_volume_comicinfo=args.refresh_volume_comicinfo,
        normalize_volume_filenames=args.normalize_volume_filenames,
        regenerate_comicinfo=args.regenerate_comicinfo,
        notify=args.notify,
    ))
