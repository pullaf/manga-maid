import os
import re
import time
from file_permissions import sanitize_file_permission_mask

# Settings are stored in SQLite (``app_config`` table under ``DATA_DIR/db/``).
# ``CONFIG_PATH`` env still selects a legacy ``settings.json`` for one-time import
# (see ``db.legacy_settings_json_path``).
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/config/settings.json")

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
    "telemetry_enabled": True,
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


def sanitize_sync_cron(expr: str | None) -> str:
    s = str(expr or "").strip()
    if not s:
        return DEFAULTS["sync_cron"]
    parts = s.split()
    if len(parts) != 5:
        return DEFAULTS["sync_cron"]
    allowed = re.compile(r"^[\d\*/,\-A-Za-z]+$")
    if not all(allowed.match(p) for p in parts):
        return DEFAULTS["sync_cron"]
    return " ".join(parts)


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
        data.pop("volume_mode", None)
        data.pop("merge_volume_naming", None)
        out = {**DEFAULTS, **data}
    finally:
        conn.close()
    out.pop("merge_volume_naming", None)
    out["volume_naming"] = sanitize_volume_naming(out.get("volume_naming"))
    out["sync_cron"] = sanitize_sync_cron(out.get("sync_cron"))
    out["file_permission_mask"] = sanitize_file_permission_mask(out.get("file_permission_mask"))
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
