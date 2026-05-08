#!/usr/bin/env python3
"""Cron hook: enqueue a global sync job instead of running sync directly."""
import os

import db


def main() -> int:
    data_dir = os.environ.get("DATA_DIR", "/data")
    conn = db.init_db(data_dir)
    stored = db.read_stored_settings(conn)
    roots = [rf for rf in (stored.get("root_folders") or []) if rf is not None]
    if not roots:
        print("[cron] no root folders configured; skipping sync_all enqueue")
        conn.close()
        return 0
    # Avoid stacking duplicate global sync jobs.
    exists = conn.execute(
        """
        SELECT id FROM jobs
        WHERE job_type='sync_all' AND status IN ('queued', 'running')
        ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    if exists:
        print(f"[cron] sync_all already active (job #{exists['id']}); skipping enqueue")
        conn.close()
        return 0
    job_id = db.enqueue_job(
        conn,
        job_type="sync_all",
        queue_key="default",
        payload={},
    )
    print(f"[cron] enqueued sync_all job #{job_id}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

