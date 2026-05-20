## What's changed

### Stability — SQLite connection rework

Users with large libraries were seeing `database is locked` and `another row available` errors that caused sync jobs to fail or the job queue to stall. This release fixes the root cause: the web server previously shared a single SQLite connection across all HTTP requests and background workers. Any error in one place could corrupt state for everything else.

**v2.1.0 changes:**
- Every HTTP request now gets its own independent SQLite connection — errors in one request cannot affect others
- The job worker has its own dedicated connection, completely isolated from the web UI
- Disk reconcile runs in a thread-pool executor instead of blocking the async event loop — the web UI stays responsive during library scans (previously it could freeze for minutes on large libraries)
- Fix page scans also run off the event loop
- Executor callbacks no longer share connections across threads

### Performance — batched chapter commits

`manga-sync` previously issued one SQLite commit per downloaded chapter. On a series with 700 chapters this meant 700 write transactions. Commits are now batched (default every 50 chapters, tunable via `SYNC_COMMIT_BATCH` env var), which reduces lock contention when the web server is active during a sync.

### Telemetry — sync duration

The telemetry ping now includes p50/p95 sync durations (anonymised, opt-out as always). This helps us understand real-world performance across library sizes.

### Bug fixes (from v2.0.5, included here)

- `append_job_log` was leaving an SQLite cursor open before `commit()`, triggering "another row available" in Python 3.12 on every log line during a long sync — this was the main driver of the error storms seen in v2.0.x
- The job worker now rolls back on unexpected exceptions instead of leaving the connection in a dirty state

---

**Docker image:** `ghcr.io/pullaf/manga-maid:2.1.0`

**Full changelog:** https://github.com/pullaf/manga-maid/compare/v2.0.4...v2.1.0
