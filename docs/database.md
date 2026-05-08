# Database Reference

This document describes the SQLite schema used by the app, how each table is used, and which code paths write/update data.

Primary implementation: `db.py`  
Default location: `/data/db/manga-sync.db`

## Runtime and lifecycle

- Engine: SQLite with WAL mode (`PRAGMA journal_mode=WAL`) and foreign keys enabled.
- Schema creation: `init_db()` executes full schema + additive migrations.
- Migrations are additive and idempotent (`ensure_schema()`).
- Settings are stored in DB (`app_config.settings_json`), not in standalone JSON.

## High-level data model

- `series` = one tracked library folder.
- `series_sources` = linked external source(s) for a series (currently MangaDex).
- `series_metadata` = cached source metadata (title, tags, author, status, volume count, etc.).
- `chapters` = canonical chapter records keyed by `(series_id, chapter_num)`.
- `volumes` = canonical volume records keyed by `(series_id, volume_num)`.
- `rename_log` = audit trail of rename/delete operations.
- `jobs` + `job_logs` = durable queue and line-based logs for background jobs.
- `app_config` = singleton settings blob used by web UI and sync.

## Table-by-table

### `series`

Purpose:
- Core tracked series row, keyed by unique relative folder `path`.

Important columns:
- `path` (UNIQUE): relative path under `MANGA_ROOT`.
- `language`: default language for sync.
- `preferred_group` / `preferred_groups_json`: scanlator priority.
- `since`: chapter threshold for initial back-catalog skip.
- `exclude_from_fix`: opt-out from fix/rename suggestions.
- `merge_volumes_override`: per-series override for merge behavior.

Written/updated by:
- `insert_series()`, `update_series()`, `delete_series()`, `unlink_series()`.
- `migrate_json_configs()` (legacy import path).
- `scan_disk_series()` (auto-add unlinked series found on disk).

### `series_sources`

Purpose:
- Source linkage for each series (for sync and metadata refresh).

Important columns:
- `source`, `source_id`, `priority`, `last_synced_at`.
- UNIQUE(`series_id`, `source`).

Written/updated by:
- `insert_series()`, `update_series()`, `unlink_series()`.
- `migrate_json_configs()` (legacy import path).
- `update_source_sync_time()` after successful sync.

### `series_metadata`

Purpose:
- Cached metadata fetched from the linked source.

Important columns:
- `title`, `description`, `tags`, `authors`, `artists`, `year`, `status`,
  `content_rating`, `total_volumes`, `cover_filename`, `fetched_at`.
- PK(`series_id`, `source`).

Written/updated by:
- `upsert_series_metadata()` from sync/web metadata fetch routines.
- `insert_series()` / `update_series()` for cover bootstrap data.
- `migrate_json_configs()` when importing existing metadata fields.

Read paths:
- `get_all_series()`, `get_series_by_path()`, `get_series_metadata()`.

### `volumes`

Purpose:
- Canonical volume rows and on-disk volume archive state.

Important columns:
- `volume_num` (REAL), `path` (archive file if present), `has_comicinfo`,
  `file_size`, `last_seen`, `cover_url`.
- UNIQUE(`series_id`, `volume_num`).

Written/updated by:
- `upsert_volume()` from feed sync / scans.
- `mark_volume_merged()` when volume archive is created.
- `mark_volume_comicinfo()` after ComicInfo write.
- `scan_disk_files()` (path/file_size/has_comicinfo reconciliation).

### `chapters`

Purpose:
- Canonical chapter rows from source feed and local file tracking.

Important columns:
- `chapter_num` (REAL), `source_chapter_id`, `title`, `group_name`,
  `publish_date`, `path`, `status` (`known`/`downloaded`), `has_comicinfo`.
- UNIQUE(`series_id`, `chapter_num`).

Written/updated by:
- `upsert_chapter()` during source feed sync.
- `assign_chapter_to_volume()`.
- `mark_chapter_downloaded()` after successful file placement.
- `mark_chapter_comicinfo()` after ComicInfo write.
- `scan_disk_files()` (authoritative disk reconciliation for `path/status`).

### `rename_log`

Purpose:
- Audit log for file rename/delete operations.

Important columns:
- `old_path`, `new_path`, `action`, `reason`, `timestamp`, optional FK refs.

Written by:
- `log_rename()` from fix/delete paths in web/sync flows.

### `jobs`

Purpose:
- Durable queue for background work.

Important columns:
- `job_type`, `queue_key`, `status` (`queued`/`running`/terminal states),
  `series_id`, `series_path_snapshot`, `payload_json`,
  `created_at`, `started_at`, `ended_at`, `exit_code`, `error_summary`.

Indexes:
- `idx_jobs_status_queue_created` on (`status`, `queue_key`, `created_at`).
- `idx_jobs_series_status` on (`series_id`, `status`).

Written/updated by:
- `enqueue_job()` creates queued jobs.
- `claim_next_queued_job()` transitions queued -> running.
- `finish_job()` marks completed/failed terminal states.
- `cancel_queued_job()` and `mark_job_cancelled()` for cancellations.
- `requeue_running_jobs()` recovers interrupted running jobs on restart.
- `cleanup_old_jobs()` removes old terminal jobs.

### `job_logs`

Purpose:
- Append-only per-job output stream with sequence numbers.

Important columns:
- `job_id`, `seq`, `line`, `ts`.
- UNIQUE(`job_id`, `seq`) to enforce ordering consistency.

Index:
- `idx_job_logs_job_seq` on (`job_id`, `seq`).

Written/updated by:
- `append_job_log()` for each emitted log line.
- Read via `get_job_logs_since()` for SSE replay/streaming.

### `app_config`

Purpose:
- Singleton key-value blob (JSON) for app settings.

Structure:
- One fixed row with `id = 1`, column `settings_json`.

Written/updated by:
- `write_stored_settings()`.
- initialized/ensured by `_ensure_app_config_row()`.
- one-time legacy import handled by `_migrate_legacy_settings_json()`.

Read via:
- `read_stored_settings()`.

## Reconciliation and source of truth rules

Disk reconciliation (`scan_disk_files`) is the authoritative path/file status sync:

- If a manga archive exists on disk and matches chapter/volume pattern:
  - DB row path/file_size/status are updated.
- If a DB `path` no longer exists:
  - `path` is nulled and status adjusted back to `known`.
- `has_comicinfo` is reset when path state changes, so ComicInfo can be re-applied.

This is why periodic/startup reconcile jobs are useful: they prevent DB drift
when files are added/renamed/deleted outside sync flows.

## Jobs retention

- Terminal jobs (`completed`, `failed`, `cancelled`) are cleaned by
  `cleanup_old_jobs(keep_days=30)` in worker loops.
- Active/queued jobs are never removed by retention cleanup.

## Common read patterns

- Dashboard/list views: `get_all_series()`.
- Series detail view: `get_series_by_path()`, then `get_chapters()`/`get_volumes()`.
- Download candidates: `get_chapters_to_download()` where `status='known'` and `path IS NULL`.
- Job screens: `list_jobs()`, `get_active_jobs()`, `get_job_logs_since()`.

## Notes for future schema changes

- Keep migrations additive in `ensure_schema()`.
- Preserve idempotency (safe on repeated startup).
- Prefer storing structured settings in `app_config.settings_json` unless query
  performance requires a dedicated table/column.
- If adding new job types, document `payload_json` shape and terminal behavior.
