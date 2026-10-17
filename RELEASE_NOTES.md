## Manga Maid 2.4

This release is about failing loudly. Sync problems now show up where you will
see them, can be sent to your webhook, and come with advice that matches the
actual problem.

### Sync problem notifications

- Notifications in Settings now has separate toggles for **New chapters** and
  **Sync problems**, both on by default.
- Sync problems are sent when a series starts failing and again when it
  recovers. A series that keeps failing is reported once, not on every sync.

### Clearer sync errors

- The dashboard shows how many series are failing; click it to show only those.
- Error banners can be dismissed.
- "How do I fix this?" now matches the actual problem (network, failed
  downloads, MangaDex or Suwayomi) instead of always pointing at Suwayomi (#21).

### Fixes

- Changing the sync schedule in Settings now applies immediately instead of
  after a restart. Invalid schedules are rejected with a warning.
- Chapters that fail to download now mark the series as failed instead of
  appearing only in the job log.
- A stalled download no longer blocks the job queue indefinitely.
- Renaming a series onto an existing folder is refused instead of nesting it.
- MangaDex volume numbers were missing for series with a volume containing only
  chapter 0.
- Stopping the container now stops a running sync cleanly and resumes it on the
  next start, and the container exits with an error if the web server crashes.

### Upgrade notes

- Series that are already failing are not reported until their state changes.
  The dashboard's failing count shows them in the meantime.
- The unused `SYNC_CRON` variable was removed from the example compose file.
  The schedule is set in Settings.

---

**Docker image:** `ghcr.io/pullaf/manga-maid:2.4`

**Full changelog:** https://github.com/pullaf/manga-maid/compare/v2.3...v2.4
