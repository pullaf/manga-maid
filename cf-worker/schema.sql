CREATE TABLE IF NOT EXISTS instances (
    instance_id   TEXT PRIMARY KEY,
    version       TEXT,
    platform      TEXT,     -- "unraid" or empty
    series_bucket TEXT,
    languages     TEXT,     -- JSON array, e.g. ["en","ja"]
    webhook       INTEGER,  -- 0 or 1
    merge_volumes INTEGER,  -- 0 or 1
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
