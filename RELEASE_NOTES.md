## What's changed

### Fix: startup hang + drive hammering on NAS/network mounts

v2.1.0 introduced a regression where the disk reconcile ran in a thread-pool
executor with no throttling, hammering network or slow mounts and blocking
the web server from starting (lifespan never yielded if the mount was slow).

**v2.1.1 fixes:**
- Startup no longer scans the library — DB migrations only. The web server is
  ready to accept connections immediately; the reconcile job runs moments later
  as a background task
- Reconcile now sleeps 20ms between each series scan to avoid saturating
  network mounts. Set `RECONCILE_SERIES_SLEEP=0` to disable, or tune the value
  (in seconds) for your setup

---

**Docker image:** `ghcr.io/pullaf/manga-maid:2.1.1`

**Full changelog:** https://github.com/pullaf/manga-maid/compare/v2.1.0...v2.1.1
