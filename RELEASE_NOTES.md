## Manga Maid 2.3

This release adds language preferences for series titles, cover art and
filenames. A series can now be titled in English while keeping its Japanese
covers, or filed under a romanized name to keep non-Latin scripts off the
filesystem. Each preference is independent, set globally and overridable per
series.

### Language preferences

- **Series title** decides what Kavita shows and what is written to ComicInfo
  `Series`. MangaDex keeps translated titles in `altTitles`, which Manga Maid
  previously ignored, so titles often defaulted to a romanization.
- **Volume cover art** decides which edition's covers are embedded. Volumes with
  no cover in the chosen language keep the original and switch over on their own
  if one is uploaded later.
- **Title used in filenames** substitutes `%3` in the chapter and volume
  templates. Setting it to **Romanized** uses the `ja-ro` / `ko-ro` titles
  MangaDex publishes, so filenames stay Latin while Kavita shows the native
  title.

Each is set in Settings and can be overridden on any series. The per-series
pickers list the actual title in every available language, and the cover picker
shows how many volumes each language covers, so a fallback is visible before it
happens rather than after.

Suwayomi series linked to a MangaDex companion take their title languages and
cover art from the companion. Without one they resolve to their single
Suwayomi title.

### Applying a language to an existing library

Choosing a language renames existing chapter and volume files on the next sync
or Fix Files run, and rewrites ComicInfo `Series`. Only the title changes:
scanlation group and numbering are left alone, and filenames that carry no
recognisable title are skipped rather than rebuilt from the template. Renames
are recorded in the rename log.

Cover art is treated more carefully than filenames, because a replaced image
cannot be recovered. Manga Maid only rewrites covers inside volumes it built
itself. Files that were already in the folder are never modified, and still get
the correct cover in Kavita. A per-series option allows rewriting those too.

### Fixed

- Volume cover selection was non-deterministic. When MangaDex had covers in
  several languages, whichever the API happened to return last won, and the
  choice could change between runs.
- A volume merged before its cover art was published kept the first page of its
  first chapter as the cover permanently. Real cover art now replaces that
  placeholder as soon as it appears, with no configuration needed.
- Kavita cover pushing looked series up by folder name, which fails once
  ComicInfo `Series` and the folder differ. It now tries the resolved title
  first.

### Settings page

- A sticky bar appears when there are unsaved changes, with Save and Discard,
  so the save button is reachable without scrolling to the bottom.
- Saving a language preference confirms that titles have updated and that a
  sync or Fix Files run is needed to apply it to files.

### Upgrade notes

Upgrading changes nothing on its own. All three preferences start unset, which
keeps every series titled exactly as it is today and leaves files untouched.
Nothing is renamed or re-covered until a language is chosen.

Two things to be aware of once you do choose one:

- Volumes merged by earlier versions are not recorded as built by Manga Maid, so
  their covers are treated as yours and left alone. Choosing a cover language for
  the series allows them to be rewritten.
- Volumes that fell back to a chapter page as their cover under an earlier
  version cannot be told apart from deliberate covers, so they are not replaced
  automatically. Setting a cover language updates them.

---

**Docker image:** `ghcr.io/pullaf/manga-maid:2.3`

**Full changelog:** https://github.com/pullaf/manga-maid/compare/v2.2.1...v2.3
