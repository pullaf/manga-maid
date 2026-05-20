#!/bin/sh
set -e

MANGA_ROOT="${MANGA_ROOT:-/manga}"
DATA_DIR="${DATA_DIR:-/data}"
CONFIG_PATH="${CONFIG_PATH:-${DATA_DIR}/config/settings.json}"
SYNC_LOG="${SYNC_LOG:-${DATA_DIR}/logs/sync.log}"
PUID="${PUID:-0}"
PGID="${PGID:-0}"

echo "manga-maid starting — root: $MANGA_ROOT | data: $DATA_DIR | uid=${PUID} gid=${PGID}"

mkdir -p "${DATA_DIR}/config" "${DATA_DIR}/logs"

# Set up unprivileged user if PUID/PGID requested
if [ "$PUID" != "0" ] || [ "$PGID" != "0" ]; then
    addgroup -g "${PGID}" appgroup 2>/dev/null || true
    adduser -D -H -u "${PUID}" -G appgroup appuser 2>/dev/null || true
    chown -R "${PUID}:${PGID}" "${DATA_DIR}" 2>/dev/null || true
    RUNAS="su-exec ${PUID}:${PGID}"
else
    RUNAS=""
fi
export CRON_RUNAS="$RUNAS"

if ! $RUNAS touch "$MANGA_ROOT/.write_test" 2>/dev/null; then
    echo "ERROR: $MANGA_ROOT is not writable as uid=${PUID} gid=${PGID} — check volume permissions." | tee /dev/stderr
    exit 1
fi
$RUNAS rm -f "$MANGA_ROOT/.write_test"

SCHED="$($RUNAS python3 - <<'PY'
import sys
sys.path.insert(0, "/app")
from sync_config import load_settings, sanitize_sync_cron, is_sync_cron_disabled
expr = sanitize_sync_cron(load_settings().get("sync_cron"))
print("__disabled__" if is_sync_cron_disabled(expr) else expr)
PY
)"
if [ "$SCHED" = "__disabled__" ]; then
  echo "# Auto-sync disabled — enqueue sync from the Jobs page." > /tmp/crontab
else
  echo "$SCHED $RUNAS python3 /app/cron_enqueue_sync.py" > /tmp/crontab
fi
if [ "$PUID" != "0" ] || [ "$PGID" != "0" ]; then
    chown "${PUID}:${PGID}" /tmp/crontab 2>/dev/null || true
fi
chmod 664 /tmp/crontab 2>/dev/null || true
if [ "$SCHED" = "__disabled__" ]; then
  echo "manga-maid sync schedule: disabled (manual only)"
else
  echo "manga-maid sync schedule: $SCHED"
fi

TRUSTED_PROXY_IPS="${TRUSTED_PROXY_IPS:-*}"
$RUNAS uvicorn web.app:app --app-dir /app --host 0.0.0.0 --port 4649 \
  --log-level warning \
  --proxy-headers \
  --forwarded-allow-ips "$TRUSTED_PROXY_IPS" &

exec supercronic -inotify /tmp/crontab
