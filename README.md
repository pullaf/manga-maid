# mangadex-kavita-sync

A containerised toolset that keeps a [Kavita](https://www.kavitareader.com/) manga library in sync with [MangaDex](https://mangadex.org/). Runs on a configurable schedule alongside your existing stack via built-in cron queueing, no manual downloads, no SSH.

Includes a **web UI** (port `4649`) for managing series, triggering syncs, browsing logs, and configuring Kavita integration.

---

## Features

**manga-sync** - automatic chapter downloader
- Polls the MangaDex API for new chapters on a schedule configured in the web UI
- Filters by translator/scanlation group per series
- Downloads chapters as CBZ (or ZIP/CBR) via the [`mdx`](https://github.com/arimatakao/mdx) CLI
- Supports volume mode - one file per volume instead of per chapter
- Automatically adds volume labels (`vol.N ch.N`) once a volume is complete on disk
- Skips back-catalogue on first run via a configurable `since` chapter
- Configurable delay between downloads to be polite to the API

**manga-fix** - Kavita filename fixer
- Fixes filenames that confuse Kavita's parser: malformed brackets, empty volume tags, front-loaded group tags
- Deduplicates files left behind by download managers (`name (1).cbz`, `name (2).cbz`)
- Runs automatically after every sync pass; also available through the web UI

**Kavita integration**
- Auto-trigger a Kavita library scan after every sync
- Fetch volume cover art from MangaDex and push it to Kavita automatically
- Configurable via the web UI - just enter your Kavita URL and API key

**Web UI** - dark-themed browser interface at port `4649`
- Dashboard showing all tracked series with edit/remove controls
- Add Series: search MangaDex by title or paste a URL, pick language and scanlation group
- Sync page with live streaming output
- Jobs page with durable queued/running/completed jobs and per-job logs
- Fix Files page for interactive filename repair
- Logs page showing sync history and rename/delete audit trail
- Settings page for download format, file naming, sync schedule presets/custom cron, Kavita integration

---

## Quick start

### 1. Pull the image

```bash
docker pull ghcr.io/pullaf/mangadex-kavita-sync:latest
```

### 2. Add to your compose stack

```yaml
services:
  manga-sync:
    image: ghcr.io/pullaf/mangadex-kavita-sync:latest
    ports:
      - "4649:4649"          # web UI
    environment:
      PUID: 1000             # run as your user - match your host UID
      PGID: 1000             # match your host GID
      TZ: "America/New_York" # your timezone
      DEFAULT_LANGUAGE: "en" # preferred chapter language
    volumes:
      - /path/to/your/manga:/manga
      - /path/to/data:/data  # config + logs
    restart: unless-stopped
```

Then open `http://your-host:4649`, add series, and configure the sync schedule in **Settings**.

## Volumes

| Mount | Purpose |
|---|---|
| `/manga` | Your manga library root. |
| `/data` | Persistent config, SQLite catalog, and logs. Web/sync settings live in the DB (`app_config`). |

```
/data/
  db/
    manga-sync.db   ← series/chapters/volumes + app_config (web UI settings blob)
  logs/
    sync.log        ← rolling sync log, trimmed to 5000 lines
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `PUID` | `0` (root) | UID to run as. Set to your host user's UID so downloaded files are owned correctly. |
| `PGID` | `0` (root) | GID to run as. |
| `TZ` | — | Timezone for schedule display (e.g. `Europe/London`, `Asia/Tokyo`). |
| `DEFAULT_LANGUAGE` | `en` | Preferred chapter language code used when adding new series (e.g. `it`, `fr`, `ja-ro`). |
| `DATA_DIR` | `/data` | Base path for config and logs. |
| `SYNC_LOG` | `$DATA_DIR/logs/sync.log` | Override log file location. |
| `TELEMETRY` | `true` | Set to `false` to opt out of anonymous usage statistics. Can also be toggled in Settings → Privacy. |

> Sync schedule is now persisted in app settings (SQLite) and edited from the web UI.
> The container reads that value on boot and live-reloads cron when you change it in Settings.

## Sync schedule

Set this in **Settings → Auto-sync schedule**:

- **Presets:** Hourly, Every 6 hours, Every 12 hours, Daily, Weekly
- **Custom:** manual 5-field cron expression (`minute hour day month weekday`)

Examples:
- `0 * * * *` hourly
- `0 */6 * * *` every 6 hours
- `0 3 * * *` daily at 03:00

## Background jobs and queue

Sync operations run as durable background jobs:

- Single FIFO queue (one active job at a time)
- Jobs survive page navigation/reload
- Live logs stream while running, with replay from persisted history
- History retention for recent completed jobs
- Cancel queued/running jobs from the UI

This includes scheduled syncs, which are enqueued by cron into the same queue.

### Finding your PUID/PGID

```bash
id $USER
# uid=1000(yourname) gid=1000(yourname) ...
```

---

## Library layout

The container expects your library at `MANGA_ROOT`. Any folder structure Kavita accepts works - flat, language-split, genre-split, etc.:

```
manga/
  Isekai Ojisan/              ← flat layout
    Isekai Ojisan vol.1 ch.1.cbz

  en/                         ← language-split layout
    Isekai Ojisan/
      ...
  jp/
    ...
```

---

## File naming

Downloaded filenames use format codes from the `mdx` CLI. Configure them in Settings:

| Code | Meaning |
|---|---|
| `%1` | Language |
| `%2` | Scanlation group |
| `%3` | Title |
| `%4` | Volume number |
| `%5` | Chapter number |
| `%6` | Chapter title |

Default chapter pattern: `[%1 %2] %3 vol.%4 ch.%5`  
Default volume pattern: `[%1 %2] %3 vol.%4`

---

## Chapter-first workflow

The current UI is tuned for chapter-based storage. Downloads are saved as chapter files, with volume numbers added to filenames when source metadata is available.

---

## Volume completion (chapter mode)

When all chapters belonging to a volume are present on disk, the sync script automatically renames untagged files from:

```
Series ch.45.cbz
```
to:
```
Series vol.5 ch.45.cbz
```

Volume membership is determined by querying the MangaDex API, so it only triggers when the API reports a complete set.

---

## Kavita integration

In the Settings page:
1. Enter your Kavita server URL (e.g. `http://kavita:5000`)
2. Enter your API key (Kavita → User Settings → 3rd Party Clients)
3. Enable **Auto library scan** to trigger a Kavita scan after every sync
4. Enable **Auto set covers** to push MangaDex volume cover art to Kavita

---

## Running manga-fix manually

```bash
# Interactive - walk through each issue
docker exec -it manga-sync python3 /app/manga-fix.py

# Auto-fix everything without prompts
docker exec manga-sync python3 /app/manga-fix.py --yes
```

---

## Updating

```bash
docker compose pull manga-sync && docker compose up -d manga-sync
```
