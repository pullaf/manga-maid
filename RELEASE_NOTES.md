## Manga Maid 2.2.0

This release improves MangaDex companion integration for series downloaded
through Suwayomi and fixes missing cover art on the dashboard.

### Fixed

- Dashboard covers now prefer the series' native Suwayomi thumbnail.
- If the Suwayomi thumbnail is unavailable, the dashboard falls back to the
  linked MangaDex companion cover.
- Cover resolution now uses the companion MangaDex UUID instead of mistakenly
  sending a Suwayomi manga ID to MangaDex.
- Resolved MangaDex companion cover metadata is retained for later requests.

### Improved

- Normal Suwayomi syncs now refresh chapter-to-volume mappings from the linked
  MangaDex companion.
- Companion mappings follow the existing volume-remap schedule: immediately
  when chapters are waiting to download, and periodically while on-disk
  chapters remain unmapped.
- Fix Files can consequently include `vol.N` in existing chapter filenames
  after a normal sync, provided the chapter naming pattern contains `%4` and
  MangaDex assigns the chapter to a volume.
- Series details and chapter-gap checks now share the same companion-aware
  MangaDex ID and aggregate-fetching logic used by sync and cover handling.

### Upgrade notes

After upgrading, run a normal sync and then open **Fix Files** to update
existing chapter filenames. Chapters that MangaDex reports without a volume
remain unchanged.

---

**Docker image:** `ghcr.io/pullaf/manga-maid:2.2.0`

**Full changelog:** https://github.com/pullaf/manga-maid/compare/v2.1.7...v2.2.0
