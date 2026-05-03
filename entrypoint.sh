#!/bin/sh
set -e

MANGA_ROOT="${MANGA_ROOT:-/manga}"
DATA_DIR="${DATA_DIR:-/data}"
CONFIG_PATH="${CONFIG_PATH:-${DATA_DIR}/config/settings.json}"
SYNC_LOG="${SYNC_LOG:-${DATA_DIR}/logs/sync.log}"
PUID="${PUID:-0}"
PGID="${PGID:-0}"

mkdir -p "${DATA_DIR}/config" "${DATA_DIR}/logs"

# Set up unprivileged user if PUID/PGID requested
if [ "$PUID" != "0" ] || [ "$PGID" != "0" ]; then
    addgroup -g "${PGID}" appgroup 2>/dev/null || true
    adduser -D -H -u "${PUID}" -G appgroup appuser 2>/dev/null || true
    chown -R "${PUID}:${PGID}" "${DATA_DIR}" 2>/dev/null || true
    # Ensure manga root is owned by the target user so downloads land correctly
    chown "${PUID}:${PGID}" "${MANGA_ROOT}" 2>/dev/null || true
    RUNAS="su-exec ${PUID}:${PGID}"
else
    RUNAS=""
fi

if ! $RUNAS touch "$MANGA_ROOT/.write_test" 2>/dev/null; then
    echo "ERROR: $MANGA_ROOT is not writable as uid=${PUID} gid=${PGID} — check volume permissions." >&2
    exit 1
fi
$RUNAS rm -f "$MANGA_ROOT/.write_test"

CRON="${SYNC_CRON:-0 */6 * * *}"
echo "$CRON $RUNAS python3 /app/manga-sync.py" > /tmp/crontab
echo "manga-sync starting — schedule: $CRON | root: $MANGA_ROOT | data: $DATA_DIR | uid=${PUID} gid=${PGID}"

$RUNAS uvicorn web.app:app --app-dir /app --host 0.0.0.0 --port 4649 --log-level warning &

exec supercronic /tmp/crontab
