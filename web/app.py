#!/usr/bin/env python3
import asyncio
import contextlib
import importlib.util
import json
import math
import os
import re
import shutil
import sys
import time
from urllib import parse, request as urlrequest

from urllib.parse import quote_plus

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

MANGA_ROOT = os.environ.get("MANGA_ROOT", "/manga")
_MANGA_ROOT_REAL = os.path.realpath(MANGA_ROOT)
DATA_DIR   = os.environ.get("DATA_DIR",   "/data")
SYNC_LOG   = os.environ.get("SYNC_LOG",   "/data/logs/sync.log")

# ISO-style codes aligned with MangaDex ``translatedLanguage`` (ComicInfo LanguageISO).
_SERIES_LANGUAGE_CHOICES_BASE = [
    ("en", "English (en)"),
    ("ja", "Japanese (ja)"),
    ("ko", "Korean (ko)"),
    ("zh", "Chinese (zh)"),
    ("zh-hk", "Chinese Traditional (zh-hk)"),
    ("es", "Spanish (es)"),
    ("fr", "French (fr)"),
    ("de", "German (de)"),
    ("it", "Italian (it)"),
    ("pt-br", "Portuguese Brazil (pt-br)"),
    ("ru", "Russian (ru)"),
    ("pl", "Polish (pl)"),
    ("tr", "Turkish (tr)"),
    ("vi", "Vietnamese (vi)"),
    ("id", "Indonesian (id)"),
    ("th", "Thai (th)"),
    ("ar", "Arabic (ar)"),
]


def _series_language_choices(
    current: str | None,
    available: list[str] | None = None,
) -> list[tuple[str, str]]:
    """Dropdown rows for series-language picker.

    Preserves unknown codes already in the DB (so we never silently mutate a
    stored value), and annotates entries that MangaDex actually has
    translations for so the user picks something the feed can actually
    deliver.
    """
    cur_raw = (current or "en").strip()
    cur_key = cur_raw.lower()
    avail = {(a or "").strip().lower() for a in (available or []) if a}
    base_codes = {c for c, _ in _SERIES_LANGUAGE_CHOICES_BASE}
    rows: list[tuple[str, str]] = []
    for code, label in _SERIES_LANGUAGE_CHOICES_BASE:
        if avail and code in avail:
            rows.append((code, f"{label} · on MangaDex"))
        else:
            rows.append((code, label))
    if cur_key and cur_key not in base_codes:
        rows.insert(0, (cur_key, f"{cur_raw} (stored)"))
    return rows


_DEFAULT_LANGUAGE = os.environ.get("DEFAULT_LANGUAGE", "en").strip().lower() or "en"


def _pick_default_series_language(available: list[str]) -> str:
    """Prefer DEFAULT_LANGUAGE env var; fall back to first available, then 'en'."""
    norm = [(a or "").strip().lower() for a in (available or []) if a]
    if not norm:
        return _DEFAULT_LANGUAGE
    if _DEFAULT_LANGUAGE in norm:
        return _DEFAULT_LANGUAGE
    return norm[0]

_WEB_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_WEB_DIR)
MANGA_SYNC_SCRIPT = os.path.join(_REPO_ROOT, "manga-sync.py")

_spec = importlib.util.spec_from_file_location("manga_fix", os.path.join(_REPO_ROOT, "manga-fix.py"))
_fix  = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fix)

sys.path.insert(0, _REPO_ROOT)
from sync_config import (  # noqa: E402
    load_settings,
    save_settings,
    sanitize_volume_naming,
    sanitize_sync_cron,
    is_sync_cron_disabled,
    get_suwayomi_client,
    _suwayomi_client_cache,
)
from kavita import KavitaClient                        # noqa: E402
import db as _db                                       # noqa: E402
from comicinfo import (                                # noqa: E402
    read_comicinfo_xml,
    parse_comicinfo_fields,
    inject_comicinfo,
)
from comicinfo_defs import MANGA_VALUES, AGE_RATING_VALUES  # noqa: E402
from file_permissions import sanitize_file_permission_mask   # noqa: E402
from naming import apply_naming_template, format_num, floor_int_str  # noqa: E402
from sources.mangadex import MangaDexSource, MDEX_COVERS              # noqa: E402

_mdx = MangaDexSource()

# ---------------------------------------------------------------------------
# Suwayomi source cache (TTL 60 s)
# ---------------------------------------------------------------------------
_suwayomi_cache: dict = {"sources": [], "ts": 0.0}
_SUWAYOMI_CACHE_TTL = 60.0


def _get_suwayomi_sources() -> list[dict]:
    import time as _time
    now = _time.monotonic()
    if now - _suwayomi_cache["ts"] > _SUWAYOMI_CACHE_TTL:
        client = get_suwayomi_client()
        if client:
            try:
                _suwayomi_cache["sources"] = client.list_sources()
            except Exception:
                _suwayomi_cache["sources"] = []
        else:
            _suwayomi_cache["sources"] = []
        _suwayomi_cache["ts"] = now
    return _suwayomi_cache["sources"]


def _enabled_source_keys() -> set[str]:
    """Return set of explicitly enabled Suwayomi source keys (empty = none)."""
    return set(load_settings().get("enabled_sources") or [])


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    # Hermetic pytest: skip workers, crontab, telemetry, startup reconcile.
    if os.environ.get("MANGA_TEST_SKIP_LIFESPAN") == "1":
        yield
        return
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _startup_init)
    cron_expr = sanitize_sync_cron(load_settings().get("sync_cron"))
    try:
        await loop.run_in_executor(None, _write_runtime_crontab, cron_expr)
    except Exception as e:
        print(f"[startup] warning: could not write runtime crontab '{RUNTIME_CRONTAB_PATH}': {e}")
    _job_worker_stop.clear()
    with contextlib.suppress(Exception):
        _enqueue_reconcile_job("startup")
    import telemetry
    loop.run_in_executor(None, telemetry.collect_and_send)
    global _job_worker_task, _reconcile_scheduler_task
    _job_worker_task = asyncio.create_task(_jobs_worker_loop())
    _reconcile_scheduler_task = asyncio.create_task(_reconcile_scheduler_loop())

    yield

    _job_worker_stop.set()
    if _reconcile_scheduler_task:
        _reconcile_scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _reconcile_scheduler_task
        _reconcile_scheduler_task = None
    if _job_worker_task:
        _job_worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _job_worker_task
        _job_worker_task = None


app = FastAPI(title="Manga Maid", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(_WEB_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(_WEB_DIR, "templates"))
templates.env.filters["urlencode"] = quote_plus
templates.env.globals["APP_VERSION"] = os.environ.get("APP_VERSION", "local")

_sync_running = False
_cover_cache: dict[str, bytes] = {}
_COVER_CACHE_MAX = 500
_job_worker_task: asyncio.Task | None = None
_reconcile_scheduler_task: asyncio.Task | None = None
_job_worker_stop = asyncio.Event()
_worker_current_job_id: int | None = None
_worker_current_proc: asyncio.subprocess.Process | None = None
_cancel_requested_job_ids: set[int] = set()
JOB_QUEUE_KEY = "default"
JOB_TYPE_SYNC_ALL = "sync_all"
JOB_TYPE_SYNC_SERIES = "sync_series"
JOB_TYPE_REGEN_COMICINFO = "regenerate_comicinfo"
JOB_TYPE_RECONCILE_DISK = "reconcile_disk"
JOB_RETENTION_DAYS = 30
JOB_STATUS_TERMINAL = {"completed", "failed", "cancelled"}
RUNTIME_CRONTAB_PATH = os.environ.get("RUNTIME_CRONTAB_PATH", "/tmp/crontab")
RECONCILE_INTERVAL_SECONDS = int(os.environ.get("RECONCILE_INTERVAL_SECONDS", "3600"))


def _get_conn() -> "_db.sqlite3.Connection":
    """Return a fresh per-call SQLite connection. Caller owns the lifecycle."""
    return _db.get_conn(DATA_DIR)


def _startup_init() -> None:
    """Blocking startup: apply DB migrations and migrate legacy JSON configs.

    Intentionally does NOT scan disk — that runs in the reconcile job so the
    web server is ready to accept connections before any filesystem I/O starts.
    """
    conn = _db.init_db(DATA_DIR)
    try:
        _db.migrate_json_configs(MANGA_ROOT, conn)
    finally:
        conn.close()


def _write_runtime_crontab(cron_expr: str) -> None:
    """Update the watched crontab file used by supercronic."""
    with open(RUNTIME_CRONTAB_PATH, "w", encoding="utf-8") as f:
        if is_sync_cron_disabled(cron_expr):
            f.write("# Auto-sync disabled — enqueue sync from the Jobs page.\n")
            return
        runas = (os.environ.get("CRON_RUNAS") or "").strip()
        cmd = "python3 /app/cron_enqueue_sync.py"
        line = f"{cron_expr} {runas} {cmd}".strip()
        f.write(line + "\n")


def _enqueue_reconcile_job(reason: str) -> int | None:
    roots = [rf for rf in (load_settings().get("root_folders") or []) if rf is not None]
    if not roots:
        return None
    conn = _get_conn()
    try:
        active = _db.get_active_jobs(conn, queue_key=JOB_QUEUE_KEY)
        for j in active:
            if j.get("job_type") == JOB_TYPE_RECONCILE_DISK:
                return None
        return _db.enqueue_job(
            conn,
            job_type=JOB_TYPE_RECONCILE_DISK,
            queue_key=JOB_QUEUE_KEY,
            payload={"reason": reason},
        )
    finally:
        conn.close()


def _run_disk_reconcile(job_id: int, payload: dict | None = None) -> None:
    """Blocking disk scan — intended to run in a thread-pool executor."""
    conn = _get_conn()
    try:
        payload = payload or {}
        reason = str(payload.get("reason") or "manual").strip()
        _db.append_job_log(conn, job_id, f"[reconcile] started (reason: {reason})")

        roots = [rf for rf in (load_settings().get("root_folders") or []) if rf is not None]
        if not roots:
            _db.append_job_log(conn, job_id, "[reconcile] skipped - no root folders configured")
            _db.append_job_log(conn, job_id, "[reconcile] done")
            return

        added = _db.scan_disk_series(MANGA_ROOT, conn, allowed_roots=roots)
        _db.append_job_log(conn, job_id, f"[reconcile] discovered {added} new series")
        series_rows = _db.get_all_series(conn)
        # Throttle between series to avoid hammering network or slow mounts.
        # Defaults to 20ms; set RECONCILE_SERIES_SLEEP=0 to disable.
        _sleep = float(os.environ.get("RECONCILE_SERIES_SLEEP", "0.02"))
        scanned = 0
        for row in series_rows:
            series_path = row.get("path") or ""
            if roots and "" not in roots:
                if not any(series_path == rf or series_path.startswith(rf + "/") for rf in roots):
                    continue
            series_dir = os.path.join(MANGA_ROOT, series_path)
            _db.scan_disk_files(series_dir, row["id"], conn)
            scanned += 1
            if _sleep:
                time.sleep(_sleep)

        _db.append_job_log(conn, job_id, f"[reconcile] scanned {scanned} tracked series")
        _db.append_job_log(conn, job_id, "[reconcile] done")
    finally:
        conn.close()


async def _reconcile_scheduler_loop() -> None:
    # Non-blocking periodic enqueue; worker processes jobs in queue order.
    while not _job_worker_stop.is_set():
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
        if _job_worker_stop.is_set():
            break
        with contextlib.suppress(Exception):
            _enqueue_reconcile_job("periodic")


def _job_display_name(job: dict) -> str:
    if job.get("job_type") == JOB_TYPE_RECONCILE_DISK:
        return "Disk reconcile"
    if job.get("job_type") == JOB_TYPE_REGEN_COMICINFO:
        return f"Regenerate ComicInfo: {job.get('series_path_snapshot') or '(unknown)'}"
    if job.get("job_type") == JOB_TYPE_SYNC_SERIES:
        return f"Series sync: {job.get('series_path_snapshot') or '(unknown)'}"
    return "Global sync"


def _job_payload_argv(job: dict) -> list[str] | None:
    payload = job.get("payload") or {}
    if job.get("job_type") == JOB_TYPE_SYNC_ALL:
        if payload.get("reason") == "scheduled":
            return ["--notify"]
        return []
    if job.get("job_type") == JOB_TYPE_SYNC_SERIES:
        series_path = payload.get("series_path") or job.get("series_path_snapshot")
        if not series_path:
            return None
        return ["--series", os.path.join(MANGA_ROOT, series_path)]
    if job.get("job_type") == JOB_TYPE_REGEN_COMICINFO:
        series_path = payload.get("series_path") or job.get("series_path_snapshot")
        if not series_path:
            return None
        return ["--series", os.path.join(MANGA_ROOT, series_path), "--regenerate-comicinfo"]
    return None


async def _run_job(worker_conn, job: dict) -> None:
    global _worker_current_proc
    job_id = job["id"]
    if job.get("job_type") == JOB_TYPE_RECONCILE_DISK:
        loop = asyncio.get_event_loop()
        _payload = job.get("payload") or {}
        try:
            await loop.run_in_executor(None, lambda: _run_disk_reconcile(job_id, _payload))
            _db.finish_job(worker_conn, job_id, success=True, exit_code=0)
        except Exception as e:
            _db.append_job_log(worker_conn, job_id, f"[reconcile] failed: {e}")
            _db.finish_job(worker_conn, job_id, success=False, exit_code=1, error_summary=str(e))
        return
    argv = _job_payload_argv(job)
    if argv is None:
        _db.append_job_log(worker_conn, job_id, "[job] unsupported or invalid payload")
        _db.finish_job(worker_conn, job_id, success=False, exit_code=2, error_summary="invalid payload")
        return
    _db.append_job_log(worker_conn, job_id, f"[job] started: {_job_display_name(job)}")
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            MANGA_SYNC_SCRIPT,
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "MANGA_ROOT": MANGA_ROOT, "DATA_DIR": DATA_DIR},
        )
    except Exception as e:
        _db.append_job_log(worker_conn, job_id, f"[job] spawn failed: {e}")
        _db.finish_job(worker_conn, job_id, success=False, exit_code=1, error_summary=str(e))
        return
    _worker_current_proc = proc

    async for line in proc.stdout:
        text = line.decode(errors="replace").rstrip()
        if text:
            _db.append_job_log(worker_conn, job_id, text)
        if job_id in _cancel_requested_job_ids and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()

    await proc.wait()
    _worker_current_proc = None
    was_cancelled = job_id in _cancel_requested_job_ids
    if was_cancelled:
        _cancel_requested_job_ids.discard(job_id)
        _db.append_job_log(worker_conn, job_id, "[job] cancelled")
        _db.mark_job_cancelled(worker_conn, job_id, reason="cancelled by user")
        return
    ok = proc.returncode == 0
    if ok:
        _db.append_job_log(worker_conn, job_id, "[job] done")
        _db.finish_job(worker_conn, job_id, success=True, exit_code=proc.returncode)
    else:
        _db.append_job_log(worker_conn, job_id, f"[job] failed (exit {proc.returncode})")
        _db.finish_job(
            worker_conn,
            job_id,
            success=False,
            exit_code=proc.returncode,
            error_summary=f"exit {proc.returncode}",
        )


async def _jobs_worker_loop():
    global _worker_current_job_id
    worker_conn = _get_conn()
    try:
        recovered = _db.requeue_running_jobs(worker_conn, queue_key=JOB_QUEUE_KEY)
        for jid in recovered:
            with contextlib.suppress(Exception):
                _db.append_job_log(worker_conn, jid, "[job] recovered after app restart; re-queued")
        _db.cleanup_old_jobs(worker_conn, keep_days=JOB_RETENTION_DAYS)
        while not _job_worker_stop.is_set():
            try:
                job = _db.claim_next_queued_job(worker_conn, queue_key=JOB_QUEUE_KEY)
                if not job:
                    await asyncio.sleep(0.4)
                    continue
                _worker_current_job_id = int(job["id"])
                await _run_job(worker_conn, job)
                _worker_current_job_id = None
                _db.cleanup_old_jobs(worker_conn, keep_days=JOB_RETENTION_DAYS)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Keep worker alive even if an unexpected error occurs.
                print(f"[jobs-worker] unexpected error: {e}")
                _worker_current_job_id = None
                with contextlib.suppress(Exception):
                    worker_conn.rollback()
                await asyncio.sleep(1.0)
    finally:
        worker_conn.close()


# ---------------------------------------------------------------------------
# Cover proxy URL helpers
# ---------------------------------------------------------------------------

def _cover_url(manga_id: str, filename: str) -> str:
    return f"/api/proxy/cover/{manga_id}/{filename}"






def _resolve_total_volumes(attr: dict | None, manga_id: str) -> int:
    """Best-effort canonical tankōbon count for parity display."""
    return _mdx.resolve_total_volumes(attr, manga_id)


def _apply_volume_mapping(conn, series_id: int, agg: dict) -> None:
    """Map local chapters to MDX volume buckets using an /aggregate response."""
    _db.apply_aggregate_volume_mapping(conn, series_id, agg)


def _scan_settings_naming_issues(series_path: str | None = None) -> list[tuple[str, str, str]]:
    """Blocking scan — safe to call from executor threads."""
    settings = load_settings()
    chapter_tpl = settings.get("chapter_naming", "%3 ch.%5")
    volume_tpl = settings.get("volume_naming", "%3 vol.%4")
    conn = _get_conn()
    series_scope = (series_path or "").strip()
    findings: list[tuple[str, str, str]] = []
    series_rows = _db.get_all_series(conn)
    if series_scope:
        series_rows = [s for s in series_rows if (s.get("path") or "") == series_scope]
    for series in series_rows:
        _db.scan_disk_files(os.path.join(MANGA_ROOT, series["path"]), series["id"], conn)

    volume_ranges = {
        row["volume_id"]: (row["min_ch"], row["max_ch"])
        for row in conn.execute("""
            SELECT volume_id, MIN(chapter_num) AS min_ch, MAX(chapter_num) AS max_ch
            FROM chapters
            WHERE volume_id IS NOT NULL
            GROUP BY volume_id
        """).fetchall()
    }

    chapter_sql = """
        SELECT
            c.path,
            c.chapter_num,
            c.title AS chapter_title,
            c.group_name,
            c.status AS chapter_status,
            v.volume_num,
            s.path AS series_path,
            s.language AS series_language,
            s.preferred_group
        FROM chapters c
        JOIN series s ON s.id = c.series_id
        LEFT JOIN volumes v ON v.id = c.volume_id
        WHERE c.path IS NOT NULL
          AND COALESCE(s.exclude_from_fix, 0) = 0
          AND COALESCE(s.ignored, 0) = 0
    """
    chapter_params: list[str] = []
    if series_scope:
        chapter_sql += "\n          AND s.path = ?"
        chapter_params.append(series_scope)
    chapter_rows = conn.execute(chapter_sql, chapter_params).fetchall()

    for row in chapter_rows:
        old_path = os.path.join(MANGA_ROOT, row["path"])
        old_name = os.path.basename(old_path)
        old_dir = os.path.dirname(old_path)
        stem, ext = os.path.splitext(old_name)
        # Files we did not download ourselves (status='on_disk') must be
        # named with series-level info only - no per-chapter title/group
        # leaking into the proposed filename.
        is_on_disk = row["chapter_status"] == "on_disk"
        new_stem = apply_naming_template(
            chapter_tpl,
            language=row["series_language"] or "en",
            group="" if is_on_disk else (row["group_name"] or row["preferred_group"] or ""),
            title=os.path.basename(row["series_path"]),
            volume_num=row["volume_num"],
            chapter_num=row["chapter_num"],
            chapter_title="" if is_on_disk else (row["chapter_title"] or ""),
        )
        # Guard against dangerous suggestions that drop chapter identity.
        ch_token = format_num(row["chapter_num"])
        if ch_token and ch_token not in new_stem:
            continue
        if not new_stem or new_stem == stem:
            continue
        new_name = f"{new_stem}{ext}"
        target = os.path.join(old_dir, new_name)
        if target != old_path and not os.path.exists(target):
            findings.append((old_path, "settings_chapter_naming", new_name))

    volume_sql = """
        SELECT
            v.id,
            v.path,
            v.volume_num,
            s.path AS series_path,
            s.language AS series_language,
            s.preferred_group
        FROM volumes v
        JOIN series s ON s.id = v.series_id
        WHERE v.path IS NOT NULL
          AND COALESCE(s.exclude_from_fix, 0) = 0
          AND COALESCE(s.ignored, 0) = 0
    """
    volume_params: list[str] = []
    if series_scope:
        volume_sql += "\n          AND s.path = ?"
        volume_params.append(series_scope)
    volume_rows = conn.execute(volume_sql, volume_params).fetchall()

    for row in volume_rows:
        old_path = os.path.join(MANGA_ROOT, row["path"])
        old_name = os.path.basename(old_path)
        old_dir = os.path.dirname(old_path)
        stem, ext = os.path.splitext(old_name)
        min_max = volume_ranges.get(row["id"])
        ch_range = ""
        if min_max:
            start, end = min_max
            s_start = floor_int_str(start)
            s_end = floor_int_str(end)
            ch_range = s_start if s_start == s_end else f"{s_start}-{s_end}"
        new_stem = apply_naming_template(
            volume_tpl,
            language=row["series_language"] or "en",
            group=row["preferred_group"] or "",
            title=os.path.basename(row["series_path"]),
            volume_num=row["volume_num"],
            chapter_range=ch_range,
        )
        if not new_stem or new_stem == stem:
            continue
        new_name = f"{new_stem}{ext}"
        target = os.path.join(old_dir, new_name)
        if target != old_path and not os.path.exists(target):
            findings.append((old_path, "settings_volume_naming", new_name))

    findings.sort(key=lambda x: x[0].lower())
    conn.close()
    return findings


def _exclude_fix_series_paths(conn) -> set[str]:
    rows = conn.execute(
        "SELECT path FROM series WHERE COALESCE(exclude_from_fix, 0) != 0 OR COALESCE(ignored, 0) != 0"
    ).fetchall()
    return {r["path"] for r in rows}


def _rel_manga_path(abs_path: str) -> str:
    return os.path.relpath(abs_path, MANGA_ROOT).replace("\\", "/")


def _rescan_series_disk_after_fix(reference_path: str) -> None:
    """Refresh chapter/volume paths in SQLite after manga-fix renames or deletes."""
    if not reference_path:
        return
    try:
        ref = os.path.abspath(reference_path)
        root = os.path.abspath(MANGA_ROOT)
    except OSError:
        return
    if ref != root and not ref.startswith(root + os.sep):
        return
    series_dir = os.path.dirname(ref)
    rel_series = os.path.relpath(series_dir, root).replace("\\", "/")
    conn = _get_conn()
    row = _db.get_series_by_path(conn, rel_series)
    if row:
        _db.scan_disk_files(series_dir, row["id"], conn)


def _is_under_excluded_series(rel_path: str, excluded: set[str]) -> bool:
    rel_norm = rel_path.replace("\\", "/").strip()
    for root in excluded:
        r = root.replace("\\", "/").strip()
        if rel_norm == r or rel_norm.startswith(r + "/"):
            return True
    return False


def _series_for_rel_path(rel_path: str, series_rows: list[dict]) -> dict | None:
    rel_norm = rel_path.replace("\\", "/").strip()
    best = None
    best_len = -1
    for row in series_rows:
        sp = (row.get("path") or "").replace("\\", "/").strip()
        if rel_norm == sp or rel_norm.startswith(sp + "/"):
            if len(sp) > best_len:
                best = row
                best_len = len(sp)
    return best


def _group_fix_entries(issues: list, dup_groups: list, series_rows: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    unknown_key = "__unknown__"

    def _group_bucket(rel_path: str):
        series = _series_for_rel_path(rel_path, series_rows)
        key = series["path"] if series else unknown_key
        if key not in grouped:
            grouped[key] = {
                "series_path": series["path"] if series else "",
                "series_title": series["name"] if series else "Unknown Series",
                "issues": [],
                "dup_groups": [],
            }
        return grouped[key]

    for fpath, issue_name, new_name in issues:
        rel_path = _rel_manga_path(fpath)
        bucket = _group_bucket(rel_path)
        bucket["issues"].append({
            "fpath": fpath,
            "display_name": os.path.basename(fpath),
            "issue_name": issue_name,
            "new_name": new_name,
        })

    for g in dup_groups:
        rel_path = _rel_manga_path(g["keep_path"])
        bucket = _group_bucket(rel_path)
        g_view = dict(g)
        g_view["keep_display"] = os.path.basename(g["keep_path"])
        g_view["delete_display"] = [os.path.basename(p) for p in g["delete_paths"]]
        bucket["dup_groups"].append(g_view)

    out = [v for v in grouped.values() if v["issues"] or v["dup_groups"]]
    out.sort(key=lambda x: x["series_title"].lower())
    return out


# ---------------------------------------------------------------------------
# Library helpers
# ---------------------------------------------------------------------------

def _root_folders() -> list[str]:
    return [rf for rf in (load_settings().get("root_folders") or []) if rf is not None]


def _human_size(size: int | None) -> str:
    if not size:
        return "-"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def get_all_series() -> list[dict]:
    conn = _get_conn()
    rows = _db.get_all_series(conn)
    roots = _root_folders()
    if not roots:
        return rows
    if "" in roots:
        # Empty string = MANGA_ROOT itself — no path restriction.
        return rows
    out: list[dict] = []
    for row in rows:
        p = (row.get("path") or "").replace("\\", "/").strip()
        if any(p == rf or p.startswith(rf + "/") for rf in roots):
            out.append(row)
    return out


def get_subdirs() -> list[str]:
    return _root_folders()


# ---------------------------------------------------------------------------
# MangaDex API helpers (thin wrappers around MangaDexSource)
# ---------------------------------------------------------------------------

def mdex_search(query: str):
    results = _mdx.search(query)
    # Reshape to legacy dict format expected by existing templates
    return [
        {"id": r["manga_id"], "title": r["title"], "cover_url": r["thumbnail_url"],
         "status": r["status"], "year": r["year"]}
        for r in results
    ]


async def _fetch_groups(manga_id: str, language: str) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _mdx.get_groups, manga_id, language)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def _no_rf() -> bool:
    return not _root_folders()


def _require_root_folders() -> None:
    if _no_rf():
        raise HTTPException(
            400,
            "Configure at least one root folder in Settings before running this action.",
        )


def _require_under_manga_root(path: str) -> None:
    """Raise 400 if resolved path escapes MANGA_ROOT (guards against .. traversal)."""
    target = os.path.realpath(path)
    if target != _MANGA_ROOT_REAL and not target.startswith(_MANGA_ROOT_REAL + os.sep):
        raise HTTPException(400, "Invalid path: must be under the manga root")


class DeleteSeriesFilesBody(BaseModel):
    chapter_ids: list[int] = Field(default_factory=list)
    volume_ids: list[int] = Field(default_factory=list)


class ComicInfoUpdateBody(BaseModel):
    fields: dict[str, str] = Field(default_factory=dict)


def _serialize_job(job: dict) -> dict:
    out = {
        "id": job.get("id"),
        "job_type": job.get("job_type"),
        "queue_key": job.get("queue_key"),
        "status": job.get("status"),
        "series_id": job.get("series_id"),
        "series_path_snapshot": job.get("series_path_snapshot"),
        "payload": job.get("payload") or {},
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "ended_at": job.get("ended_at"),
        "exit_code": job.get("exit_code"),
        "error_summary": job.get("error_summary"),
        "last_line_at": job.get("last_line_at"),
        "display_name": _job_display_name(job),
    }
    return out


def _realpath_under_series(series_rel: str, file_rel: str) -> str | None:
    """Return realpath of file if it lies inside the series directory; else None."""
    series_dir = os.path.realpath(os.path.join(MANGA_ROOT, series_rel))
    if not os.path.isdir(series_dir):
        return None
    try:
        target = os.path.realpath(os.path.join(MANGA_ROOT, file_rel))
    except OSError:
        return None
    if target == series_dir or target.startswith(series_dir + os.sep):
        return target
    return None


def _comicinfo_fields_to_xml(fields: dict[str, str]) -> str:
    allowed_order = [
        "Series", "Number", "Volume", "Title", "Summary",
        "Writer", "Penciller", "Translator", "Publisher", "Genre", "Count",
        "Web", "LanguageISO", "Year", "Manga", "AgeRating", "PageCount",
    ]
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xmlns:xsd="http://www.w3.org/2001/XMLSchema">',
    ]
    for key in allowed_order:
        val = str((fields or {}).get(key, "")).strip()
        if not val:
            continue
        esc = (
            val.replace("&", "&amp;")
               .replace("<", "&lt;")
               .replace(">", "&gt;")
               .replace('"', "&quot;")
        )
        lines.append(f"  <{key}>{esc}</{key}>")
    lines.append("</ComicInfo>")
    return "\n".join(lines)


def _validate_comicinfo_fields(fields: dict[str, str]) -> None:
    manga_val = str((fields or {}).get("Manga", "")).strip()
    if manga_val and manga_val not in MANGA_VALUES:
        allowed = ", ".join(MANGA_VALUES)
        raise HTTPException(422, f"Invalid Manga value '{manga_val}'. Allowed: {allowed}")
    age_val = str((fields or {}).get("AgeRating", "")).strip()
    if age_val and age_val not in AGE_RATING_VALUES:
        allowed = ", ".join(AGE_RATING_VALUES)
        raise HTTPException(422, f"Invalid AgeRating value '{age_val}'. Allowed: {allowed}")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    loop      = asyncio.get_event_loop()
    all_s     = await loop.run_in_executor(None, get_all_series)
    series         = [s for s in all_s if not s.get("ignored")]
    ignored_series = [s for s in all_s if s.get("ignored")]
    linked_count = sum(1 for s in series if s["linked"])
    settings     = load_settings()
    suwayomi_url = (settings.get("suwayomi_url") or "").strip().rstrip("/")
    raw_sources  = _get_suwayomi_sources() if suwayomi_url else []
    source_labels = {
        f"suwayomi:{s['id']}": s.get("displayName") or s.get("name") or f"suwayomi:{s['id']}"
        for s in raw_sources
    }
    return templates.TemplateResponse(request=request, name="index.html",
        context={"series": series, "ignored_series": ignored_series,
                 "active": "dashboard",
                 "no_root_folders": _no_rf(),
                 "linked_count": linked_count,
                 "unlinked_count": len(series) - linked_count,
                 "suwayomi_url": suwayomi_url,
                 "source_labels": source_labels})


@app.get("/series", response_class=HTMLResponse)
async def series_page(request: Request):
    return templates.TemplateResponse(request=request, name="series.html",
        context={"active": "series", "no_root_folders": _no_rf()})


@app.get("/series/{path:path}", response_class=HTMLResponse)
async def series_details_page(request: Request, path: str):
    conn = _get_conn()
    row = _db.get_series_by_path(conn, path)
    if not row:
        raise HTTPException(404, "Series not found")

    needs_meta = (
        not row.get("status")
        or not row.get("description")
        or not row.get("authors")
        or not row.get("artists")
        or not row.get("tags")
        or not row.get("year")
    )
    is_suwayomi = (row.get("source_name") or "").startswith("suwayomi:")
    # For Suwayomi series use the MDX companion UUID (if set) for metadata; skip otherwise
    mdx_lookup_id = row.get("mangadex_id") if is_suwayomi else row.get("source_id")
    if mdx_lookup_id:
        loop = asyncio.get_event_loop()
        try:
            if needs_meta:
                data = await loop.run_in_executor(
                    None, lambda: _mdx._api_get(f"/manga/{mdx_lookup_id}", {"includes[]": ["author", "artist", "cover_art"]}, timeout=15)
                )
                attr = data["data"]["attributes"]
                desc_map = attr.get("description") or {}
                description = desc_map.get("en") or next(iter(desc_map.values()), "") or None
                tags = [
                    t["attributes"]["name"]["en"]
                    for t in (attr.get("tags") or [])
                    if t.get("attributes", {}).get("name", {}).get("en")
                ]
                authors, artists = [], []
                for rel in data["data"].get("relationships", []):
                    rtype = rel.get("type")
                    rname = (rel.get("attributes") or {}).get("name", "")
                    if not rname:
                        continue
                    if rtype == "author":
                        authors.append(rname)
                    elif rtype == "artist":
                        artists.append(rname)
                total_vols = await loop.run_in_executor(
                    None, lambda: _resolve_total_volumes(attr, mdx_lookup_id)
                )
                _db.upsert_series_metadata(
                    conn, row["id"], row.get("source_name") or "mangadex",
                    description=description,
                    tags=tags,
                    authors=authors,
                    artists=artists,
                    year=attr.get("year"),
                    status=attr.get("status"),
                    content_rating=attr.get("contentRating"),
                    total_volumes=total_vols,
                )
                row = _db.get_series_by_path(conn, path) or row
            else:
                total_vols = await loop.run_in_executor(
                    None, lambda: _resolve_total_volumes(None, mdx_lookup_id)
                )
                stored = row.get("total_volumes")
                if stored is None or int(stored) != int(total_vols):
                    _db.upsert_series_metadata(
                        conn, row["id"], row.get("source_name") or "mangadex",
                        total_volumes=total_vols,
                    )
                    row = _db.get_series_by_path(conn, path) or row
            lang = (row.get("language") or "en").strip() or "en"
            agg = await loop.run_in_executor(
                None,
                lambda mid=mdx_lookup_id, lg=lang: _mdx._api_get(
                    f"/manga/{mid}/aggregate",
                    {"translatedLanguage[]": lg},
                    timeout=15,
                ),
            )
            _apply_volume_mapping(conn, row["id"], agg)
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()

    _db.scan_disk_files(os.path.join(MANGA_ROOT, row["path"]), row["id"], conn)

    all_chapters = _db.get_chapters(conn, row["id"])
    chapters = [c for c in all_chapters if c.get("path")]
    volumes = [
        v for v in _db.get_volumes(conn, row["id"])
        if v.get("path")
    ]
    chapters.sort(key=lambda x: x.get("chapter_num", 0))
    volumes.sort(key=lambda x: x.get("volume_num", 0))

    for c in chapters:
        c["display_path"] = os.path.basename(c["path"])
        c["display_size"] = _human_size(c.get("file_size"))
    for v in volumes:
        v["display_path"] = os.path.basename(v["path"])
        v["display_size"] = _human_size(v.get("file_size"))

    volume_ids_on_disk = {v["id"] for v in volumes}
    covered_chapter_nums: set[float] = set()
    for ch in all_chapters:
        ch_num = ch.get("chapter_num")
        if ch_num is None:
            continue
        if ch.get("path"):
            covered_chapter_nums.add(float(ch_num))
            continue
        vol_id = ch.get("volume_id")
        if vol_id and vol_id in volume_ids_on_disk:
            covered_chapter_nums.add(float(ch_num))

    covered_chapters = len(covered_chapter_nums)
    source_chapters = conn.execute(
        "SELECT COUNT(DISTINCT chapter_num) AS n FROM chapters WHERE series_id=?",
        (row["id"],),
    ).fetchone()["n"] or 0
    source_volumes = row.get("config", {}).get("total_volumes") or row.get("source_volume_count") or 0
    # Count volume coverage from both actual merged volume archives and chapter
    # files that are assigned to a MangaDex volume bucket.
    covered_volume_ids = {v["id"] for v in volumes}
    for ch in all_chapters:
        if not ch.get("path"):
            continue
        vol_id = ch.get("volume_id")
        if vol_id:
            covered_volume_ids.add(vol_id)
    have_volumes = len(covered_volume_ids)
    chapter_ok = source_chapters > 0 and covered_chapters >= source_chapters
    volume_ok = source_volumes > 0 and have_volumes >= source_volumes

    compact_volume_count = len(_db.get_volumes_needing_compact(conn, row["id"]))
    delete_tracked_file_count = len(chapters) + len(volumes)

    settings     = load_settings()
    suwayomi_url = (settings.get("suwayomi_url") or "").strip().rstrip("/")
    raw_sources  = _get_suwayomi_sources() if suwayomi_url else []
    source_labels = {
        f"suwayomi:{s['id']}": s.get("displayName") or s.get("name") or f"suwayomi:{s['id']}"
        for s in raw_sources
    }
    return templates.TemplateResponse(
        request=request,
        name="series_detail.html",
        context={
            "active": "dashboard",
            "series": row,
            "chapters": chapters,
            "volumes": volumes,
            "source_chapters": source_chapters,
            "source_volumes": source_volumes,
            "have_chapters": covered_chapters,
            "have_volumes": have_volumes,
            "chapter_ok": chapter_ok,
            "volume_ok": volume_ok,
            "compact_volume_count": compact_volume_count,
            "delete_tracked_file_count": delete_tracked_file_count,
            "comicinfo_manga_values": MANGA_VALUES,
            "comicinfo_age_rating_values": AGE_RATING_VALUES,
            "no_root_folders": _no_rf(),
            "suwayomi_url": suwayomi_url,
            "source_labels": source_labels,
        },
    )


@app.get("/sync", response_class=HTMLResponse)
async def sync_page(request: Request):
    return RedirectResponse("/jobs", status_code=307)


@app.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="jobs.html",
        context={"active": "jobs", "no_root_folders": _no_rf()},
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    settings  = load_settings()
    configured = settings.get("root_folders") or []
    try:
        existing = sorted(
            d for d in os.listdir(MANGA_ROOT)
            if os.path.isdir(os.path.join(MANGA_ROOT, d)) and not d.startswith(".")
        )
    except OSError:
        existing = []
    available_dirs = [d for d in existing if d not in configured]
    return templates.TemplateResponse(request=request, name="settings.html",
        context={"settings": settings, "active": "settings",
                 "available_dirs": available_dirs, "manga_root": MANGA_ROOT,
                 "no_root_folders": not configured})


@app.post("/api/settings")
async def save_settings_endpoint(
    root_folders_json: str = Form("[]"),
    file_format:       str = Form("cbz"),
    chapter_naming:    str = Form("%3 ch.%5"),
    volume_naming:     str = Form("%3 vol.%4"),
    download_delay:  float = Form(1.0),
    sync_cron:        str = Form("0 */6 * * *"),
    merge_volumes:     str = Form("true"),
    auto_scan:         str = Form("false"),
    auto_covers:       str = Form("false"),
    kavita_url:        str = Form(""),
    kavita_api_key:    str = Form(""),
    file_permission_mask: str = Form("664"),
    webhook_url:       str = Form(""),
    webhook_platform:  str = Form("generic"),
    suwayomi_url:       str = Form(""),
    suwayomi_username:  str = Form(""),
    suwayomi_password:  str = Form(""),
    telemetry_enabled:  str = Form("true"),
):
    try:
        root_folders = json.loads(root_folders_json)
        if not isinstance(root_folders, list):
            root_folders = []
    except Exception:
        root_folders = []
    seen: list[str] = []
    for rf in root_folders:
        clean = rf.strip("/").strip() if isinstance(rf, str) else ""
        if clean not in seen:
            seen.append(clean)
    before = load_settings()
    normalized_sync_cron = sanitize_sync_cron(sync_cron)
    platform = webhook_platform if webhook_platform in ("discord", "ntfy", "generic") else "generic"
    # Preserve existing suwayomi_password if the field was left blank
    stored_pw = before.get("suwayomi_password", "")
    effective_pw = suwayomi_password.strip() if suwayomi_password.strip() else stored_pw
    save_settings({
        "root_folders":      seen,
        "file_format":       file_format,
        "chapter_naming":    chapter_naming,
        "volume_naming":     sanitize_volume_naming(volume_naming.strip() or "[%1 %2] %3 vol.%4"),
        "download_delay":    download_delay,
        "sync_cron":         normalized_sync_cron,
        "merge_volumes":     merge_volumes == "true",
        "auto_scan":         auto_scan == "true",
        "auto_covers":       auto_covers == "true",
        "kavita_url":        kavita_url.strip(),
        "kavita_api_key":    kavita_api_key.strip(),
        "file_permission_mask": sanitize_file_permission_mask(file_permission_mask),
        "webhook_url":       webhook_url.strip(),
        "webhook_platform":  platform,
        "suwayomi_url":       suwayomi_url.strip(),
        "suwayomi_username":  suwayomi_username.strip(),
        "suwayomi_password":  effective_pw,
        "suwayomi_auth_mode": "",  # force re-probe after credentials change
        "telemetry_enabled":  telemetry_enabled == "true",
    })
    # Bust Suwayomi caches so new URL/credentials take effect immediately
    _suwayomi_cache["ts"] = 0.0
    _suwayomi_client_cache["key"] = None
    if sanitize_sync_cron(before.get("sync_cron")) != normalized_sync_cron:
        try:
            _write_runtime_crontab(normalized_sync_cron)
        except Exception as e:
            print(f"[settings] warning: could not write runtime crontab '{RUNTIME_CRONTAB_PATH}': {e}")
    if list(before.get("root_folders") or []) != seen:
        with contextlib.suppress(Exception):
            _enqueue_reconcile_job("settings_root_folders_changed")
    return RedirectResponse("/settings", status_code=303)


@app.post("/api/settings/test-webhook")
async def test_webhook(request: Request):
    body = await request.json()
    url      = (body.get("webhook_url") or "").strip()
    platform = body.get("webhook_platform") or "generic"
    title    = body.get("title") or "My Manga Title"
    count    = int(body.get("count") or 1)
    if not url:
        return JSONResponse({"ok": False, "error": "No webhook URL configured"})
    ch = "chapter" if count == 1 else "chapters"
    text = f"New chapters downloaded:\n• {title} - {count} new {ch}"
    try:
        from urllib import request as _urllib_request
        import json as _json
        if platform == "ntfy":
            body_bytes = text.encode()
            req = _urllib_request.Request(url, data=body_bytes, method="POST")
            req.add_header("Content-Type", "text/plain")
        else:
            body_bytes = _json.dumps({"content": text}).encode()
            req = _urllib_request.Request(url, data=body_bytes, method="POST")
            req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "manga-sync/1.0")
        with _urllib_request.urlopen(req, timeout=10):
            pass
        return JSONResponse({"ok": True, "error": None})
    except Exception as e:
        from urllib.error import HTTPError
        if isinstance(e, HTTPError):
            body = e.read(512).decode(errors="replace").strip()
            msg = f"HTTP {e.code}: {body or e.reason}"
        else:
            msg = str(e)
        return JSONResponse({"ok": False, "error": msg})


@app.get("/api/settings/webhook-preview")
async def webhook_preview():
    _FALLBACKS = [
        "My Manga Title", "Yotsuba&!", "Chainsaw Man", "Spy×Family",
    ]
    conn = _get_conn()
    row = conn.execute(
        "SELECT title FROM series WHERE title IS NOT NULL ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    title = row["title"] if row else _FALLBACKS[0]
    import random as _random
    count = _random.randint(1, 3)
    return JSONResponse({"title": title, "count": count})


@app.post("/api/settings/test-kavita")
async def test_kavita(request: Request):
    body = await request.json()
    url  = body.get("kavita_url", "").strip()
    key  = body.get("kavita_api_key", "").strip()
    if not url or not key:
        return JSONResponse({"ok": False, "error": "URL and API key are required"})
    loop = asyncio.get_event_loop()
    try:
        client = KavitaClient(url, key)
        await loop.run_in_executor(None, client._authenticate)
        return JSONResponse({"ok": True, "error": None})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/settings/test-suwayomi")
async def test_suwayomi(request: Request):
    body = await request.json()
    url  = (body.get("suwayomi_url") or "").strip()
    user = body.get("suwayomi_username", "")
    pw   = body.get("suwayomi_password", "")
    if not url:
        return JSONResponse({"ok": False, "error": "No Suwayomi URL configured"})
    from sources.suwayomi import SuwayomiClient
    client = SuwayomiClient(url, user, pw)
    loop = asyncio.get_event_loop()
    try:
        sources = await loop.run_in_executor(None, client.list_sources)
        return JSONResponse({"ok": True, "source_count": len(sources), "error": None})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request):
    settings      = load_settings()
    suwayomi_url  = (settings.get("suwayomi_url") or "").strip()
    enabled       = _enabled_source_keys()
    sources: list[dict] = []
    if suwayomi_url:
        loop = asyncio.get_event_loop()
        raw  = await loop.run_in_executor(None, _get_suwayomi_sources)
        for s in raw:
            key = f"suwayomi:{s['id']}"
            sources.append({
                "key":      key,
                "id":       str(s["id"]),
                "name":     s.get("displayName") or s.get("name", ""),
                "lang":     (s.get("lang") or "").upper(),
                "icon_url": s.get("iconUrl"),
                "enabled":  key in enabled,
            })
        sources.sort(key=lambda x: (not x["enabled"], x["name"].lower()))
    return templates.TemplateResponse(request=request, name="sources.html",
        context={"active": "sources", "no_root_folders": _no_rf(),
                 "suwayomi_url": suwayomi_url, "sources": sources})


@app.get("/api/sources")
async def api_sources():
    enabled = _enabled_source_keys()
    loop    = asyncio.get_event_loop()
    raw     = await loop.run_in_executor(None, _get_suwayomi_sources)
    out = [{"key": "mangadex", "name": "MangaDex", "builtin": True, "enabled": True, "lang": "MUL"}]
    for s in raw:
        key = f"suwayomi:{s['id']}"
        out.append({"key": key, "name": s.get("displayName") or s.get("name", ""),
                    "lang": (s.get("lang") or "").upper(),
                    "builtin": False, "enabled": key in enabled})
    return JSONResponse(out)


@app.post("/api/sources/{source_id}/enable")
async def enable_source(source_id: str):
    key = f"suwayomi:{source_id}"
    s = load_settings()
    enabled = list(set(s.get("enabled_sources") or []) | {key})
    save_settings({"enabled_sources": enabled})
    return JSONResponse({"ok": True, "key": key})


@app.post("/api/sources/{source_id}/disable")
async def disable_source(source_id: str):
    key = f"suwayomi:{source_id}"
    s = load_settings()
    enabled = [k for k in (s.get("enabled_sources") or []) if k != key]
    save_settings({"enabled_sources": enabled})
    return JSONResponse({"ok": True, "key": key})


@app.get("/fix", response_class=HTMLResponse)
async def fix_page(request: Request, series: str = ""):
    conn = _get_conn()
    series_rows = _db.get_all_series(conn)
    excluded = _exclude_fix_series_paths(conn)
    issues = [
        t for t in _fix.scan(MANGA_ROOT)
        if not _is_under_excluded_series(_rel_manga_path(t[0]), excluded)
    ]
    loop = asyncio.get_event_loop()
    issues.extend(await loop.run_in_executor(None, _scan_settings_naming_issues))
    dup_groups = [
        g for g in _fix.scan_duplicates(MANGA_ROOT)
        if not _is_under_excluded_series(_rel_manga_path(g["keep_path"]), excluded)
    ]
    series_groups = _group_fix_entries(issues, dup_groups, series_rows)
    if series:
        series_groups = [g for g in series_groups if g.get("series_path") == series]
    total = sum(len(g["issues"]) + len(g["dup_groups"]) for g in series_groups)
    return templates.TemplateResponse(request=request, name="fix.html",
        context={"series_groups": series_groups, "series_filter": series,
                 "active": "fix", "total": total,
                 "no_root_folders": _no_rf()})


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    sync_lines = []
    if os.path.exists(SYNC_LOG):
        with open(SYNC_LOG) as f:
            sync_lines = list(reversed(f.readlines()[-200:]))

    conn = _get_conn()
    renames = conn.execute("""
        SELECT old_path, new_path, action, reason, timestamp
        FROM rename_log ORDER BY id DESC LIMIT 50
    """).fetchall()
    deletes = conn.execute("""
        SELECT old_path, reason, timestamp
        FROM rename_log WHERE action='delete' ORDER BY id DESC LIMIT 20
    """).fetchall()

    return templates.TemplateResponse(request=request, name="logs.html",
        context={"sync_lines": sync_lines,
                 "renames": [dict(r) for r in renames],
                 "deletes": [dict(r) for r in deletes],
                 "active": "logs", "no_root_folders": _no_rf()})


# ---------------------------------------------------------------------------
# API - search & manga info
# ---------------------------------------------------------------------------

def _friendly_search_error(exc: Exception) -> str:
    """Convert raw source search exceptions into a readable one-liner."""
    msg = str(exc)
    # Suwayomi GQL errors look like: "Suwayomi GQL error: [{'message': '...'}]"
    import re as _re
    m = _re.search(r"GQL error.*?'message':\s*'([^']+)'", msg)
    if m:
        return m.group(1)
    # Strip leading "Suwayomi GQL error: " prefix if present
    msg = _re.sub(r"^Suwayomi GQL error:\s*", "", msg).strip()
    # Truncate very long messages
    return msg[:120] + ("…" if len(msg) > 120 else "")


@app.get("/api/search", response_class=HTMLResponse)
async def search(request: Request, q: str = ""):
    if len(q) < 2:
        return HTMLResponse("")
    loop   = asyncio.get_event_loop()
    groups: list[dict] = []

    async def _search_mdx():
        try:
            res = await loop.run_in_executor(None, lambda: _mdx.search(q))
            return {"source_key": "mangadex", "source_name": "MangaDex", "error": None,
                    "results": [{"id": r["manga_id"], "title": r["title"],
                                 "cover_url": r["thumbnail_url"], "status": r["status"],
                                 "year": r["year"], "source_key": "mangadex"} for r in res]}
        except Exception as exc:
            return {"source_key": "mangadex", "source_name": "MangaDex",
                    "error": _friendly_search_error(exc), "results": []}

    async def _search_suwayomi(src: dict, client):
        key = f"suwayomi:{src['id']}"
        try:
            mangas, _ = await loop.run_in_executor(
                None, lambda: client.fetch_source_manga(str(src["id"]), q)
            )
            return {"source_key": key,
                    "source_name": src.get("displayName") or src.get("name", key),
                    "error": None,
                    "results": [{"id": str(m["id"]), "title": m.get("title", ""),
                                 "cover_url": f"/api/proxy/suwayomi/thumbnail/{m['id']}",
                                 "status": None, "year": None, "source_key": key}
                                for m in mangas]}
        except Exception as exc:
            return {"source_key": key,
                    "source_name": src.get("displayName") or src.get("name", key),
                    "error": _friendly_search_error(exc), "results": []}

    tasks = [_search_mdx()]
    enabled = _enabled_source_keys()
    if enabled:
        client = get_suwayomi_client()
        if client:
            raw_sources = await loop.run_in_executor(None, _get_suwayomi_sources)
            for src in raw_sources:
                if f"suwayomi:{src['id']}" in enabled:
                    tasks.append(_search_suwayomi(src, client))

    results_list = await asyncio.gather(*tasks)
    groups = [g for g in results_list if g["results"] or g["error"]]

    return templates.TemplateResponse(request=request, name="partials/search_results.html",
        context={"groups": groups, "multi_source": len(groups) > 1})


@app.get("/api/search/mdx-companion", response_class=HTMLResponse)
async def search_mdx_companion(request: Request, q: str = ""):
    """MangaDex-only search for the companion-link picker in the Suwayomi setup form."""
    if len(q) < 2:
        return HTMLResponse("")
    loop = asyncio.get_event_loop()
    try:
        results = await loop.run_in_executor(None, lambda: _mdx.search(q))
    except Exception as e:
        return HTMLResponse(f'<p class="text-xs px-2 py-1" style="color:var(--danger)">Search failed: {e}</p>')
    return templates.TemplateResponse(request=request, name="partials/mdx_companion_results.html",
        context={"results": results})


@app.get("/api/manga/{manga_id}/setup", response_class=HTMLResponse)
async def get_manga_setup(request: Request, manga_id: str):
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(
            None, lambda: _mdx._api_get(f"/manga/{manga_id}", {"includes[]": "cover_art"}, timeout=15)
        )
    except Exception as e:
        return HTMLResponse(f'<p class="text-red-500 text-sm">Could not load manga: {e}</p>')

    manga_data = data["data"]
    attr   = manga_data["attributes"]
    title  = (attr.get("title") or {}).get("en") or \
             next(iter((attr.get("title") or {}).values()), "Unknown")
    status = attr.get("status", "unknown")
    year   = attr.get("year")
    available_langs = attr.get("availableTranslatedLanguages") or []

    cover_url = cover_filename = None
    for rel in manga_data.get("relationships", []):
        if rel["type"] == "cover_art":
            fname = (rel.get("attributes") or {}).get("fileName")
            if fname:
                cover_filename = fname
                cover_url      = _cover_url(manga_id, fname)

    lang_counts: dict[str, int] = {}
    if available_langs:
        tasks   = [loop.run_in_executor(None, _mdx.get_lang_chapter_count, manga_id, lang)
                   for lang in available_langs[:10]]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, tuple) and r[1] > 0:
                lang_counts[r[0]] = r[1]
    lang_counts  = dict(sorted(lang_counts.items(), key=lambda x: x[1], reverse=True))
    default_lang = next(iter(lang_counts), "en")
    groups_data  = await _fetch_groups(manga_id, default_lang)

    existing = _get_conn().execute(
        "SELECT s.path FROM series s JOIN series_sources ss ON s.id = ss.series_id "
        "WHERE ss.source = 'mangadex' AND ss.source_id = ?", (manga_id,)
    ).fetchone()
    return templates.TemplateResponse(request=request, name="partials/manga_setup.html",
        context={"manga_id": manga_id, "title": title, "status": status, "year": year,
                 "cover_url": cover_url, "cover_filename": cover_filename or "",
                 "lang_counts": lang_counts, "default_lang": default_lang,
                 "subdirs": get_subdirs(), "existing_path": existing["path"] if existing else None,
                 **groups_data})


@app.get("/api/manga/{manga_id}/link-preview", response_class=HTMLResponse)
async def get_manga_link_preview(request: Request, manga_id: str):
    """Fast step-1 panel: link manga ID only (no feed/group scans)."""
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(
            None, lambda: _mdx._api_get(f"/manga/{manga_id}", {"includes[]": "cover_art"}, timeout=15)
        )
    except Exception as e:
        return HTMLResponse(f'<p class="text-red-500 text-sm">Could not load manga: {e}</p>')

    manga_data = data["data"]
    attr   = manga_data["attributes"]
    title  = (attr.get("title") or {}).get("en") or \
             next(iter((attr.get("title") or {}).values()), "Unknown")
    status = attr.get("status", "unknown")
    year   = attr.get("year")
    available_langs = attr.get("availableTranslatedLanguages") or []

    cover_url = None
    cover_filename = ""
    for rel in manga_data.get("relationships", []):
        if rel["type"] == "cover_art":
            fname = (rel.get("attributes") or {}).get("fileName")
            if fname:
                cover_filename = fname
                cover_url      = _cover_url(manga_id, fname)
                break

    default_lang = _pick_default_series_language(available_langs)

    existing = _get_conn().execute(
        "SELECT s.path FROM series s JOIN series_sources ss ON s.id = ss.series_id "
        "WHERE ss.source = 'mangadex' AND ss.source_id = ?", (manga_id,)
    ).fetchone()
    return templates.TemplateResponse(request=request, name="partials/manga_link_confirm.html",
        context={
            "manga_id": manga_id,
            "title": title,
            "status": status,
            "year": year,
            "cover_url": cover_url,
            "cover_filename": cover_filename,
            "available_langs": available_langs,
            "default_lang": default_lang,
            "series_language_choices": _series_language_choices(default_lang, available_langs),
            "subdirs": get_subdirs(),
            "existing_path": existing["path"] if existing else None,
        })


@app.get("/api/manga/{manga_id}/langs")
async def get_manga_langs(manga_id: str):
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(
            None, lambda: _mdx._api_get(f"/manga/{manga_id}", {}, timeout=15)
        )
    except Exception as e:
        raise HTTPException(502, str(e))
    available_langs = data["data"]["attributes"].get("availableTranslatedLanguages") or []
    tasks   = [loop.run_in_executor(None, _mdx.get_lang_chapter_count, manga_id, lang)
               for lang in available_langs[:10]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    counts  = sorted(
        [(lang, cnt) for r in results if isinstance(r, tuple) for lang, cnt in [r] if cnt > 0],
        key=lambda x: x[1], reverse=True,
    )
    return JSONResponse(counts)


@app.get("/api/manga/{manga_id}/groups", response_class=HTMLResponse)
async def get_manga_groups(request: Request, manga_id: str, language: str = "en"):
    groups_data = await _fetch_groups(manga_id, language)
    return templates.TemplateResponse(request=request, name="partials/group_picker.html",
        context=groups_data)


# ---------------------------------------------------------------------------
# API - cover proxy
# ---------------------------------------------------------------------------

@app.get("/api/suwayomi/{source_id}/{manga_id}/setup", response_class=HTMLResponse)
async def suwayomi_manga_setup(request: Request, source_id: str, manga_id: str):
    client = get_suwayomi_client()
    if not client:
        return HTMLResponse('<p class="text-red-500 text-sm">Suwayomi not configured.</p>')
    loop = asyncio.get_event_loop()
    try:
        manga = await loop.run_in_executor(None, lambda: client.fetch_manga(int(manga_id)))
    except Exception as e:
        return HTMLResponse(f'<p class="text-red-500 text-sm">Could not load manga: {e}</p>')

    raw_sources = await loop.run_in_executor(None, _get_suwayomi_sources)
    src_info    = next((s for s in raw_sources if str(s["id"]) == source_id), {})
    source_name = src_info.get("displayName") or src_info.get("name") or f"Suwayomi:{source_id}"
    lang        = (src_info.get("lang") or "unknown").lower()
    source_key  = f"suwayomi:{source_id}"
    cover_url   = f"/api/proxy/suwayomi/thumbnail/{manga_id}"

    existing = _get_conn().execute(
        "SELECT s.path FROM series s JOIN series_sources ss ON s.id = ss.series_id "
        "WHERE ss.source = ? AND ss.source_id = ?", (source_key, manga_id)
    ).fetchone()
    return templates.TemplateResponse(request=request, name="partials/suwayomi_manga_setup.html",
        context={"manga_id": manga_id, "source_key": source_key, "source_name": source_name,
                 "lang": lang, "cover_url": cover_url, "title": manga.get("title", ""),
                 "subdirs": get_subdirs(), "existing_path": existing["path"] if existing else None})


@app.get("/api/proxy/suwayomi/icon/{source_id}")
async def proxy_suwayomi_icon(source_id: str):
    cache_key = f"suw-icon:{source_id}"
    if cache_key not in _cover_cache:
        raw_sources = _get_suwayomi_sources()
        src = next((s for s in raw_sources if str(s["id"]) == source_id), {})
        icon_url = src.get("iconUrl") or ""
        if not icon_url:
            raise HTTPException(404)
        client = get_suwayomi_client()
        if not client:
            raise HTTPException(503)
        loop = asyncio.get_event_loop()
        try:
            # iconUrl is a relative path on Suwayomi
            path = icon_url if icon_url.startswith("/") else f"/{icon_url}"
            data = await loop.run_in_executor(None, lambda: client.download_page(path))
        except Exception:
            raise HTTPException(404)
        if len(_cover_cache) >= _COVER_CACHE_MAX:
            _cover_cache.pop(next(iter(_cover_cache)))
        _cover_cache[cache_key] = data
    return Response(_cover_cache[cache_key], media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/proxy/suwayomi/thumbnail/{manga_id}")
async def proxy_suwayomi_thumbnail(manga_id: str):
    cache_key = f"suw:{manga_id}"
    if cache_key not in _cover_cache:
        client = get_suwayomi_client()
        if not client:
            raise HTTPException(503, "Suwayomi not configured")
        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(
                None, lambda: client.download_page(f"/api/v1/manga/{manga_id}/thumbnail")
            )
        except Exception:
            raise HTTPException(404)
        if len(_cover_cache) >= _COVER_CACHE_MAX:
            _cover_cache.pop(next(iter(_cover_cache)))
        _cover_cache[cache_key] = data
    return Response(_cover_cache[cache_key], media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/proxy/cover/{manga_id}/{filename}")
async def proxy_cover(manga_id: str, filename: str):
    cache_key = f"{manga_id}/{filename}"
    if cache_key not in _cover_cache:
        url = f"{MDEX_COVERS}/{manga_id}/{filename}.256.jpg"
        req = urlrequest.Request(url, headers={
            "Referer":    "https://mangadex.org/",
            "User-Agent": "Mozilla/5.0",
        })
        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(
                None, lambda: urlrequest.urlopen(req, timeout=15).read()
            )
        except Exception:
            raise HTTPException(404)
        if len(_cover_cache) >= _COVER_CACHE_MAX:
            _cover_cache.pop(next(iter(_cover_cache)))
        _cover_cache[cache_key] = data
    return Response(_cover_cache[cache_key], media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------------------
# API - series CRUD
# ---------------------------------------------------------------------------

def _parse_merge_volumes_override(raw: str | None) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "":
        return None
    if s in ("0", "false"):
        return 0
    if s in ("1", "true"):
        return 1
    return None


@app.post("/api/series")
async def add_series(
    request: Request,
    manga_id:       str   = Form(...),
    title:          str   = Form(...),
    subfolder:      str   = Form(""),
    language:       str   = Form("en"),
    preferred_groups_json: str = Form("[]"),
    start_chapter:  str   = Form("0"),
    cover_filename: str   = Form(""),
    exclude_from_fix: str = Form("false"),
    merge_volumes_override: str = Form(""),
    source_key:     str   = Form("mangadex"),
    mangadex_id:    str   = Form(""),
):
    _require_root_folders()
    parts      = [p for p in [subfolder.strip("/"), title] if p]
    series_dir = os.path.join(MANGA_ROOT, *parts)
    _require_under_manga_root(series_dir)
    rel_path   = os.path.relpath(series_dir, MANGA_ROOT)

    actual_source = source_key if source_key.startswith("suwayomi:") else "mangadex"
    mdx_id = mangadex_id.strip() or (manga_id if actual_source == "mangadex" else None)

    if manga_id:
        conn = _get_conn()
        dup = conn.execute(
            "SELECT s.path FROM series s JOIN series_sources ss ON s.id = ss.series_id "
            "WHERE ss.source = ? AND ss.source_id = ?", (actual_source, manga_id)
        ).fetchone()
        if dup:
            raise HTTPException(
                409,
                f"Already tracked at '{dup['path']}' - open it from your library instead."
            )
        ex = conn.execute(
            "SELECT ss.source, ss.source_id FROM series s "
            "LEFT JOIN series_sources ss ON s.id = ss.series_id "
            "WHERE s.path = ? AND ss.source_id IS NOT NULL",
            (rel_path,)
        ).fetchone()
        if ex and not (ex["source"] == actual_source and ex["source_id"] == manga_id):
            raise HTTPException(
                409,
                f"Folder '{rel_path}' already belongs to a different series - choose a different folder name."
            )

    os.makedirs(series_dir, exist_ok=True)

    try:
        start_f = float(start_chapter) if start_chapter else 0.0
    except ValueError:
        start_f = 0.0

    excl = 1 if exclude_from_fix == "true" else 0
    merge_ov = _parse_merge_volumes_override(merge_volumes_override)

    accept = request.headers.get("accept") or ""
    link_only = "application/json" in accept
    sync_conf = 0 if link_only else 1

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: _db.insert_series(
        _get_conn(),
        path=rel_path,
        title=title,
        language=language,
        start_chapter=start_f,
        source=actual_source if manga_id else None,
        source_id=manga_id or None,
        cover_filename=cover_filename.strip() or None,
        exclude_from_fix=excl,
        merge_volumes_override=merge_ov,
        preferred_groups_json=preferred_groups_json.strip() or None,
        sync_configured=sync_conf,
        mangadex_id=mdx_id or None,
    ))
    _db.increment_usage(conn, "source_links")
    if link_only:
        return JSONResponse({"ok": True, "path": rel_path}, status_code=201)
    target = f"/series/{parse.quote(rel_path, safe='/')}"
    return RedirectResponse(target, status_code=303)


@app.get("/api/series/{path:path}/cover")
async def series_cover(path: str):
    conn = _get_conn()
    row  = conn.execute("""
        SELECT ss.source_id, sm.cover_filename
        FROM series s
        JOIN series_sources ss ON s.id = ss.series_id
        LEFT JOIN series_metadata sm ON s.id = sm.series_id AND sm.source = ss.source
        WHERE s.path = ?
        ORDER BY ss.priority LIMIT 1
    """, (path,)).fetchone()
    if not row or not row["source_id"]:
        raise HTTPException(404)

    if row["cover_filename"]:
        return JSONResponse({"url": _cover_url(row["source_id"], row["cover_filename"])})

    manga_id = row["source_id"]
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(
            None, lambda: _mdx._api_get(f"/manga/{manga_id}", {"includes[]": "cover_art"}, timeout=15)
        )
    except Exception as e:
        raise HTTPException(502, str(e))

    cover_filename = None
    for rel in data["data"].get("relationships", []):
        if rel["type"] == "cover_art":
            fname = (rel.get("attributes") or {}).get("fileName")
            if fname:
                cover_filename = fname
                break
    if not cover_filename:
        raise HTTPException(404)

    series_row = _db.get_series_by_path(conn, path)
    if series_row:
        _db.upsert_series_metadata(conn, series_row["id"], "mangadex",
                                   cover_filename=cover_filename)
    return JSONResponse({"url": _cover_url(manga_id, cover_filename)})


@app.get("/api/series/{path:path}/chapter-gaps")
async def series_chapter_gaps(path: str):
    conn = _get_conn()
    row = _db.get_series_by_path(conn, path)
    if not row:
        raise HTTPException(404, "Series not found")

    source = _db.get_primary_source(conn, row["id"])
    if not source or source.get("source") != "mangadex":
        return JSONResponse({"mode": "no_source"})

    manga_id = source["source_id"]
    language = (row.get("language") or "en").strip()

    all_chapters = _db.get_chapters(conn, row["id"])
    volume_ids_on_disk = {
        v["id"] for v in _db.get_volumes(conn, row["id"]) if v.get("path")
    }

    if not all_chapters and volume_ids_on_disk:
        return JSONResponse({"mode": "no_tracking"})

    covered: set[float] = set()
    db_nums: set[float] = set()
    for ch in all_chapters:
        raw = ch.get("chapter_num")
        if raw is None:
            continue
        num = float(raw)
        db_nums.add(num)
        if ch.get("path"):
            covered.add(num)
        elif ch.get("volume_id") in volume_ids_on_disk:
            covered.add(num)

    loop = asyncio.get_event_loop()
    try:
        agg = await loop.run_in_executor(
            None,
            lambda mid=manga_id, lg=language: _mdx._api_get(
                f"/manga/{mid}/aggregate",
                {"translatedLanguage[]": lg},
                timeout=15,
            ),
        )
    except Exception as e:
        return JSONResponse({"mode": "error", "error": str(e)})

    mdex_chapters: dict[float, str] = {}
    for vol_key, vol_data in (agg.get("volumes") or {}).items():
        for ch_key in (vol_data.get("chapters") or {}):
            try:
                ch_num = float(ch_key)
            except (ValueError, TypeError):
                continue
            mdex_chapters[ch_num] = vol_key

    chips = []
    for ch_num in sorted(mdex_chapters):
        if ch_num in covered:
            status = "ok"
        else:
            status = "gap"
        chips.append({"num": ch_num, "vol": mdex_chapters[ch_num], "status": status})

    n_gaps = sum(1 for c in chips if c["status"] != "ok")
    return JSONResponse({
        "mode": "ok",
        "chips": chips,
        "total": len(chips),
        "covered": len(chips) - n_gaps,
        "gaps": n_gaps,
    })


class SyncPausedBody(BaseModel):
    paused: bool


@app.post("/api/series/{path:path}/sync-pause", response_class=JSONResponse)
async def set_series_sync_pause(path: str, body: SyncPausedBody):
    conn = _get_conn()
    if not _db.get_series_by_path(conn, path):
        raise HTTPException(404, "Series not found")
    _db.set_sync_paused(conn, path, body.paused)
    return JSONResponse({"ok": True, "sync_paused": body.paused})


@app.post("/api/series/{path:path}/ignore")
async def ignore_series(path: str):
    conn = _get_conn()
    if not _db.get_series_by_path(conn, path):
        raise HTTPException(404, "Series not found")
    loop = asyncio.get_event_loop()
    _p = path
    await loop.run_in_executor(None, lambda: _db.set_series_ignored(_get_conn(), _p, True))
    _db.increment_usage(conn, "series_ignores")
    return JSONResponse({"ok": True})


@app.delete("/api/series/{path:path}/ignore")
async def unignore_series(path: str):
    conn = _get_conn()
    if not _db.get_series_by_path(conn, path):
        raise HTTPException(404, "Series not found")
    loop = asyncio.get_event_loop()
    _p = path
    await loop.run_in_executor(None, lambda: _db.set_series_ignored(_get_conn(), _p, False))
    return JSONResponse({"ok": True})


@app.post("/api/series/{path:path}/unlink", response_class=HTMLResponse)
async def unlink_series_from_source(path: str):
    _require_root_folders()
    conn = _get_conn()
    if not _db.get_series_by_path(conn, path):
        raise HTTPException(404, "Series not found")
    loop = asyncio.get_event_loop()
    _p = path
    await loop.run_in_executor(None, lambda: _db.unlink_series(_get_conn(), _p))
    _db.increment_usage(conn, "source_unlinks")
    return HTMLResponse("")


@app.delete("/api/series/{path:path}/with-folder", response_class=JSONResponse)
async def delete_series_with_folder(path: str):
    _require_root_folders()
    conn = _get_conn()
    if not _db.get_series_by_path(conn, path):
        raise HTTPException(404, "Series not found")
    series_dir = os.path.join(MANGA_ROOT, path)
    _require_under_manga_root(series_dir)
    loop = asyncio.get_event_loop()
    _p = path
    await loop.run_in_executor(None, lambda: _db.delete_series(_get_conn(), _p))
    if os.path.isdir(series_dir):
        await loop.run_in_executor(None, lambda: shutil.rmtree(series_dir))
    return JSONResponse({"ok": True})


class MdxCompanionBody(BaseModel):
    mangadex_id: str = ""


@app.put("/api/series/{path:path}/mdx-companion")
async def set_mdx_companion(path: str, body: MdxCompanionBody):
    conn = _get_conn()
    row = _db.get_series_by_path(conn, path)
    if not row:
        raise HTTPException(404, "Series not found")
    mdx_id = body.mangadex_id.strip() or None
    conn.execute(
        "UPDATE series SET mangadex_id = ?, updated_at = ? WHERE path = ?",
        (mdx_id, _db._now(), path)
    )
    conn.commit()
    return JSONResponse({"ok": True, "mangadex_id": mdx_id})


@app.get("/api/series/{path:path}/mdx-companion-search", response_class=HTMLResponse)
async def mdx_companion_search_form(request: Request, path: str):
    conn = _get_conn()
    row = _db.get_series_by_path(conn, path)
    if not row:
        raise HTTPException(404, "Series not found")
    return templates.TemplateResponse(request=request, name="partials/mdx_companion_link.html",
        context={"path": path, "series_name": row.get("title") or path.split("/")[-1]})


@app.get("/api/series/{path:path}/edit", response_class=HTMLResponse)
async def edit_series_form(request: Request, path: str):
    """General settings: location, folder name, series language, exclude-from-fix.

    Sync-only settings (groups, chapter cutoff) use ``grab-options``.
    """
    conn = _get_conn()
    row  = _db.get_series_by_path(conn, path)
    if not row:
        raise HTTPException(404, "Series not found")

    parts      = path.split("/")
    folder_name = parts[-1]
    subfolder   = "/".join(parts[:-1])

    prefs = row.get("preferred_groups") or []
    source_name = row.get("source_name") or "mangadex"
    is_mdx = source_name == "mangadex"
    # For non-MDX series, pass empty manga_id so update_series doesn't clobber the Suwayomi source
    manga_id = (row["config"].get("id", "") or "") if is_mdx else ""
    lang_norm = (row.get("language") or "en").strip().lower()

    return templates.TemplateResponse(request=request, name="partials/series_edit_general.html",
        context={
            "path":                path,
            "manga_id":            manga_id,
            "manga_title":         folder_name,
            "current_lang":        lang_norm,
            "series_language_choices": _series_language_choices(row.get("language")),
            "current_preferred_groups_json": json.dumps(prefs, ensure_ascii=False),
            "current_start":       row.get("start_chapter", 0),
            "current_cover_filename": (row.get("config") or {}).get("cover_filename", ""),
            "exclude_from_fix":    bool(row.get("exclude_from_fix")),
            "folder_name":         folder_name,
            "subfolder":           subfolder,
            "subdirs":             get_subdirs(),
        })


@app.get("/api/series/{path:path}/grab-options", response_class=HTMLResponse)
async def get_grab_options_form(request: Request, path: str):
    """Optional step 2 after linking: language, groups, start chapter (PUT same series)."""
    conn = _get_conn()
    row  = _db.get_series_by_path(conn, path)
    if not row:
        raise HTTPException(404, "Series not found")

    source_name = row.get("source_name") or "mangadex"
    is_mdx      = source_name == "mangadex"
    manga_id    = (row.get("config") or {}).get("id") or row.get("source_id")
    cur_lang    = row.get("language") or "en"
    prefs       = row.get("preferred_groups") or []
    parts       = path.split("/")
    folder_name = parts[-1]
    subfolder   = "/".join(parts[:-1])
    cover_fn    = (row.get("config") or {}).get("cover_filename") or ""

    if not is_mdx:
        return templates.TemplateResponse(request=request, name="partials/manga_grab_options.html",
            context={
                "path": path,
                "manga_id": "",
                "folder_name": folder_name,
                "subfolder": subfolder,
                "cover_filename": cover_fn,
                "current_lang": cur_lang,
                "lang_counts": {},
                "current_start": row.get("start_chapter", 0),
                "exclude_from_fix": bool(row.get("exclude_from_fix")),
                "sync_paused": bool(row.get("sync_paused")),
                "current_preferred_groups_json": json.dumps(prefs, ensure_ascii=False),
                "is_mdx": False,
                "groups": [],
                "total_unique": 0,
            })

    if not manga_id:
        raise HTTPException(400, "Series is not linked to MangaDex")

    loop = asyncio.get_event_loop()
    lang_counts: dict[str, int] = {}
    try:
        data = await loop.run_in_executor(
            None, lambda: _mdx._api_get(f"/manga/{manga_id}", {}, timeout=15)
        )
        available_langs = data["data"]["attributes"].get("availableTranslatedLanguages") or []
        if available_langs:
            tasks = [
                loop.run_in_executor(None, _mdx.get_lang_chapter_count, manga_id, lang)
                for lang in available_langs[:10]
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, tuple) and r[1] > 0:
                    lang_counts[r[0]] = r[1]
        lang_counts = dict(sorted(lang_counts.items(), key=lambda x: x[1], reverse=True))
    except Exception:
        pass

    groups_data = await _fetch_groups(manga_id, cur_lang)

    return templates.TemplateResponse(request=request, name="partials/manga_grab_options.html",
        context={
            "path": path,
            "manga_id": manga_id,
            "folder_name": folder_name,
            "subfolder": subfolder,
            "cover_filename": cover_fn,
            "current_lang": cur_lang,
            "lang_counts": lang_counts,
            "current_start": row.get("start_chapter", 0),
            "exclude_from_fix": bool(row.get("exclude_from_fix")),
            "sync_paused": bool(row.get("sync_paused")),
            "current_preferred_groups_json": json.dumps(prefs, ensure_ascii=False),
            "is_mdx": True,
            **groups_data,
        })


@app.put("/api/series/{path:path}")
async def update_series(
    path:           str,              # URL path parameter - the OLD series path
    manga_id:       str   = Form(...),
    title:          str   = Form(...),
    subfolder:      str   = Form(""),
    language:       str   = Form("en"),
    preferred_groups_json: str = Form("[]"),
    start_chapter:  str   = Form("0"),
    cover_filename: str   = Form(""),
    exclude_from_fix: str = Form("false"),
    merge_volumes_override: str = Form(""),
    mark_sync_configured: str = Form(""),
):
    _require_root_folders()
    # path here is the URL route parameter (old path)
    old_dir    = os.path.join(MANGA_ROOT, path)
    parts      = [p for p in [subfolder.strip("/"), title] if p]
    new_dir    = os.path.join(MANGA_ROOT, *parts)
    _require_under_manga_root(new_dir)
    new_rel    = os.path.relpath(new_dir, MANGA_ROOT)

    if os.path.abspath(old_dir) != os.path.abspath(new_dir):
        os.makedirs(os.path.dirname(new_dir) or MANGA_ROOT, exist_ok=True)
        shutil.move(old_dir, new_dir)

    try:
        start_f = float(start_chapter) if start_chapter else 0.0
    except ValueError:
        start_f = 0.0

    excl = 1 if exclude_from_fix == "true" else 0
    merge_ov = _parse_merge_volumes_override(merge_volumes_override)

    loop = asyncio.get_event_loop()
    sync_conf: int | None = 1 if mark_sync_configured == "true" else None
    _old_path, _new_rel = path, new_rel
    await loop.run_in_executor(None, lambda: _db.update_series(
        _get_conn(),
        old_path=_old_path,
        new_path=_new_rel,
        title=title,
        language=language,
        start_chapter=start_f,
        source="mangadex" if manga_id else None,
        source_id=manga_id or None,
        cover_filename=cover_filename.strip() or None,
        exclude_from_fix=excl,
        merge_volumes_override=merge_ov,
        preferred_groups_json=preferred_groups_json.strip() or None,
        sync_configured=sync_conf,
    ))
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# API - sync streams
# ---------------------------------------------------------------------------


@app.post("/api/jobs/sync-all")
async def enqueue_sync_all():
    _require_root_folders()
    conn = _get_conn()
    active = _db.get_active_jobs(conn, queue_key=JOB_QUEUE_KEY)
    for j in active:
        if j.get("job_type") == JOB_TYPE_SYNC_ALL:
            return JSONResponse({"ok": True, "job": _serialize_job(j), "deduped": True})
    job_id = _db.enqueue_job(
        conn,
        job_type=JOB_TYPE_SYNC_ALL,
        queue_key=JOB_QUEUE_KEY,
        payload={},
    )
    _db.increment_usage(conn, "syncs_all")
    return JSONResponse({"ok": True, "job": _serialize_job(_db.get_job(conn, job_id)), "deduped": False})


@app.post("/api/jobs/reconcile-disk")
async def enqueue_reconcile_disk():
    _require_root_folders()
    conn = _get_conn()
    active = _db.get_active_jobs(conn, queue_key=JOB_QUEUE_KEY)
    for j in active:
        if j.get("job_type") == JOB_TYPE_RECONCILE_DISK:
            return JSONResponse({"ok": True, "job": _serialize_job(j), "deduped": True})
    job_id = _db.enqueue_job(
        conn,
        job_type=JOB_TYPE_RECONCILE_DISK,
        queue_key=JOB_QUEUE_KEY,
        payload={"reason": "manual"},
    )
    return JSONResponse({"ok": True, "job": _serialize_job(_db.get_job(conn, job_id)), "deduped": False})


@app.post("/api/jobs/sync-series/{path:path}")
async def enqueue_sync_series(path: str):
    _require_root_folders()
    conn = _get_conn()
    row = _db.get_series_by_path(conn, path)
    if not row:
        raise HTTPException(404, "Series not found")
    existing = _db.get_active_job_for_series(conn, row["id"])
    if existing:
        return JSONResponse({"ok": True, "job": _serialize_job(existing), "deduped": True})
    job_id = _db.enqueue_job(
        conn,
        job_type=JOB_TYPE_SYNC_SERIES,
        queue_key=JOB_QUEUE_KEY,
        series_id=row["id"],
        series_path_snapshot=path,
        payload={"series_path": path},
    )
    _db.increment_usage(conn, "syncs_manual")
    return JSONResponse({"ok": True, "job": _serialize_job(_db.get_job(conn, job_id)), "deduped": False})


@app.post("/api/jobs/regenerate-comicinfo/{path:path}")
async def enqueue_regenerate_comicinfo(path: str):
    _require_root_folders()
    conn = _get_conn()
    row = _db.get_series_by_path(conn, path)
    if not row:
        raise HTTPException(404, "Series not found")
    existing = _db.get_active_job_for_series(conn, row["id"])
    if existing:
        return JSONResponse({"ok": True, "job": _serialize_job(existing), "deduped": True})
    job_id = _db.enqueue_job(
        conn,
        job_type=JOB_TYPE_REGEN_COMICINFO,
        queue_key=JOB_QUEUE_KEY,
        series_id=row["id"],
        series_path_snapshot=path,
        payload={"series_path": path},
    )
    return JSONResponse({"ok": True, "job": _serialize_job(_db.get_job(conn, job_id)), "deduped": False})


@app.get("/api/jobs")
async def list_jobs():
    conn = _get_conn()
    running = [_serialize_job(j) for j in _db.list_jobs(conn, statuses=["running"], limit=50)]
    queued = [_serialize_job(j) for j in _db.list_jobs(conn, statuses=["queued"], limit=200)]
    recent = [_serialize_job(j) for j in _db.list_jobs(conn, statuses=["completed", "failed", "cancelled"], limit=200)]
    return JSONResponse({"running": running, "queued": queued, "recent": recent})


@app.get("/api/jobs/active")
async def get_active_jobs():
    conn = _get_conn()
    jobs = [_serialize_job(j) for j in _db.get_active_jobs(conn, queue_key=JOB_QUEUE_KEY)]
    return JSONResponse({"jobs": jobs, "current": jobs[0] if jobs else None})


@app.get("/api/jobs/series/{path:path}/active")
async def get_active_job_for_series(path: str):
    conn = _get_conn()
    row = _db.get_series_by_path(conn, path)
    if not row:
        raise HTTPException(404, "Series not found")
    job = _db.get_active_job_for_series(conn, row["id"])
    return JSONResponse({"job": _serialize_job(job) if job else None})


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: int):
    conn = _get_conn()
    job = _db.get_job(conn, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return JSONResponse({"job": _serialize_job(job)})


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: int):
    conn = _get_conn()
    job = _db.get_job(conn, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    status = job.get("status")
    if status in JOB_STATUS_TERMINAL:
        return JSONResponse({"ok": True, "already_terminal": True, "job": _serialize_job(job)})
    if status == "queued":
        if _db.cancel_queued_job(conn, job_id):
            _db.append_job_log(conn, job_id, "[job] cancelled while queued")
        return JSONResponse({"ok": True, "job": _serialize_job(_db.get_job(conn, job_id))})
    if status == "running":
        _cancel_requested_job_ids.add(job_id)
        if _worker_current_job_id == job_id and _worker_current_proc and _worker_current_proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                _worker_current_proc.terminate()
        _db.append_job_log(conn, job_id, "[job] cancellation requested")
        return JSONResponse({"ok": True, "job": _serialize_job(_db.get_job(conn, job_id)), "cancelling": True})
    return JSONResponse({"ok": False, "error": f"Unsupported job state: {status}"}, status_code=400)


@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: int, from_seq: int = 0):
    conn = _get_conn()
    if not _db.get_job(conn, job_id):
        raise HTTPException(404, "Job not found")

    async def generate():
        nonlocal from_seq
        last_status_sent = None
        while True:
            lines = _db.get_job_logs_since(conn, job_id, from_seq=from_seq, limit=200)
            for ln in lines:
                from_seq = int(ln["seq"])
                payload = {
                    "type": "line",
                    "seq": from_seq,
                    "line": ln["line"],
                    "ts": ln["ts"],
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            job = _db.get_job(conn, job_id)
            if not job:
                payload = {"type": "status", "status": "missing"}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                break
            cur_status = job.get("status")
            if cur_status != last_status_sent:
                payload = {
                    "type": "status",
                    "status": cur_status,
                    "exit_code": job.get("exit_code"),
                    "error_summary": job.get("error_summary"),
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                last_status_sent = cur_status
            if job["status"] in JOB_STATUS_TERMINAL:
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )

@app.get("/api/sync/stream")
async def sync_stream():
    conn = _get_conn()
    active = _db.get_active_jobs(conn, queue_key=JOB_QUEUE_KEY)
    job = None
    for j in active:
        if j.get("job_type") == JOB_TYPE_SYNC_ALL:
            job = j
            break
    if not job:
        job_id = _db.enqueue_job(conn, job_type=JOB_TYPE_SYNC_ALL, queue_key=JOB_QUEUE_KEY, payload={})
        job = _db.get_job(conn, job_id)
    job_id = job["id"]

    async def generate():
        from_seq = 0
        while True:
            lines = _db.get_job_logs_since(conn, job_id, from_seq=from_seq, limit=200)
            for ln in lines:
                from_seq = int(ln["seq"])
                yield f"data: {ln['line']}\n\n"
            cur = _db.get_job(conn, job_id)
            if cur and cur.get("status") in JOB_STATUS_TERMINAL:
                break
            await asyncio.sleep(0.4)
        yield "data: [done]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.get("/api/series/{path:path}/sync/stream")
async def series_sync_stream(path: str):
    conn = _get_conn()
    row = _db.get_series_by_path(conn, path)
    if not row:
        raise HTTPException(404, "Series not found")
    job = _db.get_active_job_for_series(conn, row["id"])
    if not job:
        job_id = _db.enqueue_job(
            conn,
            job_type=JOB_TYPE_SYNC_SERIES,
            queue_key=JOB_QUEUE_KEY,
            series_id=row["id"],
            series_path_snapshot=path,
            payload={"series_path": path},
        )
        job = _db.get_job(conn, job_id)
    job_id = job["id"]

    async def generate():
        from_seq = 0
        while True:
            lines = _db.get_job_logs_since(conn, job_id, from_seq=from_seq, limit=200)
            for ln in lines:
                from_seq = int(ln["seq"])
                yield f"data: {ln['line']}\n\n"
            cur = _db.get_job(conn, job_id)
            if cur and cur.get("status") in JOB_STATUS_TERMINAL:
                break
            await asyncio.sleep(0.4)
        yield "data: [done]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.post("/api/series/{path:path}/covers")
async def series_covers(path: str):
    _require_root_folders()
    conn = _get_conn()
    row  = _db.get_series_by_path(conn, path)
    if not row:
        raise HTTPException(404, "Series not found")
    settings = load_settings()
    if not settings.get("kavita_url") or not settings.get("kavita_api_key"):
        return JSONResponse({"ok": False, "error": "Kavita not configured in settings"})
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, MANGA_SYNC_SCRIPT,
            "--series", os.path.join(MANGA_ROOT, path),
            "--covers-only",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "MANGA_ROOT": MANGA_ROOT, "DATA_DIR": DATA_DIR},
        )
        stdout, _ = await proc.communicate()
        return JSONResponse({"ok": proc.returncode == 0,
                             "output": stdout.decode().strip()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/series/{path:path}/comicinfo-regenerate")
async def series_regenerate_comicinfo(path: str):
    _require_root_folders()
    conn = _get_conn()
    if not _db.get_series_by_path(conn, path):
        raise HTTPException(404, "Series not found")
    series_dir = os.path.join(MANGA_ROOT, path)
    if not os.path.isdir(series_dir):
        return JSONResponse({"ok": False, "error": "Series folder not found on disk"})
    if not os.path.isfile(MANGA_SYNC_SCRIPT):
        return JSONResponse({"ok": False, "error": "manga-sync.py not found"})
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            MANGA_SYNC_SCRIPT,
            "--series",
            series_dir,
            "--regenerate-comicinfo",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "MANGA_ROOT": MANGA_ROOT, "DATA_DIR": DATA_DIR},
        )
        stdout, _ = await proc.communicate()
        log_text = stdout.decode(errors="replace").strip()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return JSONResponse({"ok": proc.returncode == 0, "log": log_text})


@app.get("/api/series/{path:path}/comicinfo-regenerate/stream")
async def series_regenerate_comicinfo_stream(path: str):
    _require_root_folders()
    conn = _get_conn()
    if not _db.get_series_by_path(conn, path):
        raise HTTPException(404, "Series not found")
    series_dir = os.path.join(MANGA_ROOT, path)
    if not os.path.isdir(series_dir):
        raise HTTPException(404, "Series folder not found on disk")
    if not os.path.isfile(MANGA_SYNC_SCRIPT):
        raise HTTPException(500, "manga-sync.py not found")

    async def generate():
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                MANGA_SYNC_SCRIPT,
                "--series",
                series_dir,
                "--regenerate-comicinfo",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "MANGA_ROOT": MANGA_ROOT, "DATA_DIR": DATA_DIR},
            )
            async for line in proc.stdout:
                text = line.decode(errors="replace").rstrip()
                if text:
                    yield f"data: {text}\n\n"
            await proc.wait()
            if proc.returncode != 0:
                yield f"data: [error] regenerate failed (exit {proc.returncode})\n\n"
        except Exception as e:
            yield f"data: [error] {e}\n\n"
        yield "data: [done]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/api/series/{path:path}/compact-volumes")
async def series_compact_volumes(path: str):
    _require_root_folders()
    conn = _get_conn()
    if not _db.get_series_by_path(conn, path):
        raise HTTPException(404, "Series not found")
    series_dir = os.path.join(MANGA_ROOT, path)
    if not os.path.isdir(series_dir):
        return JSONResponse({"ok": False, "error": "Series folder not found on disk"})
    if not os.path.isfile(MANGA_SYNC_SCRIPT):
        return JSONResponse({"ok": False, "error": "manga-sync.py not found"})
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            MANGA_SYNC_SCRIPT,
            "--series",
            series_dir,
            "--compact-volumes",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "MANGA_ROOT": MANGA_ROOT, "DATA_DIR": DATA_DIR},
        )
        stdout, _ = await proc.communicate()
        log_text = stdout.decode(errors="replace").strip()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return JSONResponse({"ok": proc.returncode == 0, "log": log_text})


@app.post("/api/series/{path:path}/reset-source-metadata")
async def series_reset_source_metadata(path: str):
    """Detach per-chapter source metadata from files already on disk.

    Use this when a series was linked just for covers / catalog progress and
    chapter rows ended up with foreign metadata (e.g. ``LanguageISO=vi`` on
    English archives). Catalog rows that have no file are left intact, so
    sync can still discover and download missing chapters afterwards.
    """
    _require_root_folders()
    conn = _get_conn()
    row = _db.get_series_by_path(conn, path)
    if not row:
        raise HTTPException(404, "Series not found")
    n = _db.reset_chapter_source_metadata(conn, row["id"])
    return JSONResponse({"ok": True, "reset": int(n)})


@app.post("/api/series/{path:path}/delete-files")
async def series_delete_files(path: str, body: DeleteSeriesFilesBody):
    """Delete chapter/volume archive files on disk for this series; DB is reconciled via scan."""
    _require_root_folders()
    conn = _get_conn()
    row = _db.get_series_by_path(conn, path)
    if not row:
        raise HTTPException(404, "Series not found")
    series_id = row["id"]
    n = len(body.chapter_ids) + len(body.volume_ids)
    if n == 0:
        raise HTTPException(400, "No files selected")
    if n > 500:
        raise HTTPException(400, "Too many files at once (max 500)")

    deleted: list[str] = []
    errors: list[str] = []

    for vid in body.volume_ids:
        r = conn.execute(
            "SELECT id, path FROM volumes WHERE id=? AND series_id=?",
            (vid, series_id),
        ).fetchone()
        if not r or not r["path"]:
            errors.append(f"Volume #{vid}: not found or no file tracked")
            continue
        ext = os.path.splitext(r["path"])[1].lower()
        if ext not in _db.MANGA_EXTENSIONS:
            errors.append(f"Volume #{vid}: unsupported file type")
            continue
        abs_f = _realpath_under_series(path, r["path"])
        if not abs_f:
            errors.append(f"Volume #{vid}: path not under series folder")
            continue
        if os.path.isfile(abs_f):
            try:
                os.remove(abs_f)
            except OSError as e:
                errors.append(f"{os.path.basename(r['path'])}: {e}")
                continue
        _db.log_rename(
            conn,
            r["path"],
            None,
            "delete",
            "web_series_detail",
            series_id=series_id,
            volume_id=vid,
        )
        deleted.append(os.path.basename(r["path"]))

    for cid in body.chapter_ids:
        r = conn.execute(
            "SELECT id, path FROM chapters WHERE id=? AND series_id=?",
            (cid, series_id),
        ).fetchone()
        if not r or not r["path"]:
            errors.append(f"Chapter #{cid}: not found or no file tracked")
            continue
        ext = os.path.splitext(r["path"])[1].lower()
        if ext not in _db.MANGA_EXTENSIONS:
            errors.append(f"Chapter #{cid}: unsupported file type")
            continue
        abs_f = _realpath_under_series(path, r["path"])
        if not abs_f:
            errors.append(f"Chapter #{cid}: path not under series folder")
            continue
        if os.path.isfile(abs_f):
            try:
                os.remove(abs_f)
            except OSError as e:
                errors.append(f"{os.path.basename(r['path'])}: {e}")
                continue
        _db.log_rename(
            conn,
            r["path"],
            None,
            "delete",
            "web_series_detail",
            series_id=series_id,
            chapter_id=cid,
        )
        deleted.append(os.path.basename(r["path"]))

    series_dir = os.path.join(MANGA_ROOT, path)
    _db.scan_disk_files(series_dir, series_id, conn)
    if deleted:
        _db.increment_usage(conn, "file_deletes", len(deleted))
    return JSONResponse(
        {
            "ok": len(errors) == 0,
            "deleted": deleted,
            "errors": errors,
        }
    )


@app.get("/api/comicinfo/series/{path:path}/chapters/{chapter_id}")
async def get_chapter_comicinfo(path: str, chapter_id: int):
    conn = _get_conn()
    row = _db.get_series_by_path(conn, path)
    if not row:
        raise HTTPException(404, "Series not found")
    r = conn.execute(
        """
        SELECT c.id, c.path, c.chapter_num, c.title, v.volume_num
        FROM chapters c
        LEFT JOIN volumes v ON v.id = c.volume_id
        WHERE c.id=? AND c.series_id=?
        """,
        (chapter_id, row["id"]),
    ).fetchone()
    if not r or not r["path"]:
        raise HTTPException(404, "Chapter file not found")
    abs_f = _realpath_under_series(path, r["path"])
    if not abs_f or not os.path.isfile(abs_f):
        raise HTTPException(404, "Chapter archive missing on disk")
    xml = read_comicinfo_xml(abs_f)
    fields = parse_comicinfo_fields(xml)
    if not fields.get("Series"):
        fields["Series"] = row.get("name") or row.get("title") or ""
    if not fields.get("Manga"):
        fields["Manga"] = "YesAndRightToLeft"
    if not fields.get("Number") and r["chapter_num"] is not None:
        fields["Number"] = format_num(r["chapter_num"])
    if not fields.get("Volume") and r["volume_num"] is not None:
        fields["Volume"] = format_num(r["volume_num"])
    if not fields.get("Title") and r["title"]:
        fields["Title"] = str(r["title"]).strip()
    return JSONResponse({
        "ok": True,
        "kind": "chapter",
        "id": int(r["id"]),
        "path": r["path"],
        "xml": xml or "",
        "fields": fields,
    })


@app.put("/api/comicinfo/series/{path:path}/chapters/{chapter_id}")
async def update_chapter_comicinfo(path: str, chapter_id: int, body: ComicInfoUpdateBody):
    _require_root_folders()
    conn = _get_conn()
    row = _db.get_series_by_path(conn, path)
    if not row:
        raise HTTPException(404, "Series not found")
    r = conn.execute(
        "SELECT id, path FROM chapters WHERE id=? AND series_id=?",
        (chapter_id, row["id"]),
    ).fetchone()
    if not r or not r["path"]:
        raise HTTPException(404, "Chapter file not found")
    abs_f = _realpath_under_series(path, r["path"])
    if not abs_f or not os.path.isfile(abs_f):
        raise HTTPException(404, "Chapter archive missing on disk")
    fields = dict(body.fields or {})
    if not str(fields.get("Series", "")).strip():
        fields["Series"] = row.get("name") or row.get("title") or ""
    _validate_comicinfo_fields(fields)
    xml = _comicinfo_fields_to_xml(fields)
    ok = inject_comicinfo(
        abs_f,
        xml,
        overwrite=True,
        file_permission_mask=load_settings().get("file_permission_mask"),
    )
    if not ok:
        raise HTTPException(500, "Could not write ComicInfo.xml")
    _db.mark_chapter_comicinfo(conn, int(r["id"]))
    _db.increment_usage(conn, "comicinfo_edits")
    return JSONResponse({"ok": True, "xml": xml})


@app.get("/api/comicinfo/series/{path:path}/volumes/{volume_id}")
async def get_volume_comicinfo(path: str, volume_id: int):
    conn = _get_conn()
    row = _db.get_series_by_path(conn, path)
    if not row:
        raise HTTPException(404, "Series not found")
    r = conn.execute(
        "SELECT id, path, volume_num, title FROM volumes WHERE id=? AND series_id=?",
        (volume_id, row["id"]),
    ).fetchone()
    if not r or not r["path"]:
        raise HTTPException(404, "Volume file not found")
    abs_f = _realpath_under_series(path, r["path"])
    if not abs_f or not os.path.isfile(abs_f):
        raise HTTPException(404, "Volume archive missing on disk")
    xml = read_comicinfo_xml(abs_f)
    fields = parse_comicinfo_fields(xml)
    if not fields.get("Series"):
        fields["Series"] = row.get("name") or row.get("title") or ""
    if not fields.get("Manga"):
        fields["Manga"] = "YesAndRightToLeft"
    if not fields.get("Volume") and r["volume_num"] is not None:
        fields["Volume"] = format_num(r["volume_num"])
    if not fields.get("Title") and r["title"]:
        fields["Title"] = r["title"]
    return JSONResponse({
        "ok": True,
        "kind": "volume",
        "id": int(r["id"]),
        "path": r["path"],
        "xml": xml or "",
        "fields": fields,
    })


@app.put("/api/comicinfo/series/{path:path}/volumes/{volume_id}")
async def update_volume_comicinfo(path: str, volume_id: int, body: ComicInfoUpdateBody):
    _require_root_folders()
    conn = _get_conn()
    row = _db.get_series_by_path(conn, path)
    if not row:
        raise HTTPException(404, "Series not found")
    r = conn.execute(
        "SELECT id, path FROM volumes WHERE id=? AND series_id=?",
        (volume_id, row["id"]),
    ).fetchone()
    if not r or not r["path"]:
        raise HTTPException(404, "Volume file not found")
    abs_f = _realpath_under_series(path, r["path"])
    if not abs_f or not os.path.isfile(abs_f):
        raise HTTPException(404, "Volume archive missing on disk")
    fields = dict(body.fields or {})
    if not str(fields.get("Series", "")).strip():
        fields["Series"] = row.get("name") or row.get("title") or ""
    _validate_comicinfo_fields(fields)
    xml = _comicinfo_fields_to_xml(fields)
    ok = inject_comicinfo(
        abs_f,
        xml,
        overwrite=True,
        file_permission_mask=load_settings().get("file_permission_mask"),
    )
    if not ok:
        raise HTTPException(500, "Could not write ComicInfo.xml")
    _db.mark_volume_comicinfo(conn, int(r["id"]))
    _db.increment_usage(conn, "comicinfo_edits")
    return JSONResponse({"ok": True, "xml": xml})


# ---------------------------------------------------------------------------
# API - fix
# ---------------------------------------------------------------------------

@app.post("/api/fix/apply", response_class=HTMLResponse)
async def apply_fix(
    old_path:   str = Form(...),
    new_name:   str = Form(...),
    issue_name: str = Form(...),
):
    _require_root_folders()
    _require_under_manga_root(old_path)
    log_path = os.path.join(MANGA_ROOT, _fix.LOG_FILENAME)
    log_data = _fix.load_log(log_path)
    try:
        new_path = _fix.do_rename(old_path, new_name, issue_name, log_data, log_path)
    except Exception as e:
        raise HTTPException(500, str(e))
    _rescan_series_disk_after_fix(new_path)
    return HTMLResponse("")


@app.post("/api/fix/series-skip", response_class=HTMLResponse)
async def skip_series_from_fix(series_path: str = Form(...)):
    _require_root_folders()
    conn = _get_conn()
    row = _db.get_series_by_path(conn, series_path)
    if not row:
        raise HTTPException(404, "Series not found")
    try:
        _db.update_series(
            conn,
            old_path=series_path,
            new_path=series_path,
            title=row["title"],
            language=row["language"],
            start_chapter=float(row.get("start_chapter") or 0),
            source=row.get("source_name"),
            source_id=row.get("source_id"),
            cover_filename=row["config"].get("cover_filename"),
            exclude_from_fix=1,
            merge_volumes_override=row.get("merge_volumes_override"),
            preferred_groups_json=row.get("preferred_groups_json"),
            preferred_group=row.get("preferred_group"),
        )
    except Exception as e:
        raise HTTPException(500, str(e))
    return HTMLResponse("")


@app.post("/api/fix/series-apply-all")
async def apply_all_series_fixes(series_path: str = Form(...)):
    _require_root_folders()
    conn = _get_conn()
    row = _db.get_series_by_path(conn, series_path)
    if not row:
        raise HTTPException(404, "Series not found")
    series_dir = os.path.join(MANGA_ROOT, series_path)
    if not os.path.isdir(series_dir):
        raise HTTPException(404, "Series folder not found on disk")

    log_path = os.path.join(MANGA_ROOT, _fix.LOG_FILENAME)
    log_data = _fix.load_log(log_path)
    renamed = 0
    skipped_missing = 0
    seen_paths: set[str] = set()

    findings = list(_fix.scan(series_dir))
    loop = asyncio.get_event_loop()
    _sp = series_path
    findings.extend(await loop.run_in_executor(None, lambda: _scan_settings_naming_issues(series_path=_sp)))
    findings.sort(key=lambda x: x[0].lower())
    for old_path, issue_name, new_name in findings:
        if old_path in seen_paths:
            continue
        seen_paths.add(old_path)
        if not os.path.exists(old_path):
            skipped_missing += 1
            continue
        try:
            _fix.do_rename(old_path, new_name, issue_name, log_data, log_path)
            renamed += 1
        except Exception:
            continue

    dup_groups = _fix.scan_duplicates(series_dir)
    dedup_applied = 0
    for group in dup_groups:
        try:
            _fix.apply_dup_group(group, log_data, log_path)
            dedup_applied += 1
        except Exception:
            continue

    _db.scan_disk_files(series_dir, row["id"], conn)
    return JSONResponse({
        "ok": True,
        "series_path": series_path,
        "renamed": renamed,
        "dedup_groups": dedup_applied,
        "resolved": renamed + dedup_applied,
        "skipped_missing": skipped_missing,
    })


@app.post("/api/fix/apply-dup", response_class=HTMLResponse)
async def apply_dup(
    keep_path:    str = Form(...),
    delete_paths: str = Form(...),
    needs_rename: str = Form(""),
    keep_name:    str = Form(...),
):
    _require_root_folders()
    log_path = os.path.join(MANGA_ROOT, _fix.LOG_FILENAME)
    log_data = _fix.load_log(log_path)
    all_paths = [keep_path] + [x for x in delete_paths.split("|") if x]
    sizes = {p: os.path.getsize(p) for p in all_paths if os.path.exists(p)}
    group = {
        "keep_path":    keep_path,
        "keep_name":    keep_name,
        "needs_rename": needs_rename == "true",
        "delete_paths": [x for x in delete_paths.split("|") if x],
        "sizes":        sizes,
    }
    try:
        _fix.apply_dup_group(group, log_data, log_path)
    except Exception as e:
        raise HTTPException(500, str(e))
    _rescan_series_disk_after_fix(keep_path)
    return HTMLResponse("")
