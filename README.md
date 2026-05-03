# mangadex-kavita-sync

A containerised toolset that keeps a [Kavita](https://www.kavitareader.com/) manga library in sync with [MangaDex](https://mangadex.org/). Runs on a configurable schedule alongside your existing stack — no cron jobs, no manual downloads, no SSH.

---

## Features

**`manga-sync`** — automatic chapter downloader
- Polls the MangaDex API for new chapters on a cron schedule
- Filters by translator/scanlation group per series
- Downloads chapters as CBZ via the [`mdx`](https://github.com/arimatakao/mdx) CLI
- Automatically adds volume labels (`vol. N ch. N`) once a volume is complete on disk
- Skips back-catalogue on first run via a configurable `since` chapter

**`manga-fix`** — Kavita filename fixer
- Fixes filenames that confuse Kavita's parser: malformed brackets, empty volume tags, front-loaded group tags
- Deduplicates files left behind by download managers (`name (1).cbz`, `name (2).cbz`)
- Runs automatically after every sync pass; also available interactively

---

## Quick start

### 1. Pull the image

```bash
docker pull ghcr.io/pullaf/mangadex-kavita-sync:latest
```

### 2. Add a `.mangadex.json` to each series you want to track

Place this file inside the series directory (e.g. `manga/Isekai Ojisan/`):

```json
{
  "id":         "c9de2a46-2b2e-4a38-bdb4-bf0cbb967318",
  "language":   "en",
  "translator": "Kirei Cake",
  "since":      50
}
```

| Field | Required | Description |
|---|---|---|
| `id` | Yes | MangaDex title UUID (from the series URL) |
| `language` | No | Language code. Defaults to `DEFAULT_LANGUAGE` env var (`en`) |
| `translator` | No | Scanlation group name filter. Omit to accept any group |
| `since` | No | Skip chapters at or below this number — useful to avoid downloading an existing back-catalogue |

Series directories without a `.mangadex.json` are silently skipped.

### 3. Add to your compose stack

```yaml
services:
  manga-sync:
    image: ghcr.io/pullaf/mangadex-kavita-sync:latest
    environment:
      SYNC_CRON: "0 */6 * * *"   # every 6 hours — standard cron syntax
      DEFAULT_LANGUAGE: "en"
      MANGA_ROOT: "/manga"
    volumes:
      - /path/to/your/manga:/manga
      - /path/to/logs:/logs      # optional — omit if you don't need persistent logs
    restart: unless-stopped
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `SYNC_CRON` | `0 */6 * * *` | Cron expression controlling how often the sync runs |
| `DEFAULT_LANGUAGE` | `en` | Fallback language when a series config omits the `language` field |
| `SYNC_LOG` | `/logs/.sync.log` | Log file path. Mount a volume at `/logs` to persist it |

---

## Library layout

The container expects your library mounted at `MANGA_ROOT` (`/manga` by default). Any folder structure that Kavita accepts will work — the sync tool recursively searches for `.mangadex.json` files at any depth. Flat layout, language-split, genre-split, whatever you have:

```
manga/
  Isekai Ojisan/              ← flat: series directly under root
    .mangadex.json
    Isekai Ojisan vol. 1 ch. 1.cbz

  en/                         ← or grouped however you like
    Isekai Ojisan/
      .mangadex.json
      Isekai Ojisan vol. 1 ch. 1.cbz
  jp/
    Isekai Ojisan/
      .mangadex.json
      ...
```

Only directories that contain a `.mangadex.json` are synced — everything else is ignored.

---

## Volume completion

When all chapters belonging to a volume are present on disk, the sync script automatically renames untagged files from:

```
Series ch. 45.cbz
```
to:
```
Series vol. 5 ch. 45.cbz
```

Volume membership is determined by querying the MangaDex API, so it only triggers when the API reports a complete volume.

---

## Running manga-fix manually

To run the filename fixer interactively against your library (outside of the automatic sync pass):

```bash
# Interactive — walk through each issue
docker run --rm -it \
  -e MANGA_ROOT=/manga \
  -v /path/to/your/manga:/manga \
  --entrypoint python3 \
  ghcr.io/pullaf/mangadex-kavita-sync:latest \
  /app/manga-fix.py

# Auto-fix everything without prompts
docker run --rm \
  -e MANGA_ROOT=/manga \
  -v /path/to/your/manga:/manga \
  --entrypoint python3 \
  ghcr.io/pullaf/mangadex-kavita-sync:latest \
  /app/manga-fix.py --yes
```

---

## Testing

Run a one-off sync against your library to verify everything is working before committing to the schedule:

```bash
docker run --rm \
  -e MANGA_ROOT=/manga \
  -v /path/to/your/manga:/manga \
  --entrypoint python3 \
  ghcr.io/pullaf/mangadex-kavita-sync:latest \
  /app/manga-sync.py
```

---

## Updating

```bash
docker compose pull manga-sync && docker compose up -d manga-sync
```
