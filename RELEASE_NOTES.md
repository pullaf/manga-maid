## What's changed

### Fix: PUID/PGID containers fail to start (no UI, no logs)

Two issues combined to silently break containers running as a non-root user
(`PUID`/`PGID` set to anything other than 0):

1. **`chown /manga` hung on NAS/NFS/SMB mounts** — the entrypoint tried to
   `chown` the manga root directory to the target user, which blocks
   indefinitely on network-backed volumes (TrueNAS, Synology, etc.)

2. **Database created as root** — the cron-schedule check ran a Python snippet
   as root, which called `load_settings()` and created the SQLite database
   owned by `root`. When uvicorn then started as the target user it couldn't
   write to the database, silently aborting startup.

**v2.1.4 fixes all three:**
- Removed the `chown` on the manga root entirely — the write-permission check
  that follows it is sufficient
- The schedule snippet now runs as the target user (`$RUNAS python3`), so the
  database is created with the correct ownership from the start
- The crontab file is now written to `DATA_DIR` instead of `/tmp` — on
  container runtimes that run the entrypoint as the target user (TrueNAS SCALE,
  k8s with `securityContext.runAsUser`), `/tmp` may not be writable

Users running as root (default, no `PUID`/`PGID` set) are unaffected.

---

**Docker image:** `ghcr.io/pullaf/manga-maid:2.1.4`

**Full changelog:** https://github.com/pullaf/manga-maid/compare/v2.1.2...v2.1.4
