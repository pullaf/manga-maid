import os
import re
import time
from file_permissions import sanitize_file_permission_mask
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sources.suwayomi import SuwayomiClient

# Settings are stored in SQLite (``app_config`` table under ``DATA_DIR/db/``).
# ``CONFIG_PATH`` env still selects a legacy ``settings.json`` for one-time import
# (see ``db.legacy_settings_json_path``).
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/config/settings.json")

# Stored in settings when the user turns off automatic sync (supercronic gets no job lines).
SYNC_CRON_DISABLED = "disabled"

DEFAULTS = {
    "root_folders":    [],
    "file_format":     "cbz",
    "chapter_naming":  "[%1 %2] %3 vol.%4 ch.%5",
    "volume_naming":   "[%1 %2] %3 vol.%4",
    "download_delay":  1.0,
    "sync_cron":       "0 */6 * * *",
    "merge_volumes":   False,
    "auto_covers":     False,
    "auto_scan":       False,
    "kavita_url":      "",
    "kavita_api_key":  "",
    "file_permission_mask": "664",
    "webhook_url":     "",
    "webhook_platform": "generic",  # generic | discord | ntfy
    # Failures only fire when a series starts failing or recovers, not every run.
    "webhook_notify_downloads": True,
    "webhook_notify_failures":  True,
    "telemetry_enabled": True,
    # Locale preferences. Per-series ``*_language_override`` columns win when set.
    # ``original`` / ``romanized`` / a locale code; filename also takes
    # ``match_title``. Blank means "not chosen yet": titles stay exactly as they
    # are and files on disk are never rewritten. Upgrading must not rename or
    # re-cover anyone's library. Covers still resolve to ``original`` when
    # unset, but only for volumes merged from then on.
    "title_language":    "",
    "cover_language":    "",
    "filename_language": "",
    "suwayomi_url":      "",
    "suwayomi_username": "",
    "suwayomi_password": "",
    "enabled_sources":   [],
    # MangaDex /aggregate volume remap when chapters on disk lack volume_id:
    # at most once per N days unless a download run triggers it sooner.
    "aggregate_volume_remap_interval_days": 7,
}


def _sanitize_volume_stem_template(template: str | None, default: str) -> str:
    """Strip %5 / %6 and stray ``ch.`` from patterns meant for volume-only stems."""
    t = (template or "").strip() or default
    t = t.replace("%5", "").replace("%6", "")
    t = re.sub(r"\s+ch\.\s*", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\[\s*\]", "", t).strip()
    if not t:
        t = default
    return t


def sanitize_volume_naming(template: str | None) -> str:
    """Volume CBZ stems (per-volume files, merges, Fix Files). No %5 / %6."""
    return _sanitize_volume_stem_template(template, DEFAULTS["volume_naming"])


def is_sync_cron_disabled(expr: str | None) -> bool:
    """True when automatic enqueue-from-cron should not run (manual Jobs UI only)."""
    s = str(expr or "").strip().lower()
    return s in (SYNC_CRON_DISABLED, "off", "manual")


# (min, max, names) per field. supercronic is PID 1 and exits on a crontab
# line it cannot parse, which stops the container, so anything written to the
# crontab must pass this check. Deliberately stricter than supercronic.
_CRON_FIELDS = (
    (0, 59, ()),
    (0, 23, ()),
    (1, 31, ()),
    (1, 12, ("jan", "feb", "mar", "apr", "may", "jun",
             "jul", "aug", "sep", "oct", "nov", "dec")),
    (0, 7, ("sun", "mon", "tue", "wed", "thu", "fri", "sat")),
)
_CRON_ITEM_RE = re.compile(r"^(\*|([a-z]+|\d+)(-([a-z]+|\d+))?)(/(\d+))?$")


def _cron_value(token: str, lo: int, hi: int, names: tuple) -> int | None:
    if token.isdigit():
        v = int(token)
    elif token in names:
        v = names.index(token) + (1 if lo == 1 else 0)
    else:
        return None
    return v if lo <= v <= hi else None


def is_valid_cron_expr(expr: str | None) -> bool:
    """True for a 5-field cron expression with in-range values."""
    parts = str(expr or "").strip().lower().split()
    if len(parts) != 5:
        return False
    for part, (lo, hi, names) in zip(parts, _CRON_FIELDS):
        for item in part.split(","):
            m = _CRON_ITEM_RE.match(item)
            if not m:
                return False
            if m.group(6) is not None and int(m.group(6)) < 1:
                return False
            if m.group(1) == "*":
                continue
            start = _cron_value(m.group(2), lo, hi, names)
            if start is None:
                return False
            if m.group(4) is not None:
                end = _cron_value(m.group(4), lo, hi, names)
                if end is None or end < start:
                    return False
    return True


def sanitize_sync_cron(expr: str | None) -> str:
    raw = str(expr or "").strip()
    if is_sync_cron_disabled(raw):
        return SYNC_CRON_DISABLED
    if not is_valid_cron_expr(raw):
        return DEFAULTS["sync_cron"]
    return " ".join(raw.split())


_settings_cache: dict | None = None
_settings_cache_at: float = 0.0
_SETTINGS_CACHE_TTL = 5.0


def load_settings() -> dict:
    global _settings_cache, _settings_cache_at
    now = time.monotonic()
    if _settings_cache is not None and (now - _settings_cache_at) < _SETTINGS_CACHE_TTL:
        return _settings_cache

    from db import init_db, read_stored_settings

    conn = init_db()
    try:
        data = read_stored_settings(conn)
        raw_sw = (data.get("suwayomi_url") or "").strip()
        raw_kv = (data.get("kavita_url") or "").strip()
        data.pop("volume_mode", None)
        data.pop("merge_volume_naming", None)
        out = {**DEFAULTS, **data}
    finally:
        conn.close()
    out.pop("merge_volume_naming", None)
    out["volume_naming"] = sanitize_volume_naming(out.get("volume_naming"))
    out["sync_cron"] = sanitize_sync_cron(out.get("sync_cron"))
    out["file_permission_mask"] = sanitize_file_permission_mask(out.get("file_permission_mask"))
    # Optional Docker Compose defaults when the DB still has blank URLs (omit env lines or set
    # SUWAYOMI_URL= / KAVITA_URL= via override to leave unset in the container).
    if not (out.get("suwayomi_url") or "").strip():
        sw = (os.environ.get("SUWAYOMI_URL") or "").strip().rstrip("/")
        if sw:
            out["suwayomi_url"] = sw
    if not (out.get("kavita_url") or "").strip():
        kv = (os.environ.get("KAVITA_URL") or "").strip().rstrip("/")
        if kv:
            out["kavita_url"] = kv
    # Persist compose/env URLs once when the DB row had blanks so Settings matches runtime.
    seed: dict[str, str] = {}
    if not raw_sw and (out.get("suwayomi_url") or "").strip():
        seed["suwayomi_url"] = out["suwayomi_url"]
    if not raw_kv and (out.get("kavita_url") or "").strip():
        seed["kavita_url"] = out["kavita_url"]
    if seed:
        save_settings(seed)
    _settings_cache = out
    _settings_cache_at = now
    return out


def save_settings(data: dict):
    global _settings_cache_at
    _settings_cache_at = 0.0  # invalidate cache
    data = dict(data)
    data.pop("volume_mode", None)
    data.pop("merge_volume_naming", None)
    from db import init_db, read_stored_settings, write_stored_settings

    conn = init_db()
    try:
        stored = read_stored_settings(conn)
        stored.pop("volume_mode", None)
        stored.pop("merge_volume_naming", None)
        current = {**DEFAULTS, **stored}
        merged = {**current, **data}
        merged.pop("merge_volume_naming", None)
        merged["volume_naming"] = sanitize_volume_naming(merged.get("volume_naming"))
        merged["sync_cron"] = sanitize_sync_cron(merged.get("sync_cron"))
        merged["file_permission_mask"] = sanitize_file_permission_mask(merged.get("file_permission_mask"))
        write_stored_settings(conn, merged)
    finally:
        conn.close()


_suwayomi_client_cache: dict = {"key": None, "client": None}


def get_suwayomi_client() -> "SuwayomiClient | None":
    """Return a cached SuwayomiClient; recreates it only when settings change."""
    from sources.suwayomi import SuwayomiClient

    settings = load_settings()
    url = (settings.get("suwayomi_url") or "").strip().rstrip("/")
    if not url:
        _suwayomi_client_cache["key"] = None
        _suwayomi_client_cache["client"] = None
        return None
    key = (url, settings.get("suwayomi_username", ""), settings.get("suwayomi_password", ""))
    if _suwayomi_client_cache["key"] != key:
        _suwayomi_client_cache["key"] = key

        def _persist_auth_mode(mode: str) -> None:
            save_settings({"suwayomi_auth_mode": mode})

        _suwayomi_client_cache["client"] = SuwayomiClient(
            *key,
            auth_mode=settings.get("suwayomi_auth_mode", ""),
            on_auth_mode_change=_persist_auth_mode,
        )
    return _suwayomi_client_cache["client"]
