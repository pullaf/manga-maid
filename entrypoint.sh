#!/bin/sh
set -e

CRON="${SYNC_CRON:-0 */6 * * *}"
echo "$CRON python3 /app/manga-sync.py" > /tmp/crontab
echo "manga-sync starting — schedule: $CRON | language: ${DEFAULT_LANGUAGE:-en} | root: ${MANGA_ROOT:-/manga}"
exec supercronic /tmp/crontab
