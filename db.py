"""Database layer - schema, migration, and CRUD for manga-kavita-sync."""
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta

MANGA_ROOT      = os.environ.get("MANGA_ROOT", "/manga")
DATA_DIR        = os.environ.get("DATA_DIR", "/data")
DB_SUBDIR       = "db"
DB_FILENAME     = "manga-sync.db"
CONFIG_FILENAME = ".mangadex.json"

MANGA_EXTENSIONS = {".cbz", ".cbr", ".zip", ".rar"}
CH_RE  = re.compile(r"ch\.?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
VOL_RE = re.compile(r"\b(?:vol(?:ume)?\.?|v)\s*0*(\d+)\b", re.IGNORECASE)
CH_RANGE_RE = re.compile(
    r"ch\.?\s*\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS series (
    id                       INTEGER PRIMARY KEY,
    title                    TEXT    NOT NULL,
    path                     TEXT    NOT NULL UNIQUE,
    language                 TEXT    NOT NULL DEFAULT 'en',
    preferred_group          TEXT,
    preferred_groups_json    TEXT,
    -- ``start_chapter``: download chapters where chapter_num >= this value.
    -- 0 means "download everything". Replaced the old ``since`` column whose
    -- semantics were "skip chapters <= since" (download where chapter_num >
    -- since) - the new ``>=`` form lets users type the first chapter they
    -- actually want (e.g. 105 for Yotsuba) instead of guessing 104.999...
    start_chapter            REAL    NOT NULL DEFAULT 0,
    exclude_from_fix         INTEGER NOT NULL DEFAULT 0,
    merge_volumes_override   INTEGER,
    sync_configured          INTEGER NOT NULL DEFAULT 1,
    sync_paused              INTEGER NOT NULL DEFAULT 0,
    ignored                  INTEGER NOT NULL DEFAULT 0,
    created_at               TEXT    NOT NULL,
    updated_at               TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS series_sources (
    id             INTEGER PRIMARY KEY,
    series_id      INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    source         TEXT    NOT NULL,
    source_id      TEXT    NOT NULL,
    priority       INTEGER NOT NULL DEFAULT 1,
    last_synced_at TEXT,
    UNIQUE(series_id, source)
);

CREATE TABLE IF NOT EXISTS series_metadata (
    series_id      INTEGER NOT NULL,
    source         TEXT    NOT NULL,
    title          TEXT,
    description    TEXT,
    tags           TEXT,
    authors        TEXT,
    artists        TEXT,
    year           INTEGER,
    status         TEXT,
    content_rating TEXT,
    total_volumes  INTEGER,
    cover_filename TEXT,
    fetched_at     TEXT    NOT NULL,
    PRIMARY KEY(series_id, source)
);

CREATE TABLE IF NOT EXISTS volumes (
    id            INTEGER PRIMARY KEY,
    series_id     INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    volume_num    REAL    NOT NULL,
    title         TEXT,
    description   TEXT,
    cover_url     TEXT,
    path          TEXT,
    has_comicinfo INTEGER NOT NULL DEFAULT 0,
    file_size     INTEGER,
    last_seen     TEXT,
    UNIQUE(series_id, volume_num)
);

CREATE TABLE IF NOT EXISTS chapters (
    id                INTEGER PRIMARY KEY,
    series_id         INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    volume_id         INTEGER REFERENCES volumes(id),
    source            TEXT,
    source_chapter_id TEXT,
    chapter_num       REAL    NOT NULL,
    title             TEXT,
    group_name        TEXT,
    language          TEXT,
    publish_date      TEXT,
    path              TEXT,
    has_comicinfo     INTEGER NOT NULL DEFAULT 0,
    file_size         INTEGER,
    status            TEXT    NOT NULL DEFAULT 'known',
    UNIQUE(series_id, chapter_num)
);

CREATE TABLE IF NOT EXISTS rename_log (
    id          INTEGER PRIMARY KEY,
    series_id   INTEGER REFERENCES series(id),
    volume_id   INTEGER REFERENCES volumes(id),
    chapter_id  INTEGER REFERENCES chapters(id),
    old_path    TEXT    NOT NULL,
    new_path    TEXT,
    action      TEXT    NOT NULL,
    reason      TEXT,
    timestamp   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id                   INTEGER PRIMARY KEY,
    job_type             TEXT    NOT NULL,
    queue_key            TEXT    NOT NULL DEFAULT 'default',
    status               TEXT    NOT NULL DEFAULT 'queued',
    series_id            INTEGER REFERENCES series(id) ON DELETE SET NULL,
    series_path_snapshot TEXT,
    payload_json         TEXT    NOT NULL DEFAULT '{}',
    requested_by         TEXT,
    created_at           TEXT    NOT NULL,
    started_at           TEXT,
    ended_at             TEXT,
    exit_code            INTEGER,
    error_summary        TEXT,
    last_line_at         TEXT
);

CREATE TABLE IF NOT EXISTS job_logs (
    id         INTEGER PRIMARY KEY,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    line       TEXT    NOT NULL,
    ts         TEXT    NOT NULL,
    UNIQUE(job_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_queue_created
    ON jobs(status, queue_key, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_series_status
    ON jobs(series_id, status);
CREATE INDEX IF NOT EXISTS idx_job_logs_job_seq
    ON job_logs(job_id, seq);

CREATE TABLE IF NOT EXISTS app_config (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    settings_json   TEXT    NOT NULL DEFAULT '{}',
    usage_json      TEXT    NOT NULL DEFAULT '{}'
);
"""

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def db_path(data_dir: str = None) -> str:
    base = data_dir or DATA_DIR
    return os.path.join(base, DB_SUBDIR, DB_FILENAME)


def get_conn(data_dir: str = None) -> sqlite3.Connection:
    path = db_path(data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply additive migrations for existing databases."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(series)")}
    if "exclude_from_fix" not in cols:
        conn.execute(
            "ALTER TABLE series ADD COLUMN exclude_from_fix INTEGER NOT NULL DEFAULT 0"
        )
    if "merge_volumes_override" not in cols:
        conn.execute(
            "ALTER TABLE series ADD COLUMN merge_volumes_override INTEGER"
        )
    if "preferred_groups_json" not in cols:
        conn.execute("ALTER TABLE series ADD COLUMN preferred_groups_json TEXT")
        _migrate_preferred_groups_json(conn)
    if "sync_configured" not in cols:
        conn.execute(
            "ALTER TABLE series ADD COLUMN sync_configured INTEGER NOT NULL DEFAULT 1"
        )
    if "sync_paused" not in cols:
        conn.execute(
            "ALTER TABLE series ADD COLUMN sync_paused INTEGER NOT NULL DEFAULT 0"
        )
    if "ignored" not in cols:
        conn.execute(
            "ALTER TABLE series ADD COLUMN ignored INTEGER NOT NULL DEFAULT 0"
        )
    if "mangadex_id" not in cols:
        conn.execute("ALTER TABLE series ADD COLUMN mangadex_id TEXT")
        # Back-fill for existing MDX-primary series: source_id IS the MDX UUID
        conn.execute(
            """
            UPDATE series SET mangadex_id = (
                SELECT source_id FROM series_sources
                WHERE series_sources.series_id = series.id
                  AND series_sources.source = 'mangadex'
                LIMIT 1
            ) WHERE mangadex_id IS NULL
            """
        )
    if "last_aggregate_volume_remap_at" not in cols:
        conn.execute(
            "ALTER TABLE series ADD COLUMN last_aggregate_volume_remap_at TEXT"
        )

    # ``since`` (skip <= N) → ``start_chapter`` (download where >= N). Convert
    # the value so the resulting download set is unchanged: 0 stays 0; any
    # positive value becomes ``floor(value) + 1``, i.e. the smallest integer
    # strictly greater than the old cutoff. ``CAST(real AS INTEGER)`` in
    # SQLite truncates toward zero, which equals floor for non-negatives.
    if "since" in cols and "start_chapter" not in cols:
        conn.execute("ALTER TABLE series RENAME COLUMN since TO start_chapter")
        conn.execute(
            """
            UPDATE series
            SET start_chapter = CASE
                WHEN start_chapter IS NULL OR start_chapter <= 0 THEN 0
                ELSE CAST(start_chapter AS INTEGER) + 1
            END
            """
        )

    # One-shot: chapters with files but no real source identity were treated
    # as ``downloaded`` by older builds, which leaked feed metadata
    # (group_name, language, etc.) into ComicInfo for user-placed files. Tag
    # those rows as ``on_disk`` so series-level data drives ComicInfo. Rows
    # with ``source_chapter_id`` set are left alone - they may be real mdx
    # downloads. Use the ``Reset chapter metadata`` UI to clear ambiguous
    # cases (e.g. linked-only-for-covers like Yotsuba).
    if conn.execute(
        "SELECT 1 FROM chapters WHERE path IS NOT NULL"
        " AND COALESCE(source_chapter_id, '') = '' AND status != 'on_disk' LIMIT 1"
    ).fetchone():
        conn.execute(
            """
            UPDATE chapters
            SET status = 'on_disk'
            WHERE path IS NOT NULL
              AND COALESCE(source_chapter_id, '') = ''
              AND status != 'on_disk'
            """
        )

    ch_cols = {row[1] for row in conn.execute("PRAGMA table_info(chapters)")}
    if "created_at" not in ch_cols:
        conn.execute("ALTER TABLE chapters ADD COLUMN created_at TEXT")

    ac_cols = {row[1] for row in conn.execute("PRAGMA table_info(app_config)")}
    if "usage_json" not in ac_cols:
        conn.execute(
            "ALTER TABLE app_config ADD COLUMN usage_json TEXT NOT NULL DEFAULT '{}'"
        )

    vol_cols = {row[1] for row in conn.execute("PRAGMA table_info(volumes)")}
    if "kavita_cover_url" not in vol_cols:
        conn.execute("ALTER TABLE volumes ADD COLUMN kavita_cover_url TEXT")


def _migrate_preferred_groups_json(conn: sqlite3.Connection) -> None:
    """One-time: [preferred_group] JSON for rows that only had a single string."""
    rows = conn.execute(
        "SELECT id, preferred_group FROM series WHERE preferred_group IS NOT NULL"
        " AND TRIM(preferred_group) != ''"
    ).fetchall()
    for r in rows:
        pj = json.dumps([r["preferred_group"].strip()], ensure_ascii=False)
        conn.execute(
            "UPDATE series SET preferred_groups_json=? WHERE id=?",
            (pj, r["id"]),
        )


def init_db(data_dir: str = None) -> sqlite3.Connection:
    conn = get_conn(data_dir)
    conn.executescript(_SCHEMA)
    ensure_schema(conn)
    _ensure_app_config_row(conn)
    _migrate_legacy_settings_json(conn, data_dir)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# App settings (web UI / sync) - stored in SQLite under ``/data/db/``
# ---------------------------------------------------------------------------

def legacy_settings_json_path(data_dir: str = None) -> str:
    """Path to legacy ``settings.json`` (imported once into ``app_config``)."""
    override = os.environ.get("CONFIG_PATH", "").strip()
    if override:
        return override
    base = data_dir or DATA_DIR
    return os.path.join(base, "config", "settings.json")


def _ensure_app_config_row(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO app_config (id, settings_json) VALUES (1, '{}')"
    )


def _migrate_legacy_settings_json(
    conn: sqlite3.Connection, data_dir: str = None
) -> None:
    row = conn.execute(
        "SELECT settings_json FROM app_config WHERE id=1"
    ).fetchone()
    if not row:
        return
    try:
        cur = json.loads(row["settings_json"] or "{}")
    except json.JSONDecodeError:
        cur = {}
    if cur:
        return
    path = legacy_settings_json_path(data_dir)
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            file_data = json.load(f)
    except Exception:
        return
    if not isinstance(file_data, dict):
        return
    file_data.pop("volume_mode", None)
    conn.execute(
        "UPDATE app_config SET settings_json = ? WHERE id = 1",
        (json.dumps(file_data),),
    )
    try:
        os.rename(path, path + ".migrated")
    except OSError:
        pass


def read_stored_settings(conn: sqlite3.Connection) -> dict:
    """Return decoded settings dict from ``app_config`` (may be partial)."""
    row = conn.execute(
        "SELECT settings_json FROM app_config WHERE id=1"
    ).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row["settings_json"] or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def write_stored_settings(conn: sqlite3.Connection, settings: dict) -> None:
    """Persist full merged settings blob (replaces JSON file from older builds)."""
    s = dict(settings)
    s.pop("volume_mode", None)
    conn.execute(
        """
        INSERT INTO app_config (id, settings_json) VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET settings_json = excluded.settings_json
        """,
        (json.dumps(s, indent=2),),
    )
    conn.commit()


def increment_usage(conn: sqlite3.Connection, key: str, amount: int = 1) -> None:
    """Bump a lifetime action counter by ``amount`` (default 1)."""
    row = conn.execute("SELECT usage_json FROM app_config WHERE id=1").fetchone()
    data: dict = {}
    if row:
        try:
            data = json.loads(row["usage_json"] or "{}") or {}
        except json.JSONDecodeError:
            pass
    data[key] = data.get(key, 0) + amount
    conn.execute(
        "UPDATE app_config SET usage_json=? WHERE id=1",
        (json.dumps(data),),
    )
    conn.commit()


def get_usage(conn: sqlite3.Connection) -> dict:
    """Return the current usage counters dict."""
    row = conn.execute("SELECT usage_json FROM app_config WHERE id=1").fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["usage_json"] or "{}") or {}
    except json.JSONDecodeError:
        return {}


def record_job_timing(conn: sqlite3.Connection, job_type: str, duration_s: float) -> None:
    """Append a job timing entry; keep rolling 20 entries per type total."""
    usage = get_usage(conn)
    timings: list = usage.get("job_timings") or []
    timings.append({"type": job_type, "duration_s": round(duration_s, 1), "ts": _now()})
    timings = timings[-20:]
    conn.execute(
        "UPDATE app_config SET usage_json=? WHERE id=1",
        (json.dumps({**usage, "job_timings": timings}),),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_json_list(val) -> list:
    if not val:
        return []
    if isinstance(val, list):
        return val
    try:
        return json.loads(val)
    except Exception:
        return []


def normalize_preferred_groups_storage(
    preferred_groups_json: str | None,
    preferred_group_legacy: str | None = None,
) -> tuple[str | None, str | None]:
    """Return ``(preferred_groups_json, preferred_group)`` for DB columns."""
    names: list[str] = []
    if preferred_groups_json and str(preferred_groups_json).strip():
        try:
            arr = json.loads(preferred_groups_json)
            if isinstance(arr, list):
                names = [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            pass
    if not names and preferred_group_legacy and str(preferred_group_legacy).strip():
        names = [preferred_group_legacy.strip()]
    if not names:
        return None, None
    return json.dumps(names, ensure_ascii=False), names[0]


def preferred_groups_list_from_row(row: dict) -> list[str]:
    raw = row.get("preferred_groups_json")
    if raw:
        try:
            arr = json.loads(raw)
            if isinstance(arr, list):
                out = [str(x).strip() for x in arr if str(x).strip()]
                if out:
                    return out
        except Exception:
            pass
    pg = row.get("preferred_group")
    if pg and str(pg).strip():
        return [str(pg).strip()]
    return []

# ---------------------------------------------------------------------------
# Migration: .mangadex.json → DB
# ---------------------------------------------------------------------------

def migrate_json_configs(manga_root: str, conn: sqlite3.Connection) -> int:
    """Walk manga_root, import .mangadex.json files into DB (idempotent)."""
    imported = 0
    now = _now()
    for root, dirs, files in os.walk(manga_root):
        dirs.sort()
        if CONFIG_FILENAME not in files:
            continue
        cfg_path = os.path.join(root, CONFIG_FILENAME)
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
        except Exception:
            dirs.clear()
            continue

        manga_id = cfg.get("id")
        rel_path = os.path.relpath(root, manga_root)
        title = os.path.basename(root)
        language = cfg.get("language", "en")
        preferred_group = cfg.get("translator") or None
        pj, pg = normalize_preferred_groups_storage(None, preferred_group)
        # Legacy ``.mangadex.json`` files used ``since`` with skip-<= semantics.
        # Convert to the new ``start_chapter`` (download chapter_num >= value):
        # 0 stays 0, a positive cutoff becomes ``floor(value) + 1`` so the
        # resulting download set is unchanged.
        legacy_since = float(cfg.get("since", 0) or 0)
        if legacy_since <= 0:
            start_chapter = 0.0
        else:
            start_chapter = float(int(legacy_since) + 1)

        conn.execute("""
            INSERT INTO series (
                title, path, language, preferred_group, preferred_groups_json,
                start_chapter, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                language              = excluded.language,
                preferred_group       = excluded.preferred_group,
                preferred_groups_json = excluded.preferred_groups_json,
                start_chapter         = excluded.start_chapter,
                updated_at            = excluded.updated_at
        """, (title, rel_path, language, pg, pj, start_chapter, now, now))

        series_id = conn.execute(
            "SELECT id FROM series WHERE path = ?", (rel_path,)
        ).fetchone()["id"]

        if manga_id:
            # Do not push legacy MangaDex rows from disk if this folder is already
            # linked via the web UI to another adapter (e.g. Suwayomi/MangaPlus).
            # Otherwise both sources sit at default priority 1 and list queries
            # duplicate the series until the stale ``.mangadex.json`` is removed.
            has_non_mdx_source = conn.execute(
                "SELECT 1 FROM series_sources WHERE series_id=? AND source != 'mangadex' LIMIT 1",
                (series_id,),
            ).fetchone()
            if not has_non_mdx_source:
                conn.execute("""
                    INSERT INTO series_sources (series_id, source, source_id, priority)
                    VALUES (?, 'mangadex', ?, 1)
                    ON CONFLICT(series_id, source) DO UPDATE SET source_id = excluded.source_id
                """, (series_id, manga_id))

                cover_filename = cfg.get("cover_filename")
                status         = cfg.get("status")
                total_volumes  = cfg.get("total_volumes")
                if any(v is not None for v in [cover_filename, status, total_volumes]):
                    conn.execute("""
                        INSERT INTO series_metadata
                            (series_id, source, cover_filename, status, total_volumes, fetched_at)
                        VALUES (?, 'mangadex', ?, ?, ?, ?)
                        ON CONFLICT(series_id, source) DO UPDATE SET
                            cover_filename = COALESCE(excluded.cover_filename, cover_filename),
                            status         = COALESCE(excluded.status, status),
                            total_volumes  = COALESCE(excluded.total_volumes, total_volumes)
                    """, (series_id, cover_filename, status, total_volumes, now))

        imported += 1
        dirs.clear()
        try:
            os.remove(cfg_path)
        except OSError:
            pass

    conn.commit()
    return imported


def scan_disk_series(
    manga_root: str,
    conn: sqlite3.Connection,
    allowed_roots: list[str] | None = None,
) -> int:
    """Find dirs with manga files but no DB entry; add as unlinked series.

    If ``allowed_roots`` is a list (including empty), only paths under those
    roots are considered; an empty list discovers nothing. If ``None``, the
    whole tree under ``manga_root`` is scanned (legacy callers / tests).
    """
    unrestricted = allowed_roots is None
    roots: list[str] = []
    if not unrestricted:
        for rf in allowed_roots:
            s = str(rf or "").strip().strip("/")
            if not s:
                # Empty string means MANGA_ROOT itself — treat as unrestricted.
                unrestricted = True
                break
            if s not in roots:
                roots.append(s)

    def _is_allowed(rel_path: str) -> bool:
        if unrestricted:
            return True
        if not roots:
            return False
        p = rel_path.replace("\\", "/").strip()
        return any(p == r or p.startswith(r + "/") for r in roots)

    added = 0
    now = _now()
    for root, dirs, files in os.walk(manga_root):
        dirs.sort()
        has_manga = any(os.path.splitext(f)[1].lower() in MANGA_EXTENSIONS for f in files)
        if not has_manga:
            continue
        rel_path = os.path.relpath(root, manga_root)
        if not _is_allowed(rel_path):
            continue
        if conn.execute("SELECT 1 FROM series WHERE path=?", (rel_path,)).fetchone():
            dirs.clear()
            continue
        parent = os.path.dirname(rel_path)
        if parent != "." and conn.execute(
            "SELECT 1 FROM series WHERE path=?", (parent,)
        ).fetchone():
            dirs.clear()
            continue
        title = os.path.basename(root)
        conn.execute("""
            INSERT INTO series (title, path, language, start_chapter, created_at, updated_at)
            VALUES (?, ?, 'en', 0, ?, ?)
        """, (title, rel_path, now, now))
        added += 1
        dirs.clear()

    conn.commit()
    return added

# ---------------------------------------------------------------------------
# Series CRUD
# ---------------------------------------------------------------------------

def get_all_series(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT
            s.id, s.title, s.path, s.language, s.preferred_group,
            s.preferred_groups_json, s.start_chapter,
            s.exclude_from_fix, s.merge_volumes_override, s.sync_configured, s.sync_paused,
            s.ignored, s.updated_at,
            ss.source  AS source_name,
            ss.source_id,
            (SELECT MAX(c.created_at) FROM chapters c WHERE c.series_id = s.id) AS latest_chapter_at,
            sm.description, sm.tags, sm.authors, sm.artists,
            sm.year, sm.status, sm.content_rating, sm.total_volumes, sm.cover_filename,
            (SELECT COUNT(*) FROM chapters c
             WHERE c.series_id = s.id AND c.path IS NOT NULL) AS chapter_count,
            (SELECT COUNT(*) FROM (
                SELECT id FROM volumes WHERE series_id = s.id AND path IS NOT NULL
                UNION
                SELECT volume_id FROM chapters
                WHERE series_id = s.id AND path IS NOT NULL AND volume_id IS NOT NULL
            )) AS volume_count
        FROM series s
        LEFT JOIN series_sources ss ON ss.id = (
            SELECT ss2.id FROM series_sources ss2
            WHERE ss2.series_id = s.id
            ORDER BY ss2.priority ASC, ss2.source ASC
            LIMIT 1
        )
        LEFT JOIN series_metadata sm
            ON s.id = sm.series_id AND sm.source = ss.source
        ORDER BY LOWER(s.title)
    """).fetchall()

    result = []
    for row in rows:
        d = dict(row)
        rel = d["path"]
        parent = os.path.dirname(rel)
        d["subfolder"] = parent if parent != "." else ""
        d["name"]      = os.path.basename(rel)
        d["linked"]    = bool(d.get("source_id"))
        d["preferred_groups"] = preferred_groups_list_from_row(d)
        d["config"] = {
            "id":            d.get("source_id"),
            "language":      d["language"],
            "translator": (d["preferred_groups"][0] if d["preferred_groups"] else None),
            "translators":   d["preferred_groups"],
            "start_chapter": d["start_chapter"],
            "status":        d.get("status"),
            "total_volumes": d.get("total_volumes"),
            "cover_filename":d.get("cover_filename"),
        }
        if d.get("source_id") and d.get("cover_filename"):
            d["cover_url"] = f"/api/proxy/cover/{d['source_id']}/{d['cover_filename']}"
        else:
            d["cover_url"] = None
        result.append(d)
    return result


def get_series_by_path(conn: sqlite3.Connection, path: str) -> dict | None:
    row = conn.execute("""
        SELECT s.*, ss.source AS source_name, ss.source_id,
               sm.cover_filename, sm.status, sm.total_volumes, sm.description,
               sm.tags, sm.authors, sm.artists, sm.year, sm.content_rating,
               (SELECT COUNT(*) FROM chapters c WHERE c.series_id = s.id) AS chapter_count,
               (SELECT COUNT(*) FROM volumes v WHERE v.series_id = s.id) AS source_volume_count
        FROM series s
        LEFT JOIN series_sources ss ON ss.id = (
            SELECT ss2.id FROM series_sources ss2
            WHERE ss2.series_id = s.id
            ORDER BY ss2.priority ASC, ss2.source ASC
            LIMIT 1
        )
        LEFT JOIN series_metadata sm ON s.id = sm.series_id AND sm.source = ss.source
        WHERE s.path = ?
    """, (path,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["tags"] = _parse_json_list(d.get("tags"))
    d["authors"] = _parse_json_list(d.get("authors"))
    d["artists"] = _parse_json_list(d.get("artists"))
    d["name"]   = os.path.basename(path)
    d["linked"] = bool(d.get("source_id"))
    d["preferred_groups"] = preferred_groups_list_from_row(d)
    d["config"] = {
        "id":            d.get("source_id"),
        "language":      d["language"],
        "translator": (d["preferred_groups"][0] if d["preferred_groups"] else None),
        "translators":   d["preferred_groups"],
        "start_chapter": d["start_chapter"],
        "status":        d.get("status"),
        "total_volumes": d.get("total_volumes"),
        "cover_filename":d.get("cover_filename"),
    }
    if d.get("source_id") and d.get("cover_filename"):
        d["cover_url"] = f"/api/proxy/cover/{d['source_id']}/{d['cover_filename']}"
    else:
        d["cover_url"] = None
    return d


def get_linked_series(conn: sqlite3.Connection) -> list[dict]:
    """Return series that have at least one source linked."""
    return [s for s in get_all_series(conn) if s["linked"]]


def insert_series(
    conn: sqlite3.Connection,
    path: str,
    title: str,
    language: str,
    start_chapter: float,
    source: str | None = None,
    source_id: str | None = None,
    mangadex_id: str | None = None,
    cover_filename: str | None = None,
    exclude_from_fix: int = 0,
    merge_volumes_override: int | None = None,
    preferred_groups_json: str | None = None,
    preferred_group: str | None = None,
    sync_configured: int = 1,
) -> int:
    now = _now()
    pj, pg = normalize_preferred_groups_storage(preferred_groups_json, preferred_group)
    # For MDX-primary series the MDX UUID and the source_id are the same thing.
    effective_mdx_id = mangadex_id or (source_id if source == "mangadex" else None)
    conn.execute("""
        INSERT INTO series (
            title, path, language, preferred_group, preferred_groups_json, start_chapter,
            exclude_from_fix, merge_volumes_override, sync_configured, mangadex_id,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            title                 = excluded.title,
            language              = excluded.language,
            preferred_group       = excluded.preferred_group,
            preferred_groups_json = excluded.preferred_groups_json,
            start_chapter         = excluded.start_chapter,
            sync_configured       = excluded.sync_configured,
            mangadex_id           = COALESCE(excluded.mangadex_id, series.mangadex_id),
            updated_at            = excluded.updated_at
    """, (
        title, path, language, pg, pj, start_chapter,
        exclude_from_fix, merge_volumes_override, int(bool(sync_configured)),
        effective_mdx_id, now, now,
    ))

    series_id = conn.execute(
        "SELECT id FROM series WHERE path=?", (path,)
    ).fetchone()["id"]

    if source and source_id:
        conn.execute("""
            INSERT INTO series_sources (series_id, source, source_id, priority)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(series_id, source) DO UPDATE SET source_id = excluded.source_id
        """, (series_id, source, source_id))

    if cover_filename and source and source_id:
        conn.execute("""
            INSERT INTO series_metadata (series_id, source, cover_filename, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(series_id, source) DO UPDATE SET
                cover_filename = excluded.cover_filename
        """, (series_id, source, cover_filename, now))

    conn.commit()
    return series_id


def update_series(
    conn: sqlite3.Connection,
    old_path: str,
    new_path: str,
    title: str,
    language: str,
    start_chapter: float,
    source: str | None = None,
    source_id: str | None = None,
    mangadex_id: str | None = None,
    cover_filename: str | None = None,
    exclude_from_fix: int = 0,
    merge_volumes_override: int | None = None,
    preferred_groups_json: str | None = None,
    preferred_group: str | None = None,
    sync_configured: int | None = None,
) -> bool:
    now = _now()
    pj, pg = normalize_preferred_groups_storage(preferred_groups_json, preferred_group)
    effective_mdx_id = mangadex_id or (source_id if source == "mangadex" else None)
    if sync_configured is None:
        cur = conn.execute("""
            UPDATE series
            SET path=?, title=?, language=?, preferred_group=?, preferred_groups_json=?,
                start_chapter=?,
                exclude_from_fix=?, merge_volumes_override=?,
                mangadex_id=COALESCE(?, mangadex_id),
                updated_at=?
            WHERE path=?
        """, (
            new_path, title, language, pg, pj, start_chapter,
            exclude_from_fix, merge_volumes_override,
            effective_mdx_id, now, old_path,
        ))
    else:
        cur = conn.execute("""
            UPDATE series
            SET path=?, title=?, language=?, preferred_group=?, preferred_groups_json=?,
                start_chapter=?,
                exclude_from_fix=?, merge_volumes_override=?,
                sync_configured=?,
                mangadex_id=COALESCE(?, mangadex_id),
                updated_at=?
            WHERE path=?
        """, (
            new_path, title, language, pg, pj, start_chapter,
            exclude_from_fix, merge_volumes_override,
            int(bool(sync_configured)),
            effective_mdx_id, now, old_path,
        ))
    if cur.rowcount == 0:
        return False

    series_id = conn.execute(
        "SELECT id FROM series WHERE path=?", (new_path,)
    ).fetchone()["id"]

    if source and source_id:
        conn.execute("""
            INSERT INTO series_sources (series_id, source, source_id, priority)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(series_id, source) DO UPDATE SET source_id = excluded.source_id
        """, (series_id, source, source_id))

    if cover_filename and source and source_id:
        conn.execute("""
            INSERT INTO series_metadata (series_id, source, cover_filename, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(series_id, source) DO UPDATE SET
                cover_filename = excluded.cover_filename
        """, (series_id, source, cover_filename, now))

    conn.commit()
    return True


def reset_chapter_source_metadata(
    conn: sqlite3.Connection, series_id: int
) -> int:
    """Detach per-chapter source metadata from files already on disk.

    Catalog-only rows (``path IS NULL``) keep their source info so future
    downloads still work. For files we already have on disk, all per-chapter
    fields populated from the remote feed (title, group, language,
    source_chapter_id) are wiped and the row is moved to ``status='on_disk'``,
    which makes ComicInfo fall back to series-level data only.

    ``has_comicinfo`` is reset to 0 so the next ComicInfo pass rewrites the
    embedded XML.
    """
    cur = conn.execute(
        """
        UPDATE chapters
        SET status = 'on_disk',
            source = NULL,
            source_chapter_id = NULL,
            title = NULL,
            group_name = NULL,
            language = NULL,
            publish_date = NULL,
            has_comicinfo = 0
        WHERE series_id = ? AND path IS NOT NULL
        """,
        (series_id,),
    )
    conn.commit()
    return cur.rowcount or 0


def unlink_series(conn: sqlite3.Connection, path: str) -> bool:
    """Remove source links, series-level remote metadata, and per-chapter source fields.

    Clears ``mangadex_id`` and ``series_metadata`` so an unlinked folder does not
    retain stale MangaDex companion or cover rows. Files on disk and the series
    row stay; merging the unlink with the chapter reset means ComicInfo
    regenerated afterwards will not retain feed-only fields (e.g. a Vietnamese
    scanlator's name on an English archive that only had the link for cover purposes).
    """
    row = conn.execute("SELECT id FROM series WHERE path=?", (path,)).fetchone()
    if not row:
        return False
    sid = row["id"]
    conn.execute("DELETE FROM series_sources WHERE series_id=?", (sid,))
    # Remote metadata / companion UUID — not tied to series_sources rows but
    # misleading if left behind after unlink (and harmless to drop; refetch on relink).
    conn.execute("DELETE FROM series_metadata WHERE series_id=?", (sid,))
    now = _now()
    conn.execute(
        "UPDATE series SET mangadex_id = NULL, updated_at = ? WHERE id = ?",
        (now, sid),
    )
    reset_chapter_source_metadata(conn, sid)
    conn.commit()
    return True


def delete_series(conn: sqlite3.Connection, path: str) -> bool:
    """Remove series entirely from DB (files on disk untouched)."""
    row = conn.execute("SELECT id FROM series WHERE path=?", (path,)).fetchone()
    if not row:
        return False
    sid = row["id"]
    # ``rename_log`` references series/volumes/chapters without ON DELETE CASCADE;
    # clearing first avoids IntegrityError when removing the series row.
    conn.execute("DELETE FROM rename_log WHERE series_id=?", (sid,))
    conn.execute(
        "DELETE FROM rename_log WHERE volume_id IN (SELECT id FROM volumes WHERE series_id=?)",
        (sid,),
    )
    conn.execute(
        "DELETE FROM rename_log WHERE chapter_id IN (SELECT id FROM chapters WHERE series_id=?)",
        (sid,),
    )
    conn.execute("DELETE FROM series WHERE id=?", (sid,))
    conn.commit()
    return True


def set_sync_paused(conn: sqlite3.Connection, path: str, paused: bool) -> bool:
    cur = conn.execute(
        "UPDATE series SET sync_paused=?, updated_at=? WHERE path=?",
        (1 if paused else 0, _now(), path),
    )
    conn.commit()
    return cur.rowcount > 0


def set_series_ignored(conn: sqlite3.Connection, path: str, ignored: bool) -> bool:
    cur = conn.execute(
        "UPDATE series SET ignored=?, updated_at=? WHERE path=?",
        (1 if ignored else 0, _now(), path),
    )
    conn.commit()
    return cur.rowcount > 0

# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def get_sources(conn: sqlite3.Connection, series_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM series_sources WHERE series_id=? ORDER BY priority",
        (series_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_primary_source(conn: sqlite3.Connection, series_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM series_sources WHERE series_id=? ORDER BY priority LIMIT 1",
        (series_id,)
    ).fetchone()
    return dict(row) if row else None


def update_source_sync_time(conn: sqlite3.Connection, series_id: int, source: str):
    conn.execute("""
        UPDATE series_sources SET last_synced_at=? WHERE series_id=? AND source=?
    """, (_now(), series_id, source))
    conn.commit()

# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def upsert_series_metadata(
    conn: sqlite3.Connection, series_id: int, source: str, **fields
) -> None:
    for key in ("tags", "authors", "artists"):
        if key in fields and isinstance(fields[key], list):
            fields[key] = json.dumps(fields[key], ensure_ascii=False)

    fields["fetched_at"] = _now()

    existing = conn.execute(
        "SELECT 1 FROM series_metadata WHERE series_id=? AND source=?",
        (series_id, source)
    ).fetchone()

    if existing:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(
            f"UPDATE series_metadata SET {sets} WHERE series_id=? AND source=?",
            list(fields.values()) + [series_id, source]
        )
    else:
        fields["series_id"] = series_id
        fields["source"]    = source
        cols = ", ".join(fields.keys())
        phs  = ", ".join("?" * len(fields))
        conn.execute(
            f"INSERT INTO series_metadata ({cols}) VALUES ({phs})",
            list(fields.values())
        )
    conn.commit()


def get_series_metadata(
    conn: sqlite3.Connection, series_id: int, source: str = None
) -> dict | None:
    if source:
        row = conn.execute(
            "SELECT * FROM series_metadata WHERE series_id=? AND source=?",
            (series_id, source)
        ).fetchone()
    else:
        row = conn.execute("""
            SELECT sm.* FROM series_metadata sm
            JOIN series_sources ss ON sm.series_id = ss.series_id AND sm.source = ss.source
            WHERE sm.series_id=? ORDER BY ss.priority LIMIT 1
        """, (series_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for key in ("tags", "authors", "artists"):
        d[key] = _parse_json_list(d.get(key))
    return d


def is_metadata_stale(fetched_at: str | None, days: int = 7) -> bool:
    if not fetched_at:
        return True
    try:
        return datetime.now() - datetime.fromisoformat(fetched_at) > timedelta(days=days)
    except Exception:
        return True

# ---------------------------------------------------------------------------
# Volumes
# ---------------------------------------------------------------------------

def upsert_volume(
    conn: sqlite3.Connection, series_id: int, volume_num: float, **fields
) -> int:
    existing = conn.execute(
        "SELECT id FROM volumes WHERE series_id=? AND volume_num=?",
        (series_id, volume_num)
    ).fetchone()
    if existing:
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(
                f"UPDATE volumes SET {sets} WHERE series_id=? AND volume_num=?",
                list(fields.values()) + [series_id, volume_num]
            )
        return existing["id"]
    fields["series_id"]  = series_id
    fields["volume_num"] = volume_num
    cols = ", ".join(fields.keys())
    phs  = ", ".join("?" * len(fields))
    cur  = conn.execute(
        f"INSERT INTO volumes ({cols}) VALUES ({phs})", list(fields.values())
    )
    return cur.lastrowid


def get_volume(
    conn: sqlite3.Connection, series_id: int, volume_num: float
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM volumes WHERE series_id=? AND volume_num=?",
        (series_id, volume_num)
    ).fetchone()
    return dict(row) if row else None


def get_volumes(conn: sqlite3.Connection, series_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM volumes WHERE series_id=? ORDER BY volume_num",
        (series_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def mark_volume_merged(
    conn: sqlite3.Connection, volume_id: int, path: str, file_size: int
):
    conn.execute("""
        UPDATE volumes SET path=?, file_size=?, last_seen=? WHERE id=?
    """, (path, file_size, _now(), volume_id))
    conn.commit()


def mark_volume_comicinfo(conn: sqlite3.Connection, volume_id: int):
    conn.execute("UPDATE volumes SET has_comicinfo=1 WHERE id=?", (volume_id,))
    conn.commit()

# ---------------------------------------------------------------------------
# Chapters
# ---------------------------------------------------------------------------

def upsert_chapter(
    conn: sqlite3.Connection,
    series_id: int,
    chapter_num: float,
    source: str,
    source_chapter_id: str,
    **fields,
) -> int:
    existing = conn.execute(
        "SELECT id FROM chapters WHERE series_id=? AND chapter_num=?",
        (series_id, chapter_num)
    ).fetchone()

    base = {"source": source, "source_chapter_id": source_chapter_id, **fields}

    if existing:
        sets = ", ".join(f"{k}=?" for k in base)
        conn.execute(
            f"UPDATE chapters SET {sets} WHERE series_id=? AND chapter_num=?",
            list(base.values()) + [series_id, chapter_num]
        )
        return existing["id"]

    base["series_id"]   = series_id
    base["chapter_num"] = chapter_num
    base["created_at"]  = _now()
    cols = ", ".join(base.keys())
    phs  = ", ".join("?" * len(base))
    cur  = conn.execute(
        f"INSERT INTO chapters ({cols}) VALUES ({phs})", list(base.values())
    )
    return cur.lastrowid


def get_chapter(conn: sqlite3.Connection, chapter_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM chapters WHERE id=?", (chapter_id,)).fetchone()
    return dict(row) if row else None


def get_chapters(conn: sqlite3.Connection, series_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM chapters WHERE series_id=? ORDER BY chapter_num",
        (series_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_chapters_for_volume(
    conn: sqlite3.Connection, series_id: int, volume_num: float
) -> list[dict]:
    rows = conn.execute("""
        SELECT c.* FROM chapters c
        JOIN volumes v ON c.volume_id = v.id
        WHERE c.series_id=? AND v.volume_num=?
        ORDER BY c.chapter_num
    """, (series_id, volume_num)).fetchall()
    return [dict(r) for r in rows]


def get_chapters_to_download(
    conn: sqlite3.Connection, series_id: int, start_chapter: float
) -> list[dict]:
    """Return catalog rows that sync should pull (``chapter_num >= start_chapter``).

    A ``start_chapter`` of 0 selects every catalog row. The series row stores
    this as ``start_chapter`` and the UI exposes it as
    *Start from chapter ≥*.
    """
    rows = conn.execute("""
        SELECT * FROM chapters
        WHERE series_id=? AND chapter_num >= ? AND status='known' AND path IS NULL
        ORDER BY chapter_num
    """, (series_id, start_chapter)).fetchall()
    return [dict(r) for r in rows]


def mark_chapter_downloaded(
    conn: sqlite3.Connection, chapter_id: int, path: str, file_size: int,
    commit: bool = True,
):
    conn.execute("""
        UPDATE chapters SET path=?, file_size=?, status='downloaded' WHERE id=?
    """, (path, file_size, chapter_id))
    if commit:
        conn.commit()


def mark_chapter_comicinfo(conn: sqlite3.Connection, chapter_id: int):
    conn.execute("UPDATE chapters SET has_comicinfo=1 WHERE id=?", (chapter_id,))
    conn.commit()


def assign_chapter_to_volume(
    conn: sqlite3.Connection, chapter_id: int, volume_id: int
):
    conn.execute(
        "UPDATE chapters SET volume_id=? WHERE id=?", (volume_id, chapter_id)
    )
    conn.commit()


def apply_aggregate_volume_mapping(
    conn: sqlite3.Connection, series_id: int, agg: dict | None
) -> None:
    """Map local chapter rows to MangaDex volume buckets using a /aggregate response."""
    volumes_map = (agg or {}).get("volumes") or {}
    for vol_key, vol_data in volumes_map.items():
        if vol_key in ("none", "0"):
            continue
        try:
            vol_num = float(vol_key)
        except (ValueError, TypeError):
            continue
        vol_id = upsert_volume(conn, series_id, vol_num)
        for ch_key in (vol_data.get("chapters") or {}):
            try:
                ch_num = float(ch_key)
            except (ValueError, TypeError):
                continue
            ch_row = conn.execute(
                "SELECT id FROM chapters WHERE series_id=? AND chapter_num=?",
                (series_id, ch_num),
            ).fetchone()
            if ch_row:
                conn.execute(
                    "UPDATE chapters SET volume_id=? WHERE id=? AND (volume_id IS NULL OR volume_id != ?)",
                    (vol_id, ch_row["id"], vol_id),
                )
    conn.commit()


def series_has_materialized_chapter_missing_volume(
    conn: sqlite3.Connection, series_id: int
) -> bool:
    """True if any on-disk (or downloaded) chapter row still lacks ``volume_id``."""
    row = conn.execute(
        """
        SELECT 1 FROM chapters
        WHERE series_id = ? AND volume_id IS NULL
          AND (
              path IS NOT NULL
              OR COALESCE(status, '') IN ('downloaded', 'on_disk')
          )
        LIMIT 1
        """,
        (series_id,),
    ).fetchone()
    return row is not None


def weekly_mdx_aggregate_volume_remap_due(
    conn: sqlite3.Connection, series_id: int, interval_days: int = 7
) -> bool:
    """Whether the low-frequency /aggregate pass should run for stuck chapters.

    Only applies when at least one **materialized** chapter is missing ``volume_id``
    (e.g. MD backfilled tankōbon metadata after the file was downloaded). Debounced
    by ``interval_days`` using ``series.last_aggregate_volume_remap_at``.
    """
    if interval_days < 1:
        interval_days = 1
    if not series_has_materialized_chapter_missing_volume(conn, series_id):
        return False
    row = conn.execute(
        "SELECT last_aggregate_volume_remap_at FROM series WHERE id=?",
        (series_id,),
    ).fetchone()
    raw = row["last_aggregate_volume_remap_at"] if row else None
    if raw is None or not str(raw).strip():
        return True
    try:
        last_dt = datetime.fromisoformat(str(raw).strip())
    except ValueError:
        return True
    return datetime.now() - last_dt >= timedelta(days=interval_days)


def touch_series_aggregate_volume_remap_at(
    conn: sqlite3.Connection, series_id: int
) -> None:
    """Record that we ran the MD /aggregate volume remap for debouncing."""
    conn.execute(
        """
        UPDATE series SET last_aggregate_volume_remap_at = ?
        WHERE id = ?
        """,
        (datetime.now().isoformat(timespec="seconds"), series_id),
    )
    conn.commit()


def get_complete_volumes(conn: sqlite3.Connection, series_id: int) -> list[float]:
    """Volumes with all chapters downloaded and no merged CBZ yet."""
    rows = conn.execute("""
        SELECT v.volume_num FROM volumes v
        WHERE v.series_id = ?
          AND v.path IS NULL
          AND EXISTS (
              SELECT 1 FROM chapters c WHERE c.volume_id = v.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM chapters c
              WHERE c.volume_id = v.id
                AND (c.path IS NULL OR c.status != 'downloaded')
          )
        ORDER BY v.volume_num
    """, (series_id,)).fetchall()
    return [r["volume_num"] for r in rows]


def get_volumes_needing_compact(conn: sqlite3.Connection, series_id: int) -> list[float]:
    """Volumes with no merged CBZ yet but at least one chapter file on disk."""
    rows = conn.execute("""
        SELECT v.volume_num FROM volumes v
        WHERE v.series_id = ?
          AND v.path IS NULL
          AND EXISTS (
              SELECT 1 FROM chapters c
              WHERE c.volume_id = v.id AND c.path IS NOT NULL
          )
        ORDER BY v.volume_num
    """, (series_id,)).fetchall()
    return [r["volume_num"] for r in rows]


def get_files_missing_comicinfo(
    conn: sqlite3.Connection, series_id: int
) -> tuple[list[dict], list[dict]]:
    """Return (chapter_rows, volume_rows) needing ComicInfo injection."""
    ch_rows = conn.execute("""
        SELECT * FROM chapters
        WHERE has_comicinfo=0 AND path IS NOT NULL AND series_id=?
        ORDER BY chapter_num
    """, (series_id,)).fetchall()

    vol_rows = conn.execute("""
        SELECT * FROM volumes
        WHERE has_comicinfo=0 AND path IS NOT NULL AND series_id=?
        ORDER BY volume_num
    """, (series_id,)).fetchall()

    return [dict(r) for r in ch_rows], [dict(r) for r in vol_rows]

# ---------------------------------------------------------------------------
# Disk reconciliation
# ---------------------------------------------------------------------------

def scan_disk_files(
    series_dir: str, series_id: int, conn: sqlite3.Connection,
    manga_root: str = None
):
    """Sync filesystem state into chapters/volumes tables (idempotent)."""
    root = manga_root or MANGA_ROOT
    now  = _now()
    seen_paths: set[str] = set()

    try:
        filenames = sorted(os.listdir(series_dir))
    except OSError:
        return

    for fname in filenames:
        ext = os.path.splitext(fname)[1].lower()
        if ext not in MANGA_EXTENSIONS:
            continue
        fpath    = os.path.join(series_dir, fname)
        rel_path = os.path.relpath(fpath, root)
        size     = os.path.getsize(fpath)
        seen_paths.add(rel_path)

        vol_m = VOL_RE.search(fname)
        ch_m  = CH_RE.search(fname)
        vol_num = float(vol_m.group(1)) if vol_m else None
        ch_num  = float(ch_m.group(1)) if ch_m else None

        has_ch_range = bool(CH_RANGE_RE.search(fname))
        if vol_num is not None and (ch_num is None or has_ch_range):
            # Pure volume file (e.g. "vol.1.cbz" or "Series Vol.01.cbz")
            upsert_volume(conn, series_id, vol_num,
                          path=rel_path, file_size=size, last_seen=now)
            conn.execute(
                "UPDATE chapters SET path=NULL, file_size=NULL, status='known',"
                " has_comicinfo=0 WHERE series_id=? AND path=?",
                (series_id, rel_path)
            )
        elif ch_num is not None:
            vol_id = None
            if vol_num is not None:
                vol_id = upsert_volume(conn, series_id, vol_num)

            existing = conn.execute(
                "SELECT id, path, status FROM chapters"
                " WHERE series_id=? AND chapter_num=?",
                (series_id, ch_num),
            ).fetchone()
            if existing:
                # Only ``mark_chapter_downloaded`` (called right after a real
                # ``mdx dl``) is allowed to mint a row as ``downloaded``. If we
                # find a different file path here the file was placed/renamed
                # outside the sync pipeline → treat it as on_disk and ignore
                # any per-chapter source metadata for ComicInfo.
                if existing["path"] == rel_path:
                    if vol_id is not None:
                        conn.execute(
                            "UPDATE chapters SET volume_id=COALESCE(?, volume_id)"
                            " WHERE id=?",
                            (vol_id, existing["id"]),
                        )
                else:
                    keep_downloaded = (
                        existing["status"] == "downloaded"
                        and existing["path"] is None
                    )
                    new_status = "downloaded" if keep_downloaded else "on_disk"
                    conn.execute(
                        """
                        UPDATE chapters
                        SET path=?, file_size=?, status=?,
                            volume_id=COALESCE(?, volume_id)
                        WHERE id=?
                        """,
                        (rel_path, size, new_status, vol_id, existing["id"]),
                    )
            else:
                conn.execute(
                    """
                    INSERT INTO chapters
                        (series_id, volume_id, chapter_num, path, file_size, status, created_at)
                    VALUES (?, ?, ?, ?, ?, 'on_disk', ?)
                    """,
                    (series_id, vol_id, ch_num, rel_path, size, now),
                )

    # Null-out records for files that no longer exist
    if seen_paths:
        ph = ",".join("?" * len(seen_paths))
        conn.execute(
            f"UPDATE chapters SET path=NULL, file_size=NULL, status='known',"
            f" has_comicinfo=0"
            f" WHERE series_id=? AND path IS NOT NULL AND path NOT IN ({ph})",
            [series_id] + list(seen_paths)
        )
        conn.execute(
            f"UPDATE volumes SET path=NULL, file_size=NULL, last_seen=NULL,"
            f" has_comicinfo=0"
            f" WHERE series_id=? AND path IS NOT NULL AND path NOT IN ({ph})",
            [series_id] + list(seen_paths)
        )
    else:
        conn.execute(
            "UPDATE chapters SET path=NULL, file_size=NULL, status='known',"
            " has_comicinfo=0"
            " WHERE series_id=? AND path IS NOT NULL", (series_id,)
        )
        conn.execute(
            "UPDATE volumes SET path=NULL, file_size=NULL, last_seen=NULL,"
            " has_comicinfo=0"
            " WHERE series_id=? AND path IS NOT NULL", (series_id,)
        )

    # Chapter rows with no file on disk must not stay ``downloaded`` (e.g. after a
    # volume merge or deleting archives) or sync will never re-queue them.
    conn.execute(
        """
        UPDATE chapters SET status='known', has_comicinfo=0
        WHERE series_id=? AND path IS NULL AND status='downloaded'
        """,
        (series_id,),
    )

    conn.commit()

# ---------------------------------------------------------------------------
# Rename log
# ---------------------------------------------------------------------------

def log_rename(
    conn: sqlite3.Connection,
    old_path: str,
    new_path: str | None,
    action: str,
    reason: str,
    series_id: int = None,
    volume_id:  int = None,
    chapter_id: int = None,
):
    conn.execute("""
        INSERT INTO rename_log
            (series_id, volume_id, chapter_id, old_path, new_path, action, reason, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (series_id, volume_id, chapter_id, old_path, new_path, action, reason, _now()))
    conn.commit()


# ---------------------------------------------------------------------------
# Jobs queue + logs
# ---------------------------------------------------------------------------

JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"
JOB_TERMINAL_STATUSES = {
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
}


def enqueue_job(
    conn: sqlite3.Connection,
    *,
    job_type: str,
    queue_key: str = "default",
    series_id: int | None = None,
    series_path_snapshot: str | None = None,
    payload: dict | None = None,
    requested_by: str | None = None,
) -> int:
    payload_json = json.dumps(payload or {}, ensure_ascii=False)
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO jobs (
            job_type, queue_key, status, series_id, series_path_snapshot,
            payload_json, requested_by, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_type,
            queue_key or "default",
            JOB_STATUS_QUEUED,
            series_id,
            series_path_snapshot,
            payload_json,
            requested_by,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _decode_job(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    d = dict(row)
    try:
        d["payload"] = json.loads(d.get("payload_json") or "{}")
    except Exception:
        d["payload"] = {}
    return d


def get_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _decode_job(row)


def list_jobs(
    conn: sqlite3.Connection,
    *,
    statuses: list[str] | None = None,
    limit: int = 100,
) -> list[dict]:
    if statuses:
        ph = ",".join("?" * len(statuses))
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({ph}) ORDER BY id DESC LIMIT ?",
            [*statuses, limit],
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_decode_job(r) for r in rows]


def get_active_jobs(conn: sqlite3.Connection, *, queue_key: str | None = None) -> list[dict]:
    params: list = [JOB_STATUS_RUNNING, JOB_STATUS_QUEUED]
    where = "status IN (?, ?)"
    if queue_key:
        where += " AND queue_key=?"
        params.append(queue_key)
    rows = conn.execute(
        f"SELECT * FROM jobs WHERE {where} ORDER BY "
        "CASE WHEN status='running' THEN 0 ELSE 1 END, id ASC",
        params,
    ).fetchall()
    return [_decode_job(r) for r in rows]


def get_active_job_for_series(conn: sqlite3.Connection, series_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT * FROM jobs
        WHERE series_id=? AND status IN (?, ?)
        ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END, id ASC
        LIMIT 1
        """,
        (series_id, JOB_STATUS_RUNNING, JOB_STATUS_QUEUED),
    ).fetchone()
    return _decode_job(row)


def claim_next_queued_job(conn: sqlite3.Connection, queue_key: str = "default") -> dict | None:
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute(
        """
        SELECT id FROM jobs
        WHERE status=? AND queue_key=?
        ORDER BY id ASC
        LIMIT 1
        """,
        (JOB_STATUS_QUEUED, queue_key),
    )
    row = cur.fetchone()
    cur.close()
    if not row:
        conn.commit()
        return None
    now = _now()
    conn.execute(
        """
        UPDATE jobs
        SET status=?, started_at=?, ended_at=NULL, exit_code=NULL, error_summary=NULL
        WHERE id=? AND status=?
        """,
        (JOB_STATUS_RUNNING, now, row["id"], JOB_STATUS_QUEUED),
    )
    cur = conn.execute("SELECT changes()")
    changed = cur.fetchone()[0]
    cur.close()
    conn.commit()
    if not changed:
        return None
    return get_job(conn, row["id"])


def append_job_log(conn: sqlite3.Connection, job_id: int, line: str) -> int:
    ts = _now()
    cur = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM job_logs WHERE job_id=?",
        (job_id,),
    )
    next_seq = cur.fetchone()["next_seq"]
    cur.close()
    conn.execute(
        "INSERT INTO job_logs (job_id, seq, line, ts) VALUES (?, ?, ?, ?)",
        (job_id, next_seq, line, ts),
    )
    conn.execute("UPDATE jobs SET last_line_at=? WHERE id=?", (ts, job_id))
    conn.commit()
    return int(next_seq)


def get_job_logs_since(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    from_seq: int = 0,
    limit: int = 1000,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT job_id, seq, line, ts
        FROM job_logs
        WHERE job_id=? AND seq>?
        ORDER BY seq ASC
        LIMIT ?
        """,
        (job_id, from_seq, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def finish_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    success: bool,
    exit_code: int | None = None,
    error_summary: str | None = None,
) -> None:
    status = JOB_STATUS_COMPLETED if success else JOB_STATUS_FAILED
    conn.execute(
        """
        UPDATE jobs
        SET status=?, ended_at=?, exit_code=?, error_summary=?
        WHERE id=?
        """,
        (status, _now(), exit_code, error_summary, job_id),
    )
    conn.commit()


def cleanup_old_jobs(conn: sqlite3.Connection, *, keep_days: int = 30) -> int:
    cutoff = (datetime.now() - timedelta(days=max(1, keep_days))).isoformat(timespec="seconds")
    ph = ",".join("?" * len(JOB_TERMINAL_STATUSES))
    cur = conn.execute(
        f"""
        DELETE FROM jobs
        WHERE status IN ({ph})
          AND COALESCE(ended_at, created_at) < ?
        """,
        [*JOB_TERMINAL_STATUSES, cutoff],
    )
    conn.commit()
    return int(cur.rowcount or 0)


def requeue_running_jobs(
    conn: sqlite3.Connection,
    *,
    queue_key: str | None = None,
) -> list[int]:
    """Move orphaned ``running`` jobs back to ``queued`` and return job ids."""
    params: list = [JOB_STATUS_RUNNING]
    where = "status=?"
    if queue_key:
        where += " AND queue_key=?"
        params.append(queue_key)
    rows = conn.execute(f"SELECT id FROM jobs WHERE {where}", params).fetchall()
    if not rows:
        return []
    ids = [int(r["id"]) for r in rows]
    ph = ",".join("?" * len(ids))
    conn.execute(
        f"""
        UPDATE jobs
        SET status=?,
            started_at=NULL,
            ended_at=NULL,
            exit_code=NULL,
            error_summary=NULL
        WHERE id IN ({ph})
        """,
        [JOB_STATUS_QUEUED, *ids],
    )
    conn.commit()
    return ids


def cancel_queued_job(conn: sqlite3.Connection, job_id: int) -> bool:
    cur = conn.execute(
        """
        UPDATE jobs
        SET status=?, ended_at=?, error_summary=COALESCE(error_summary, 'cancelled by user')
        WHERE id=? AND status=?
        """,
        (JOB_STATUS_CANCELLED, _now(), job_id, JOB_STATUS_QUEUED),
    )
    conn.commit()
    return bool(cur.rowcount)


def mark_job_cancelled(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    reason: str = "cancelled by user",
) -> bool:
    cur = conn.execute(
        """
        UPDATE jobs
        SET status=?, ended_at=?, error_summary=?
        WHERE id=? AND status IN (?, ?)
        """,
        (JOB_STATUS_CANCELLED, _now(), reason, job_id, JOB_STATUS_QUEUED, JOB_STATUS_RUNNING),
    )
    conn.commit()
    return bool(cur.rowcount)
