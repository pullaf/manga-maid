## Manga Maid 2.2.1

This release makes source failures understandable and recoverable, and expands
volume matching for titles whose MangaDex chapters are incomplete in the
configured language.

### Fixed

- A failed series feed now makes the sync job fail instead of incorrectly
  reporting that the job completed successfully.
- Per-series source errors are saved and shown on both the dashboard and the
  Series Details page until a later successful feed refresh clears them.
- Long Suwayomi server traces are reduced to concise, actionable messages.
- The dashboard no longer falls back to the unhelpful label `Source` when a
  Suwayomi extension name cannot be resolved.

### Source visibility and recovery

- Dashboard cards show the resolved Suwayomi extension name, or a stable
  `Suwayomi source <ID>` fallback while Suwayomi is unavailable.
- Series Details now identifies the primary sync source, its source key, and
  the optional MangaDex companion separately.
- Source errors include a **How do I fix this?** guide explaining how to update
  the extension, re-add the title in Suwayomi, or safely unlink and reconnect
  the source without deleting downloaded files.
- Failed job summaries are now displayed directly in dashboard and details
  sync output.

### Volume mapping

- MangaDex volume matching now prefers mappings from the configured language,
  then fills only missing chapter mappings from MangaDex entries in other
  languages.
- Language-specific mappings are never replaced by the fallback.
- Series Details reports how many downloaded chapters still have no volume
  information after both lookups, without adding noise to Fix Files.
- Chapters absent from MangaDex remain unmapped; Manga Maid does not guess a
  volume number.

### Upgrade notes

Run a normal sync after upgrading. Existing source errors will appear on the
affected series, and newly available volume mappings will be applied before
filename normalization.

---

**Docker image:** `ghcr.io/pullaf/manga-maid:2.2.1`

**Full changelog:** https://github.com/pullaf/manga-maid/compare/v2.2.0...v2.2.1
