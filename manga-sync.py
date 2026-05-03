#!/usr/bin/env python3
"""manga-sync — download new MangaDex chapters for tracked series.

Config file (.mangadex.json) per series directory (optional — no file = skip):
  {
    "id":         "5e3a710f-0b0d-482b-9e84-d9c91960c625",  # MangaDex title UUID
    "language":   "en",                                      # default "en"
    "translator": "Sho Habby Scans",                        # optional group filter
    "since":      207                                        # skip chapters <= this
  }

  Use "since" to avoid downloading a full back-catalogue when first adding a series.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from urllib import error as urlerror
from urllib import parse, request

MANGA_ROOT = os.environ.get("MANGA_ROOT", os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LANGUAGE = os.environ.get("DEFAULT_LANGUAGE", "en")
CONFIG_FILENAME = ".mangadex.json"
SYNC_LOG = os.environ.get("SYNC_LOG", os.path.join(MANGA_ROOT, ".sync.log"))
MDEX_BASE = "https://api.mangadex.org"
CONTENT_RATINGS = ["safe", "suggestive", "erotica", "pornographic"]

# ---------------------------------------------------------------------------
# Import shared utilities from manga-fix.py (hyphen in name, use importlib)
# ---------------------------------------------------------------------------
_spec = importlib.util.spec_from_file_location(
    "manga_fix", os.path.join(os.path.dirname(os.path.abspath(__file__)), "manga-fix.py")
)
_fix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fix)

CH_RE = _fix.CH_RE
MANGA_EXTENSIONS = _fix.MANGA_EXTENSIONS


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(msg):
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    try:
        with open(SYNC_LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------

def all_series_dirs():
    """Find every directory under MANGA_ROOT that contains a .mangadex.json."""
    dirs = []
    for root, subdirs, files in os.walk(MANGA_ROOT):
        subdirs.sort()
        if CONFIG_FILENAME in files:
            dirs.append(root)
            subdirs.clear()  # don't recurse into a matched series dir
    return dirs


def load_config(series_dir):
    path = os.path.join(series_dir, CONFIG_FILENAME)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def chapters_on_disk(series_dir):
    """Return set of chapter numbers (float) present in the series directory."""
    nums = set()
    for fname in os.listdir(series_dir):
        if os.path.splitext(fname)[1].lower() not in MANGA_EXTENSIONS:
            continue
        m = CH_RE.search(fname)
        if m:
            nums.add(float(m.group(1)))
    return nums


# ---------------------------------------------------------------------------
# MangaDex API
# ---------------------------------------------------------------------------

def _api_get(path, params):
    url = f"{MDEX_BASE}{path}?{parse.urlencode(params, doseq=True)}"
    try:
        with request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read())
    except urlerror.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} for {url}") from e


def _group_name(chapter_data):
    for rel in chapter_data.get("relationships", []):
        if rel["type"] == "scanlation_group":
            return rel.get("attributes", {}).get("name", "")
    return ""


def _translator_match(chapter_data, translator):
    if not translator:
        return True
    return translator.lower() in _group_name(chapter_data).lower()


class _Ch:
    """Lightweight chapter record from the MangaDex feed."""
    __slots__ = ("ch_str", "ch_num", "volume", "group")

    def __init__(self, data):
        attr = data["attributes"]
        self.ch_str = attr.get("chapter") or "0"
        self.ch_num = float(self.ch_str)
        self.volume = attr.get("volume")   # str like "14" or None
        self.group = _group_name(data)


def _feed(manga_id, lang, params_extra=None):
    """Paginate /manga/{id}/feed and yield raw chapter data dicts."""
    params_base = {
        "translatedLanguage[]": lang,
        "limit": 100,
        "includes[]": "scanlation_group",
        "contentRating[]": CONTENT_RATINGS,
    }
    if params_extra:
        params_base.update(params_extra)
    offset = 0
    while True:
        params_base["offset"] = offset
        data = _api_get(f"/manga/{manga_id}/feed", params_base)
        items = data.get("data", [])
        total = data.get("total", 0)
        yield from items
        offset += len(items)
        if offset >= total or not items:
            break
        time.sleep(0.4)


def fetch_latest_chapter(config):
    """Return the newest _Ch for this config (translator-filtered), or None."""
    manga_id = config["id"]
    lang = config.get("language", DEFAULT_LANGUAGE)
    translator = config.get("translator")
    data = _api_get(f"/manga/{manga_id}/feed", {
        "translatedLanguage[]": lang,
        "order[chapter]": "desc",
        "limit": 10,
        "includes[]": "scanlation_group",
        "contentRating[]": CONTENT_RATINGS,
    })
    for item in data.get("data", []):
        if not item["attributes"].get("chapter"):
            continue
        if not _translator_match(item, translator):
            continue
        return _Ch(item)
    return None


def fetch_chapters_after(config, after):
    """Return sorted list of _Ch with ch_num > after, deduplicated by chapter number."""
    manga_id = config["id"]
    lang = config.get("language", DEFAULT_LANGUAGE)
    translator = config.get("translator")
    seen = {}   # ch_num -> _Ch (keep first translator-match per chapter number)
    for item in _feed(manga_id, lang, {"order[chapter]": "asc"}):
        ch_str = item["attributes"].get("chapter")
        if not ch_str:
            continue
        try:
            ch_num = float(ch_str)
        except ValueError:
            continue
        if ch_num <= after:
            continue
        if not _translator_match(item, translator):
            continue
        if ch_num not in seen:
            seen[ch_num] = _Ch(item)
    return sorted(seen.values(), key=lambda c: c.ch_num)


def fetch_volume_chapters(config, vol_str):
    """Return set of chapter numbers (float) that belong to the given volume."""
    manga_id = config["id"]
    lang = config.get("language", DEFAULT_LANGUAGE)
    translator = config.get("translator")
    nums = set()
    for item in _feed(manga_id, lang, {"order[chapter]": "asc"}):
        attr = item["attributes"]
        if attr.get("volume") != vol_str:
            continue
        ch_str = attr.get("chapter")
        if not ch_str:
            continue
        if not _translator_match(item, translator):
            continue
        try:
            nums.add(float(ch_str))
        except ValueError:
            pass
    return nums


# ---------------------------------------------------------------------------
# mdx download
# ---------------------------------------------------------------------------

def mdx_download(ch, config, series_dir):
    manga_id = config["id"]
    lang = config.get("language", DEFAULT_LANGUAGE)
    translator = config.get("translator")
    cmd = [
        "mdx", "dl",
        "-e", "cbz",
        "-c", ch.ch_str,
        "-l", lang,
        "-o", series_dir,
        "--file-name", "%3 ch.%5",
        f"https://mangadex.org/title/{manga_id}",
    ]
    if translator:
        cmd += ["-t", translator]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        _log(f"  [warn] mdx ch.{ch.ch_str}: {result.stderr.strip() or result.stdout.strip()}")
        return False
    return True


# ---------------------------------------------------------------------------
# Volume completion — rename ch.N files to vol.V ch.N when a volume is full
# ---------------------------------------------------------------------------

def apply_volume_completions(series_dir, config, downloaded):
    """For each volume newly touched, check if complete and rename if so."""
    volumes = {ch.volume for ch in downloaded if ch.volume is not None}
    if not volumes:
        return 0

    log_path = os.path.join(MANGA_ROOT, _fix.LOG_FILENAME)
    log_data = _fix.load_log(log_path)
    on_disk = chapters_on_disk(series_dir)
    renames = 0

    for vol_str in sorted(volumes):
        try:
            api_nums = fetch_volume_chapters(config, vol_str)
        except Exception as e:
            _log(f"  [warn] volume {vol_str} API error: {e}")
            continue
        if not api_nums:
            continue
        if not api_nums.issubset(on_disk):
            continue  # volume incomplete — don't rename yet

        vol_label = int(float(vol_str))
        for fname in sorted(os.listdir(series_dir)):
            if os.path.splitext(fname)[1].lower() not in MANGA_EXTENSIONS:
                continue
            m = CH_RE.search(fname)
            if not m or float(m.group(1)) not in api_nums:
                continue
            if re.search(r"vol\.", fname, re.IGNORECASE):
                continue  # already has a volume label
            new_name = re.sub(r"(ch\.)", f"vol. {vol_label} \\1", fname, count=1)
            _fix.do_rename(os.path.join(series_dir, fname), new_name,
                           "add_volume", log_data, log_path)
            renames += 1
        time.sleep(0.3)

    return renames


# ---------------------------------------------------------------------------
# Post-download fix pass (reuse manga-fix scan+rename)
# ---------------------------------------------------------------------------

def run_fix_pass(series_dir):
    log_path = os.path.join(MANGA_ROOT, _fix.LOG_FILENAME)
    log_data = _fix.load_log(log_path)
    for fpath, issue_name, new_name in _fix.scan(series_dir):
        _fix.do_rename(fpath, new_name, issue_name, log_data, log_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    for series_dir in all_series_dirs():
        config = load_config(series_dir)
        if not config:
            continue

        series_name = os.path.basename(series_dir)
        on_disk = chapters_on_disk(series_dir)
        last_ch = max(on_disk) if on_disk else 0.0

        # "since" lets the user skip the back-catalogue on first run
        since = float(config.get("since", 0))
        effective_last = max(last_ch, since)

        if last_ch == 0 and since == 0:
            _log(f"[{series_name}] no chapters on disk and no 'since' in config — skipping "
                 f"(add \"since\": N to start from chapter N)")
            continue

        try:
            latest = fetch_latest_chapter(config)
        except Exception as e:
            _log(f"[{series_name}] API error: {e}")
            continue

        if latest is None or latest.ch_num <= effective_last:
            _log(f"[{series_name}] up-to-date (last={effective_last})")
            continue

        try:
            new_chapters = fetch_chapters_after(config, after=effective_last)
        except Exception as e:
            _log(f"[{series_name}] API error (feed): {e}")
            continue

        downloaded = []
        for ch in new_chapters:
            if ch.ch_num in on_disk:
                continue
            if mdx_download(ch, config, series_dir):
                downloaded.append(ch)
                on_disk.add(ch.ch_num)
            time.sleep(1.0)

        vol_renames = 0
        if downloaded:
            try:
                vol_renames = apply_volume_completions(series_dir, config, downloaded)
            except Exception as e:
                _log(f"[{series_name}] volume completion error: {e}")
            run_fix_pass(series_dir)

        _log(
            f"[{series_name}] downloaded={len(downloaded)} "
            f"({effective_last} → {latest.ch_num}) "
            f"vol_renames={vol_renames}"
        )


if __name__ == "__main__":
    main()
