## What's changed

### Fix: no visible startup logs

`docker logs` showed only supercronic crontab messages with nothing from
the web server, making it impossible to tell whether the app had started
successfully or was still initialising.

**v2.1.2 adds:**
- A `[startup] ready` line printed once the web server is fully up and
  accepting connections — no more guessing whether it started

If you upgraded to v2.1.1 and everything is working, this is a quality-of-life
fix only. If you are still seeing no UI after upgrading to v2.1.1, the ready
line will confirm whether the server started or is still initialising.

---

**Docker image:** `ghcr.io/pullaf/manga-maid:2.1.2`

**Full changelog:** https://github.com/pullaf/manga-maid/compare/v2.1.1...v2.1.2
