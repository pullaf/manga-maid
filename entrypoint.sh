#!/bin/sh
set -e

MANGA_ROOT="${MANGA_ROOT:-/manga}"

if ! touch "$MANGA_ROOT/.write_test" 2>/dev/null; then
    echo "ERROR: $MANGA_ROOT is not writable — cannot download manga. Check volume permissions." >&2
    exit 1
fi
rm -f "$MANGA_ROOT/.write_test"

CRON="${SYNC_CRON:-0 */6 * * *}"
echo "$CRON python3 /app/manga-sync.py" > /tmp/crontab
echo "manga-sync starting — schedule: $CRON | language: ${DEFAULT_LANGUAGE:-en} | root: $MANGA_ROOT"
exec supercronic /tmp/crontab
