#!/usr/bin/env python3
"""manga-sync — download new chapters, merge volumes, inject ComicInfo.xml."""

import importlib.util
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from urllib import error as urlerror
from urllib import parse, request

MANGA_ROOT = os.environ.get("MANGA_ROOT", "/manga")
DATA_DIR   = os.environ.get("DATA_DIR",   "/data")
SYNC_LOG   = os.environ.get("SYNC_LOG",   "/data/logs/sync.log")
SYNC_LOG_MAX_LINES = 5000
MDEX_BASE       = "https://api.mangadex.org"
CONTENT_RATINGS = ["safe", "suggestive", "erotica", "pornographic"]

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
    get_complete_volumes, get_volumes_needing_compact,
    get_files_missing_comicinfo,
    mark_chapter_downloaded, mark_chapter_comicinfo,
    mark_volume_merged, mark_volume_comicinfo,
    assign_chapter_to_volume, scan_disk_files, log_rename,
)
from comicinfo import (                                 # noqa: E402
    build_comicinfo_xml, count_pages, inject_comicinfo,
    merge_chapters_into_volume, read_first_image_bytes,
)
from sync_config import load_settings, sanitize_volume_naming  # noqa: E402
from kavita import KavitaClient                        # noqa: E402
from file_permissions import apply_file_permission_mask # noqa: E402


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
# MangaDex API
# ---------------------------------------------------------------------------

def _api_get(path: str, params: dict) -> dict:
    url = f"{MDEX_BASE}{path}?" + parse.urlencode(params, doseq=True)
    try:
        with request.urlopen(url, timeout=30) as resp:
            return __import__("json").loads(resp.read())
    except urlerror.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} for {url}") from e


def fetch_volume_covers(manga_id: str) -> dict[str, str]:
    """Map volume number string → MangaDex cover URL (.512.jpg), for tests and tooling."""
    data = _api_get("/cover", {
        "manga[]": manga_id, "limit": 100, "order[volume]": "asc",
    })
    covers: dict[str, str] = {}
    for item in data.get("data", []):
        attr = item["attributes"]
        vol = attr.get("volume")
        fname = attr.get("fileName")
        if vol and fname:
            covers[str(vol)] = (
                f"https://uploads.mangadex.org/covers/{manga_id}/{fname}.512.jpg"
            )
    return covers


def _ensure_volume_cover_urls(conn, series_id: int, manga_id: str):
    """Cache per-volume cover URLs in the DB (same source as sync merge / Nagatoro-style rips)."""
    try:
        for vol_str, url in fetch_volume_covers(manga_id).items():
            try:
                upsert_volume(conn, series_id, float(vol_str), cover_url=url)
            except (ValueError, TypeError):
                pass
        conn.commit()
    except Exception:
        pass


def _group_name(chapter_data: dict) -> str:
    for rel in chapter_data.get("relationships", []):
        if rel["type"] == "scanlation_group":
            return rel.get("attributes", {}).get("name", "") or ""
    return ""


class _Ch:
    __slots__ = ("ch_id", "ch_str", "ch_num", "volume", "group", "title", "publish_date")

    def __init__(self, data: dict):
        attr = data["attributes"]
        self.ch_id        = data["id"]
        self.ch_str       = attr.get("chapter") or "0"
        self.ch_num       = float(self.ch_str)
        self.volume       = attr.get("volume")
        self.group        = _group_name(data)
        self.title        = (attr.get("title") or "").strip() or None
        self.publish_date = (attr.get("publishAt") or "")[:10] or None


def _feed(manga_id: str, lang: str, params_extra: dict = None):
    params = {
        "translatedLanguage[]": lang,
        "limit": 100,
        "includes[]": "scanlation_group",
        "contentRating[]": CONTENT_RATINGS,
    }
    if params_extra:
        params.update(params_extra)
    offset = 0
    while True:
        params["offset"] = offset
        data  = _api_get(f"/manga/{manga_id}/feed", params)
        items = data.get("data", [])
        total = data.get("total", 0)
        yield from items
        offset += len(items)
        if offset >= total or not items:
            break
        time.sleep(0.4)


# ---------------------------------------------------------------------------
# Metadata fetch & cache
# ---------------------------------------------------------------------------

def _fetch_and_cache_meta(
    manga_id: str, series_id: int, source: str, language: str, conn
) -> dict:
    try:
        data = _api_get(f"/manga/{manga_id}", {
            "includes[]": ["author", "artist", "cover_art"],
        })
        attr = data["data"]["attributes"]

        titles = attr.get("title") or {}
        title  = titles.get("en") or next(iter(titles.values()), "")

        desc_map    = attr.get("description") or {}
        description = desc_map.get("en") or next(iter(desc_map.values()), "") or None

        tags = [
            t["attributes"]["name"]["en"]
            for t in (attr.get("tags") or [])
            if t.get("attributes", {}).get("name", {}).get("en")
        ]

        authors, artists = [], []
        cover_filename   = None
        for rel in data["data"].get("relationships", []):
            rtype = rel.get("type")
            rname = (rel.get("attributes") or {}).get("name", "")
            if rname:
                if rtype == "author":
                    authors.append(rname)
                elif rtype == "artist":
                    artists.append(rname)
            if rtype == "cover_art":
                cover_filename = (rel.get("attributes") or {}).get("fileName")

        # Canonical tankōbon count: prefer attributes.lastVolume; fall back
        # to /aggregate **without** a language filter (filtering by language
        # under-counts series that aren't fully translated, e.g. only EN vols
        # 1–11 published when the manga has 15 in print).
        total_vols = 0
        last_vol_raw = (attr.get("lastVolume") or "").strip()
        if last_vol_raw:
            try:
                lv = int(float(last_vol_raw))
                if lv > 0:
                    total_vols = lv
            except (ValueError, TypeError):
                pass
        if not total_vols:
            try:
                agg = _api_get(f"/manga/{manga_id}/aggregate", {})
                vols = agg.get("volumes") or {}
                total_vols = len([k for k in vols if k not in ("none", "0")])
            except Exception:
                total_vols = 0

        # Cache per-volume cover URLs so the merge step can embed them
        try:
            covers_data = _api_get("/cover", {
                "manga[]": manga_id, "limit": 100, "order[volume]": "asc",
            })
            for item in covers_data.get("data", []):
                cover_attr = item["attributes"]
                vol_str    = cover_attr.get("volume")
                fname      = cover_attr.get("fileName")
                if vol_str and fname:
                    try:
                        cover_url = (
                            f"https://uploads.mangadex.org/covers/{manga_id}/{fname}.512.jpg"
                        )
                        upsert_volume(conn, series_id, float(vol_str),
                                      cover_url=cover_url)
                    except (ValueError, TypeError):
                        pass
            conn.commit()
        except Exception:
            pass

        upsert_series_metadata(conn, series_id, source,
            title=title,
            description=description,
            tags=tags,
            authors=authors,
            artists=artists,
            year=attr.get("year"),
            status=attr.get("status"),
            content_rating=attr.get("contentRating"),
            total_volumes=total_vols,
            cover_filename=cover_filename,
        )
    except Exception as e:
        _log(f"  [warn] metadata fetch failed: {e}")

    return get_series_metadata(conn, series_id, source) or {}


# ---------------------------------------------------------------------------
# Chapter feed → DB
# ---------------------------------------------------------------------------

def _series_preferred_groups(series_row: dict) -> list[str]:
    pg = series_row.get("preferred_groups")
    if pg is not None:
        return list(pg)
    return preferred_groups_list_from_row(series_row)


def _sync_chapters_to_db(
    manga_id: str, language: str, preferred_groups: list[str],
    series_id: int, conn,
):
    """Fetch full chapter feed, pick best per chapter_num, upsert to DB."""
    all_candidates: dict[float, list] = {}
    for item in _feed(manga_id, language, {"order[chapter]": "asc"}):
        ch_str = item["attributes"].get("chapter")
        if not ch_str:
            continue
        try:
            ch_num = float(ch_str)
        except ValueError:
            continue
        all_candidates.setdefault(ch_num, []).append((item["id"], _Ch(item)))

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

        vol_id = None
        if picked.volume and picked.volume not in ("none", ""):
            try:
                vol_id = upsert_volume(conn, series_id, float(picked.volume))
            except (ValueError, TypeError):
                pass

        # Never propagate per-chapter feed metadata (title, group, source_id,
        # publish_date) onto a row whose file is already on disk. The whole
        # point of ``status='on_disk'`` is that ComicInfo for that file uses
        # series-level info only — re-tagging it on every sync would undo
        # that. We still ensure the volume mapping so merge logic can reason
        # about coverage.
        existing = conn.execute(
            "SELECT id, path, status FROM chapters"
            " WHERE series_id=? AND chapter_num=?",
            (series_id, ch_num),
        ).fetchone()
        if existing and (existing["path"] or existing["status"] == "on_disk"):
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


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

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

def _apply_template(template: str, lang: str, group: str, title: str, vol_num,
                    ch_range: str = "") -> str:
    def _safe(s):
        return re.sub(r'[<>:"/\\|?*]', "_", str(s or ""))

    result = template
    result = result.replace("%1", _safe(lang))
    result = result.replace("%2", _safe(group))
    result = result.replace("%3", _safe(title))
    result = result.replace("%4", str(int(vol_num)) if vol_num is not None else "")
    result = result.replace("%5", ch_range).replace("%6", "")
    # Drop empty grouping wrappers left by missing placeholders like %2.
    result = re.sub(r"\(\s*\)", "", result)
    result = re.sub(r"\[\s*\]", "", result)
    result = re.sub(r"\{\s*\}", "", result)
    result = re.sub(r"\s+([)\]}])", r"\1", result)
    result = re.sub(r"([(\[{])\s+", r"\1", result)
    result = re.sub(r"\s{2,}", " ", result).strip()
    return result


def _apply_chapter_template(
    template: str,
    *,
    lang: str,
    group: str,
    title: str,
    vol_num,
    chapter_num,
    chapter_title: str | None,
) -> str:
    def _safe(s):
        return re.sub(r'[<>:"/\\|?*]', "_", str(s or ""))

    result = template or ""
    result = result.replace("%1", _safe(lang))
    result = result.replace("%2", _safe(group))
    result = result.replace("%3", _safe(title))
    result = result.replace("%4", str(int(vol_num)) if vol_num is not None else "")
    result = result.replace("%5", _fmt_chapter_token(chapter_num) if chapter_num is not None else "")
    result = result.replace("%6", _safe(chapter_title or ""))
    # Drop empty grouping wrappers left by missing placeholders like %2 or %6.
    result = re.sub(r"\(\s*\)", "", result)
    result = re.sub(r"\[\s*\]", "", result)
    result = re.sub(r"\{\s*\}", "", result)
    result = re.sub(r"\s+([)\]}])", r"\1", result)
    result = re.sub(r"([(\[{])\s+", r"\1", result)
    result = re.sub(r"\s{2,}", " ", result).strip()
    return result


def _fmt_chapter_for_range(n) -> str:
    return str(math.floor(float(n)))


def _fmt_chapter_token(n) -> str:
    num = float(n)
    return str(int(num)) if num.is_integer() else str(num)


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
    putting ``ch.1-7``-style text in ``Title`` — combined with a filename that
    also contains ``ch.*``, Kavita may show duplicate pseudo-chapters. Put the
    span in ``Summary`` only and use a filename without ``ch.*`` (see
    ``volume_naming`` in settings).
    """
    desc = (meta or {}).get("description")
    if chapter_lo is not None and chapter_hi is not None:
        ch_range = (
            _fmt_chapter_for_range(chapter_lo)
            if chapter_lo == chapter_hi
            else f"{_fmt_chapter_for_range(chapter_lo)}-{_fmt_chapter_for_range(chapter_hi)}"
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
    series_web  = f"https://mangadex.org/title/{manga_id}" if manga_id else None
    series_title = (meta or {}).get("title") or os.path.basename(series_dir)
    label       = series_row.get("name", series_path)

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
) -> tuple[int, list[str]]:
    """Merge listed volumes; returns (success_count, error_messages)."""
    volume_naming = sanitize_volume_naming(settings.get("volume_naming"))
    file_format   = settings.get("file_format", "cbz")
    series_title  = (meta or {}).get("title") or os.path.basename(series_dir)
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
        base_name  = _apply_template(volume_naming, language or "en",
                                     group_name, series_title, vol_num, "")
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

        cover_bytes = None
        cover_url   = vol_row.get("cover_url")
        if cover_url:
            try:
                req = request.Request(cover_url, headers={
                    "Referer":    "https://mangadex.org/",
                    "User-Agent": "Mozilla/5.0",
                })
                cover_bytes = request.urlopen(req, timeout=15).read()
            except Exception:
                pass
        if not cover_bytes and ch_paths:
            cover_bytes = read_first_image_bytes(ch_paths[0])

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


def _merge_complete_volumes(
    series_id: int, series_dir: str, conn,
    meta: dict, language: str, preferred_group: str | None,
    settings: dict, series_web: str | None = None,
):
    complete = get_complete_volumes(conn, series_id)
    if not complete:
        return
    _merge_volume_batch(
        series_id, series_dir, conn, complete,
        meta, language, preferred_group, settings, series_web=series_web,
    )


def compact_series_volumes(series_row: dict, conn, settings: dict) -> tuple[int, list[str]]:
    """Manual/UI: merge every volume that has chapter files but no merged CBZ yet."""
    series_id   = series_row["id"]
    series_path = series_row["path"]
    series_dir  = os.path.join(MANGA_ROOT, series_path)
    manga_id    = series_row.get("source_id")
    source_name = series_row.get("source_name") or "mangadex"
    label       = series_row.get("name") or os.path.basename(series_path)

    scan_disk_files(series_dir, series_id, conn)
    if manga_id:
        _ensure_volume_cover_urls(conn, series_id, manga_id)

    meta = get_series_metadata(conn, series_id, source_name) or {}
    vol_nums = get_volumes_needing_compact(conn, series_id)
    if not vol_nums:
        return 0, []

    series_web = f"https://mangadex.org/title/{manga_id}" if manga_id else None
    _log(f"[{label}] compact into volumes: {len(vol_nums)} volume(s)…")
    _pg = _series_preferred_groups(series_row)
    merged, errors = _merge_volume_batch(
        series_id, series_dir, conn, vol_nums,
        meta,
        series_row.get("language", "en"),
        _pg[0] if _pg else None,
        settings,
        series_web=series_web,
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
):
    ch_rows, vol_rows = get_files_missing_comicinfo(conn, series_id)
    if not ch_rows and not vol_rows:
        return
    label = series_label or os.path.basename(series_dir)
    _log(f"[{label}] ComicInfo pass: {len(ch_rows)} chapter file(s), {len(vol_rows)} volume file(s)")

    series_title   = (meta or {}).get("title") or os.path.basename(series_dir)
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
        # ``on_disk`` rows never carry trustworthy per-chapter source info —
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
    _log(f"[{label}] ComicInfo done: {injected_ch} chapter(s), {injected_vol} volume(s) updated")


# ---------------------------------------------------------------------------
# Fix pass + Kavita
# ---------------------------------------------------------------------------

def _run_fix_pass(series_dir: str):
    log_path = os.path.join(MANGA_ROOT, _fix.LOG_FILENAME)
    log_data = _fix.load_log(log_path)
    for fpath, issue_name, new_name in _fix.scan(series_dir):
        _fix.do_rename(fpath, new_name, issue_name, log_data, log_path)


def _kavita_set_covers(client: KavitaClient, series_dir: str, manga_id: str):
    series_name = os.path.basename(series_dir)
    try:
        from manga_sync_helpers import fetch_volume_covers  # type: ignore
    except ImportError:
        pass
    try:
        data = _api_get("/cover", {
            "manga[]": manga_id, "limit": 100, "order[volume]": "asc",
        })
        covers: dict[str, str] = {}
        for item in data.get("data", []):
            attr = item["attributes"]
            vol  = attr.get("volume")
            fname = attr.get("fileName")
            if vol and fname:
                covers[vol] = (
                    f"https://uploads.mangadex.org/covers/{manga_id}/{fname}.512.jpg"
                )
        if not covers:
            return
        series = client.search_series(series_name)
        if not series:
            _log(f"[kavita] series not found: {series_name}")
            return
        for vol in client.get_volumes(series["id"]):
            vol_num = str(vol.get("number", ""))
            url     = covers.get(vol_num)
            if not url:
                continue
            try:
                client.set_volume_cover(vol["id"], url)
                _log(f"[kavita] cover set: {series_name} vol.{vol_num}")
            except Exception as e:
                _log(f"[kavita] cover failed vol.{vol_num}: {e}")
    except Exception as e:
        _log(f"[kavita] covers error for {series_name}: {e}")


# ---------------------------------------------------------------------------
# Per-series sync
# ---------------------------------------------------------------------------

def _sync_one_series(
    series_row: dict, conn, settings: dict,
    kavita_client: KavitaClient | None,
) -> bool:
    series_id   = series_row["id"]
    series_path = series_row["path"]
    series_dir  = os.path.join(MANGA_ROOT, series_path)
    language    = series_row.get("language", "en")
    prefs       = _series_preferred_groups(series_row)
    group       = prefs[0] if prefs else None
    start_chapter = float(series_row.get("start_chapter") or 0)
    manga_id    = series_row.get("source_id")
    source_name = series_row.get("source_name") or "mangadex"
    name        = series_row.get("name") or os.path.basename(series_path)
    series_web  = f"https://mangadex.org/title/{manga_id}" if manga_id else None
    file_permission_mask = settings.get("file_permission_mask")
    _log(f"[{name}] checking for updates…")

    # Always reconcile disk state first
    scan_disk_files(series_dir, series_id, conn)

    # Fetch & cache series metadata (7-day TTL)
    meta = get_series_metadata(conn, series_id, source_name)
    if is_metadata_stale(meta and meta.get("fetched_at")):
        meta = _fetch_and_cache_meta(manga_id, series_id, source_name, language, conn)

    # Linked-only (no download options chosen yet): refresh metadata+ComicInfo,
    # do not pull the chapter feed or queue downloads.
    if not series_row.get("sync_configured"):
        _log(f"[{name}] sync not configured — skipping feed/download")
        _ensure_comicinfo_all(
            series_id, series_dir, conn, meta, language,
            series_web=series_web, series_label=name,
            file_permission_mask=file_permission_mask,
        )
        update_source_sync_time(conn, series_id, source_name)
        return False

    # Sync chapter feed → DB
    try:
        _sync_chapters_to_db(manga_id, language, prefs, series_id, conn)
    except Exception as e:
        _log(f"[{name}] feed error: {e}")
        return False

    to_download = get_chapters_to_download(conn, series_id, start_chapter)

    if not to_download:
        _log(f"[{name}] up-to-date")
        _ensure_comicinfo_all(
            series_id, series_dir, conn, meta, language,
            series_web=series_web, series_label=name,
            file_permission_mask=file_permission_mask,
        )
        return False

    delay         = float(settings.get("download_delay", 1.0))
    file_format   = settings.get("file_format", "cbz")
    ch_naming     = settings.get("chapter_naming", "%3 ch.%5")
    downloaded    = 0

    for ch_row in to_download:
        # Re-check in case scan_disk_files already found it
        fresh = conn.execute(
            "SELECT path FROM chapters WHERE id=?", (ch_row["id"],)
        ).fetchone()
        if fresh and fresh["path"]:
            downloaded += 1
            continue

        _log(f"[{name}] ch.{ch_row['chapter_num']}: downloading…")
        before = _cbz_files_in(series_dir)
        ok     = _mdx_download(ch_row, series_dir, file_format, ch_naming)
        if ok:
            fpath = _find_new_file(series_dir, before, ch_row["chapter_num"])
            if fpath:
                vol_num = None
                if ch_row.get("volume_id"):
                    vr = conn.execute(
                        "SELECT volume_num FROM volumes WHERE id=?",
                        (ch_row["volume_id"],),
                    ).fetchone()
                    if vr:
                        vol_num = vr["volume_num"]
                desired_stem = _apply_chapter_template(
                    ch_naming,
                    lang=language,
                    group=ch_row.get("group_name") or "",
                    title=name,
                    vol_num=vol_num,
                    chapter_num=ch_row["chapter_num"],
                    chapter_title=ch_row.get("title"),
                )
                if desired_stem:
                    ext = os.path.splitext(fpath)[1]
                    desired_path = os.path.join(series_dir, desired_stem + ext)
                    if os.path.abspath(desired_path) != os.path.abspath(fpath):
                        if not os.path.exists(desired_path):
                            os.rename(fpath, desired_path)
                            fpath = desired_path
                        else:
                            _log(f"[{name}] ch.{ch_row['chapter_num']}: naming target exists, keeping existing file")
                            # Avoid duplicate archives when mdx emits a suffixed
                            # filename but the normalized target already exists.
                            with contextlib.suppress(OSError):
                                os.remove(fpath)
                            fpath = desired_path
                rel = os.path.relpath(fpath, MANGA_ROOT)
                apply_file_permission_mask(fpath, file_permission_mask)
                mark_chapter_downloaded(conn, ch_row["id"], rel,
                                        os.path.getsize(fpath))
                downloaded += 1
            else:
                _log(f"[{name}] ch.{ch_row['chapter_num']}: file not found after download")
        else:
            _log(f"[{name}] ch.{ch_row['chapter_num']}: failed")

        time.sleep(delay)

    # Legacy UI-level "compact into volume" toggles are intentionally ignored.
    # Volume merge should only run via explicit compact mode CLI path.
    merge_ok = False

    # Filename cleanup (skip series excluded from Fix Files / manual filenames)
    if not series_row.get("exclude_from_fix"):
        _run_fix_pass(series_dir)
    # Re-scan after rename pass (or after download when fix pass skipped)
    scan_disk_files(series_dir, series_id, conn)

    # Volume merge
    if merge_ok:
        _merge_complete_volumes(series_id, series_dir, conn,
                                meta, language, group, settings, series_web=series_web)

    # ComicInfo injection for all remaining files
    _ensure_comicinfo_all(
        series_id, series_dir, conn, meta, language,
        series_web=series_web, series_label=name,
        file_permission_mask=file_permission_mask,
    )

    # Kavita covers
    if kavita_client and settings.get("auto_covers"):
        _kavita_set_covers(kavita_client, series_dir, manga_id)

    update_source_sync_time(conn, series_id, source_name)
    _log(f"[{name}] done — {downloaded}/{len(to_download)} downloaded")
    return downloaded > 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(
    series_filter: str = None,
    covers_only: bool = False,
    compact_volumes: bool = False,
    refresh_volume_comicinfo: bool = False,
    normalize_volume_filenames: bool = False,
    regenerate_comicinfo: bool = False,
):
    _rotate_log()

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
        if roots:
            filtered: list[dict] = []
            for s in series_list:
                p = (s.get("path") or "").replace("\\", "/").strip()
                if any(p == rf or p.startswith(rf + "/") for rf in roots):
                    filtered.append(s)
            series_list = filtered

    if covers_only:
        for s in series_list:
            if s.get("source_id") and kavita_client:
                _kavita_set_covers(kavita_client,
                                   os.path.join(MANGA_ROOT, s["path"]),
                                   s["source_id"])
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
        _log(f"[compact] finished — {merged} volume(s) merged")
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
        _ensure_comicinfo_all(
            row["id"],
            series_dir,
            conn,
            meta,
            row.get("language", "en"),
            series_web=(f"https://mangadex.org/title/{row['source_id']}" if row.get("source_id") else None),
            series_label=row.get("name", row["path"]),
            force_overwrite=True,
            file_permission_mask=settings.get("file_permission_mask"),
        )
        _log("[comicinfo-regenerate] done")
        conn.close()
        return

    any_downloaded = False
    processed = 0

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
            )
            continue

        try:
            if _sync_one_series(series_row, conn, settings, kavita_client):
                any_downloaded = True
        except Exception as e:
            _log(f"[{series_row.get('name', series_row['path'])}] error: {e}")

    if any_downloaded and kavita_client and settings.get("auto_scan"):
        try:
            kavita_client.scan_all()
            _log("[kavita] library scan triggered")
        except Exception as e:
            _log(f"[kavita] scan failed: {e}")

    _log(f"[sync] completed — processed {processed} series")
    conn.close()


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
        "--regenerate-comicinfo",
        action="store_true",
        help="Force rewrite ComicInfo.xml for all chapter/volume files in a series (requires --series)",
    )
    args = parser.parse_args()
    main(
        series_filter=args.series,
        covers_only=args.covers_only,
        compact_volumes=args.compact_volumes,
        refresh_volume_comicinfo=args.refresh_volume_comicinfo,
        normalize_volume_filenames=args.normalize_volume_filenames,
        regenerate_comicinfo=args.regenerate_comicinfo,
    )
