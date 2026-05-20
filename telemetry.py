"""Anonymous opt-out telemetry - pings a Cloudflare Worker with aggregate usage stats.

Disable by setting TELEMETRY=false in the container environment.
No personal data, no IPs stored. Instance ID is a random UUID generated once
and stored in DATA_DIR/instance_id.
"""
import json
import os
import uuid
from urllib import request as urlrequest

DATA_DIR = os.environ.get("DATA_DIR", "/data")
APP_VERSION = os.environ.get("APP_VERSION", "unknown")

_ENDPOINT = os.environ.get("TELEMETRY_ENDPOINT", "https://manga-sync-telemetry.themdk.workers.dev/ping")

_ID_PATH = os.path.join(DATA_DIR, "instance_id")


def _instance_id() -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        val = open(_ID_PATH).read().strip()
        if val:
            return val
    except FileNotFoundError:
        pass
    uid = str(uuid.uuid4())
    with open(_ID_PATH, "w") as f:
        f.write(uid)
    return uid


def _percentile(values: list, p: float):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


def _bucket(n: int) -> str:
    if n <= 5:   return "1-5"
    if n <= 20:  return "6-20"
    if n <= 50:  return "21-50"
    if n <= 100: return "51-100"
    return "100+"


def _gather_stats(conn, settings: dict) -> dict:
    """Point-in-time aggregate stats from the local DB."""
    rows = conn.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN ss.source_id IS NOT NULL THEN 1 ELSE 0 END) AS linked,
            SUM(CASE WHEN COALESCE(s.ignored, 0) = 1 THEN 1 ELSE 0 END) AS ignored,
            SUM(CASE WHEN ss.source LIKE 'suwayomi:%' OR ss.source = 'suwayomi' THEN 1 ELSE 0 END) AS suwayomi,
            SUM(CASE WHEN COALESCE(s.start_chapter, 0) > 0 THEN 1 ELSE 0 END) AS start_chapter_active,
            SUM(CASE WHEN s.preferred_groups_json IS NOT NULL
                          AND s.preferred_groups_json != '[]'
                          AND s.preferred_groups_json != '' THEN 1 ELSE 0 END) AS groups_active,
            SUM(CASE WHEN COALESCE(s.sync_configured, 0) = 1
                          AND ss.source_id IS NOT NULL THEN 1 ELSE 0 END) AS sync_configured
        FROM series s
        LEFT JOIN series_sources ss ON ss.id = (
            SELECT ss2.id FROM series_sources ss2
            WHERE ss2.series_id = s.id
            ORDER BY ss2.priority ASC, ss2.source ASC
            LIMIT 1
        )
    """).fetchone()

    vol_files = conn.execute("SELECT COUNT(*) FROM volumes WHERE path IS NOT NULL").fetchone()[0]
    ch_files  = conn.execute("SELECT COUNT(*) FROM chapters WHERE path IS NOT NULL").fetchone()[0]

    langs = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT language FROM series WHERE language IS NOT NULL"
        ).fetchall()
    ]

    return {
        "total":               rows["total"] or 0,
        "linked":              rows["linked"] or 0,
        "unlinked":            (rows["total"] or 0) - (rows["linked"] or 0) - (rows["ignored"] or 0),
        "ignored":             rows["ignored"] or 0,
        "suwayomi":            rows["suwayomi"] or 0,
        "start_chapter_active":rows["start_chapter_active"] or 0,
        "groups_active":       rows["groups_active"] or 0,
        "sync_configured":     rows["sync_configured"] or 0,
        "volume_files":        vol_files or 0,
        "chapter_files":       ch_files or 0,
        "languages":           sorted(langs),
        "kavita":              int(bool(settings.get("kavita_url") and settings.get("kavita_api_key"))),
        "webhook":             int(bool(settings.get("webhook_url"))),
    }


def collect_and_send() -> None:
    if not _ENDPOINT:
        return
    try:
        import db as _db
        from sync_config import load_settings, save_settings

        env_disabled = os.environ.get("TELEMETRY", "true").lower() in ("false", "0", "no")

        conn = _db.init_db()
        try:
            settings = load_settings()
            if env_disabled:
                if settings.get("telemetry_enabled", True):
                    save_settings({"telemetry_enabled": False})
                return
            if not settings.get("telemetry_enabled", True):
                return

            stats  = _gather_stats(conn, settings)
            usage  = _db.get_usage(conn)
        finally:
            conn.close()

        timings = usage.get("job_timings") or []
        by_type: dict = {}
        for t in timings:
            by_type.setdefault(t["type"], []).append(t["duration_s"])
        timing_stats = {
            jtype: {"p50": _percentile(durs, 50), "p95": _percentile(durs, 95), "n": len(durs)}
            for jtype, durs in by_type.items()
        }

        payload = json.dumps({
            "instance_id": _instance_id(),
            "version":     APP_VERSION,
            "platform":    os.environ.get("PLATFORM", ""),
            "series_bucket": _bucket(stats["total"]),
            # kept for backward compat with existing D1 columns:
            "languages":   stats["languages"],
            "webhook":     stats["webhook"],
            "merge_volumes": 0,
            # new:
            "stats":       stats,
            "usage":       usage,
            "job_timings": timing_stats,
        }).encode()

        req = urlrequest.Request(
            _ENDPOINT,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": f"manga-sync/{APP_VERSION}"},
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=5):
            pass
    except Exception:
        pass
