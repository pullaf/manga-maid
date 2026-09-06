# Manga Maid

A containerised toolset that keeps a [Kavita](https://www.kavitareader.com/) manga library in sync with [MangaDex](https://mangadex.org/) and with **200+ sources** via [Suwayomi-Server](https://github.com/Suwayomi/Suwayomi-Server). Runs on a configurable schedule alongside your existing stack via built-in cron queueing, no manual downloads, no SSH.

Source code: [github.com/pullaf/manga-maid](https://github.com/pullaf/manga-maid)

Includes a **web UI** (port `4649`) for managing series, triggering syncs, browsing logs, and configuring all integrations.

<img width="1310" height="1123" alt="image" src="https://github.com/user-attachments/assets/49b97f4b-ed97-480c-8a47-6dd46e92423d" />

---

## Features

**manga-sync** - automatic chapter downloader
- Polls MangaDex **and Suwayomi sources** for new chapters on a schedule configured in the web UI
- Filters by translator/scanlation group per series
- Downloads chapters as CBZ (or ZIP/CBR) via the [`mdx`](https://github.com/arimatakao/mdx) CLI (MangaDex) or direct page proxy (Suwayomi)
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

**Suwayomi integration** *(new in v2.0)*
- Connect any running [Suwayomi-Server](https://github.com/Suwayomi/Suwayomi-Server) instance
- Browse and enable installed Mihon/Tachiyomi extension sources from the **Sources** page
- Enabled sources are included alongside MangaDex in every "Add Series" search
- Suwayomi sources can be used as the download backend for any tracked series
- Optional username/password auth support; connection test built into Settings

**Web UI** - mobile-friendly browser interface at port `4649`
- Dashboard showing all tracked series with source badges, edit/remove controls
- **Add Series**: search MangaDex and all enabled Suwayomi sources simultaneously; results grouped by source
- **Sources page**: browse Suwayomi extensions, toggle them on/off for search and sync
- Sync page with live streaming output
- Jobs page with durable queued/running/completed jobs and per-job logs
- Fix Files page for interactive filename repair
- Logs page showing sync history and rename/delete audit trail
- Settings page for download format, file naming, sync schedule, Kavita integration, and Suwayomi connection

---

## Quick start

### 1. Pull the image

```bash
docker pull ghcr.io/pullaf/manga-maid:latest
```

CI still publishes `ghcr.io/pullaf/mangadex-kavita-sync:*` with the same digests for existing installs only — new setups should use **`manga-maid`**.

### 2. Add to your compose stack

```yaml
services:
  manga-sync:
    image: ghcr.io/pullaf/manga-maid:latest
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

### Optional: add Suwayomi for extra sources

```yaml
services:
  suwayomi:
    image: ghcr.io/suwayomi/suwayomi-server:latest
    ports:
      - "4567:4567"
    volumes:
      - /path/to/suwayomi-data:/home/suwayomi/.local/share/Tachidesk
    restart: unless-stopped

  manga-sync:
    image: ghcr.io/pullaf/manga-maid:latest
    # ... same as above
```

Then in **Settings → Suwayomi Integration** enter `http://suwayomi:4567`, hit **Test**, and go to **Sources** to enable whichever extensions you have installed.

---

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
| `TZ` | - | Timezone for schedule display (e.g. `Europe/London`, `Asia/Tokyo`). |
| `DEFAULT_LANGUAGE` | `en` | Preferred chapter language code used when adding new series (e.g. `it`, `fr`, `ja-ro`). |
| `DATA_DIR` | `/data` | Base path for config and logs. |
| `SYNC_LOG` | `$DATA_DIR/logs/sync.log` | Override log file location. |
| `TELEMETRY` | `true` | Set to `false` to opt out of anonymous usage statistics. Can also be toggled in Settings → Privacy. |
| `TRUSTED_PROXY_IPS` | `*` | Comma-separated IPs (or `*`) whose `X-Forwarded-*` headers are trusted. Relevant when running behind a reverse proxy. Default `*` is fine for home setups; tighten to your proxy's IP in exposed deployments. |

> Sync schedule and all integration settings (Kavita, Suwayomi) are persisted in SQLite and edited from the web UI.

---

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

---

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

> Suwayomi sources use the same naming patterns. `%2` (group) is populated from the scanlator field when available.

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

Volume membership is determined by querying the source API, so it only triggers when the API reports a complete set.

---

## Kavita integration

In the Settings page:
1. Enter your Kavita server URL (e.g. `http://kavita:5000`)
2. Enter your API key (Kavita → User Settings → 3rd Party Clients)
3. Enable **Auto library scan** to trigger a Kavita scan after every sync
4. Enable **Auto set covers** to push MangaDex volume cover art to Kavita

---

## Suwayomi integration

Suwayomi-Server acts as a proxy to Mihon/Tachiyomi extensions, giving access to hundreds of manga sources beyond MangaDex.

1. Run a [Suwayomi-Server](https://github.com/Suwayomi/Suwayomi-Server) instance and install extensions via its own web UI
2. In **Settings → Suwayomi Integration**, enter the Suwayomi URL (e.g. `http://suwayomi:4567`) and hit **Test**
3. Go to **Sources** and toggle on the extensions you want to search and sync from
4. **Add Series** will now show results from MangaDex and all your enabled sources side by side

Suwayomi sources are identified internally as `suwayomi:<source-id>` and are stored per-series in the database, so each series remembers which source it was added from.

---

## Running manga-fix manually

```bash
# Interactive - walk through each issue
docker exec -it manga-sync python3 /app/manga-fix.py

# Auto-fix everything without prompts
docker exec manga-sync python3 /app/manga-fix.py --yes
```

---

## Reverse proxy (nginx / Caddy / Traefik)

Manga Maid speaks plain HTTP/1.1. **Always proxy to `http://`, never `https://`.** Sending TLS traffic to the container directly returns `400 Invalid HTTP request received.` — that is uvicorn rejecting the TLS handshake, not an application error.

### nginx

```nginx
server {
    listen 443 ssl;
    server_name manga.example.com;

    ssl_certificate     /etc/ssl/certs/manga.crt;
    ssl_certificate_key /etc/ssl/private/manga.key;

    location / {
        proxy_pass http://127.0.0.1:4649;   # ← http, not https
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Required for live job-log streaming (Server-Sent Events)
        proxy_buffering      off;
        proxy_read_timeout   3600s;
        proxy_http_version   1.1;
    }
}
```

If nginx and the container are in **different Docker networks** (or nginx is on the host), replace `127.0.0.1:4649` with the container's IP or the Docker service name: `http://manga-sync:4649`.

> **Common mistakes**
> - `proxy_pass https://…` → TLS error (see above)
> - Missing `proxy_buffering off` → sync log stream freezes mid-run
> - Missing `proxy_read_timeout` → long syncs time out after 60 s

---

## Updating

```bash
docker compose pull manga-sync && docker compose up -d manga-sync
```
