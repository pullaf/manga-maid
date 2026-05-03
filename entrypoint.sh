#!/bin/sh
set -e

MANGA_ROOT="${MANGA_ROOT:-/manga}"
DATA_DIR="${DATA_DIR:-/data}"
CONFIG_PATH="${CONFIG_PATH:-${DATA_DIR}/config/settings.json}"
SYNC_LOG="${SYNC_LOG:-${DATA_DIR}/logs/sync.log}"
mkdir -p "${DATA_DIR}/config" "${DATA_DIR}/logs"

if ! touch "$MANGA_ROOT/.write_test" 2>/dev/null; then
    echo "ERROR: $MANGA_ROOT is not writable — cannot download manga. Check volume permissions." >&2
    exit 1
fi
rm -f "$MANGA_ROOT/.write_test"

CRON="${SYNC_CRON:-0 */6 * * *}"
echo "$CRON python3 /app/manga-sync.py" > /tmp/crontab
echo "manga-sync starting — schedule: $CRON | root: $MANGA_ROOT | data: $DATA_DIR"

uvicorn web.app:app --app-dir /app --host 0.0.0.0 --port 4649 --log-level warning &

exec supercronic /tmp/crontab
