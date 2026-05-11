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


def _bucket(n: int) -> str:
    if n <= 5:   return "1-5"
    if n <= 20:  return "6-20"
    if n <= 50:  return "21-50"
    if n <= 100: return "51-100"
    return "100+"


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
            series_count = conn.execute("SELECT COUNT(*) FROM series").fetchone()[0]
            langs = [
                r[0] for r in conn.execute(
                    "SELECT DISTINCT language FROM series WHERE language IS NOT NULL"
                ).fetchall()
            ]
        finally:
            conn.close()

        payload = json.dumps({
            "instance_id":   _instance_id(),
            "version":       APP_VERSION,
            "platform":      os.environ.get("PLATFORM", ""),
            "series_bucket": _bucket(series_count),
            "languages":     sorted(langs),
            "webhook":       int(bool(settings.get("webhook_url", ""))),
            "merge_volumes": int(bool(settings.get("merge_volumes", False))),
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
