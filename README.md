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

Place this file inside the series directory (e.g. `manga/en/Yotsuba&!/`):

```json
{
  "id":         "5e3a710f-0b0d-482b-9e84-d9c91960c625",
  "language":   "en",
  "translator": "Sho Habby Scans",
  "since":      207
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
    restart: unless-stopped
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `SYNC_CRON` | `0 */6 * * *` | Cron expression controlling how often the sync runs |
| `MANGA_ROOT` | `/manga` | Path inside the container where the library is mounted |
| `DEFAULT_LANGUAGE` | `en` | Fallback language when a series config omits the `language` field |

---

## Library layout

The container expects your library mounted at `MANGA_ROOT` (`/manga` by default), organised like this:

```
manga/
  en/<Series Title>/
    .mangadex.json          ← sync config for this series
    Series vol. 1 ch. 1.cbz
    Series vol. 1 ch. 2.cbz
  jp/<Series Title>/
    .mangadex.json
    ...
```

Language subdirectories (`en/`, `jp/`, etc.) are discovered automatically.

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
