#!/usr/bin/env python3
"""manga-sync — download new MangaDex chapters for tracked series.

Config file (.mangadex.json) per series directory:
  {
    "id":         "5e3a710f-0b0d-482b-9e84-d9c91960c625",
    "language":   "en",
    "translator": "Sho Habby Scans",
    "since":      207   # skip chapters/volumes <= this on first run
  }
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

MANGA_ROOT = os.environ.get("MANGA_ROOT", "/manga")
DEFAULT_LANGUAGE = os.environ.get("DEFAULT_LANGUAGE", "en")
CONFIG_FILENAME = ".mangadex.json"
SYNC_LOG = os.environ.get("SYNC_LOG", "/data/logs/sync.log")
SYNC_LOG_MAX_LINES = 5000
MDEX_BASE = "https://api.mangadex.org"
CONTENT_RATINGS = ["safe", "suggestive", "erotica", "pornographic"]

_here = os.path.dirname(os.path.abspath(__file__))

# Import manga-fix.py (hyphen in name prevents normal import)
_spec = importlib.util.spec_from_file_location("manga_fix", os.path.join(_here, "manga-fix.py"))
_fix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fix)

CH_RE = _fix.CH_RE
MANGA_EXTENSIONS = _fix.MANGA_EXTENSIONS

# Import shared settings + Kavita client
sys.path.insert(0, _here)
from sync_config import load_settings  # noqa: E402
from kavita import KavitaClient        # noqa: E402


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _rotate_log():
    try:
        with open(SYNC_LOG) as f:
            lines = f.readlines()
        if len(lines) > SYNC_LOG_MAX_LINES:
            with open(SYNC_LOG, "w") as f:
                f.writelines(lines[-SYNC_LOG_MAX_LINES:])
    except FileNotFoundError:
        pass


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
    dirs = []
    for root, subdirs, files in os.walk(MANGA_ROOT):
        subdirs.sort()
        if CONFIG_FILENAME in files:
            dirs.append(root)
            subdirs.clear()
    return dirs


def load_config(series_dir):
    path = os.path.join(series_dir, CONFIG_FILENAME)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def chapters_on_disk(series_dir):
    nums = set()
    for fname in os.listdir(series_dir):
        if os.path.splitext(fname)[1].lower() not in MANGA_EXTENSIONS:
            continue
        m = CH_RE.search(fname)
        if m:
            nums.add(float(m.group(1)))
    return nums


def volumes_on_disk(series_dir):
    nums = set()
    for fname in os.listdir(series_dir):
        if os.path.splitext(fname)[1].lower() not in MANGA_EXTENSIONS:
            continue
        m = re.search(r"vol\.?\s*(\d+)", fname, re.IGNORECASE)
        if m:
            nums.add(int(m.group(1)))
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
    __slots__ = ("ch_str", "ch_num", "volume", "group")

    def __init__(self, data):
        attr = data["attributes"]
        self.ch_str = attr.get("chapter") or "0"
        self.ch_num = float(self.ch_str)
        self.volume = attr.get("volume")
        self.group = _group_name(data)


def _feed(manga_id, lang, params_extra=None):
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
    manga_id = config["id"]
    lang = config.get("language", DEFAULT_LANGUAGE)
    translator = config.get("translator")
    seen = {}
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


def fetch_new_volumes(config, after_vol):
    """Return sorted list of volume numbers > after_vol from the aggregate endpoint."""
    manga_id = config["id"]
    lang = config.get("language", DEFAULT_LANGUAGE)
    data = _api_get(f"/manga/{manga_id}/aggregate", {"translatedLanguage[]": lang})
    new_vols = []
    for vol_key in data.get("volumes", {}).keys():
        if vol_key == "none":
            continue
        try:
            vol_num = int(float(vol_key))
        except ValueError:
            continue
        if vol_num > after_vol:
            new_vols.append(vol_num)
    return sorted(set(new_vols))


def fetch_volume_chapters(config, vol_str):
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


def fetch_volume_covers(manga_id):
    """Return dict mapping volume string → MangaDex cover image URL."""
    data = _api_get("/cover", {"manga[]": manga_id, "limit": 100, "order[volume]": "asc"})
    covers = {}
    for item in data.get("data", []):
        attr = item["attributes"]
        vol = attr.get("volume")
        fname = attr.get("fileName")
        if vol and fname:
            covers[vol] = f"https://uploads.mangadex.org/covers/{manga_id}/{fname}.512.jpg"
    return covers


# ---------------------------------------------------------------------------
# mdx download
# ---------------------------------------------------------------------------

def mdx_download_chapter(ch, config, series_dir, settings):
    manga_id = config["id"]
    lang = config.get("language", DEFAULT_LANGUAGE)
    translator = config.get("translator")
    cmd = [
        "mdx", "dl",
        "-e", settings.get("file_format", "cbz"),
        "-c", ch.ch_str,
        "-l", lang,
        "-o", series_dir,
        "--file-name", settings.get("chapter_naming", "%3 ch.%5"),
        f"https://mangadex.org/title/{manga_id}",
    ]
    if translator:
        cmd += ["-t", translator]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        _log(f"  [warn] mdx ch.{ch.ch_str}: {result.stderr.strip() or result.stdout.strip()}")
        return False
    return True


def mdx_download_volume(vol_num, config, series_dir, settings):
    manga_id = config["id"]
    lang = config.get("language", DEFAULT_LANGUAGE)
    translator = config.get("translator")
    cmd = [
        "mdx", "dl",
        "-e", settings.get("file_format", "cbz"),
        "-v", str(vol_num),
        "-l", lang,
        "-o", series_dir,
        "--file-name", settings.get("volume_naming", "%3 vol.%4"),
        f"https://mangadex.org/title/{manga_id}",
    ]
    if translator:
        cmd += ["-t", translator]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        _log(f"  [warn] mdx vol.{vol_num}: {result.stderr.strip() or result.stdout.strip()}")
        return False
    return True


# ---------------------------------------------------------------------------
# Volume completion (chapter mode only)
# ---------------------------------------------------------------------------

def apply_volume_completions(series_dir, config, downloaded):
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
        if not api_nums or not api_nums.issubset(on_disk):
            continue

        vol_label = int(float(vol_str))
        for fname in sorted(os.listdir(series_dir)):
            if os.path.splitext(fname)[1].lower() not in MANGA_EXTENSIONS:
                continue
            m = CH_RE.search(fname)
            if not m or float(m.group(1)) not in api_nums:
                continue
            if re.search(r"vol\.", fname, re.IGNORECASE):
                continue
            new_name = re.sub(r"(ch\.)", f"vol. {vol_label} \\1", fname, count=1)
            _fix.do_rename(os.path.join(series_dir, fname), new_name,
                           "add_volume", log_data, log_path)
            renames += 1
        time.sleep(0.3)

    return renames


def run_fix_pass(series_dir):
    log_path = os.path.join(MANGA_ROOT, _fix.LOG_FILENAME)
    log_data = _fix.load_log(log_path)
    for fpath, issue_name, new_name in _fix.scan(series_dir):
        _fix.do_rename(fpath, new_name, issue_name, log_data, log_path)


# ---------------------------------------------------------------------------
# Kavita post-sync actions
# ---------------------------------------------------------------------------

def kavita_set_covers(client, series_dir, config):
    series_name = os.path.basename(series_dir)
    if not config.get("id"):
        _log(f"[kavita] skipping covers for {series_name}: no MangaDex ID in config")
        return
    try:
        covers = fetch_volume_covers(config["id"])
        if not covers:
            return
        series = client.search_series(series_name)
        if not series:
            _log(f"[kavita] series not found in Kavita: {series_name}")
            return
        volumes = client.get_volumes(series["id"])
        for vol in volumes:
            vol_num = str(vol.get("number", ""))
            image_url = covers.get(vol_num)
            if not image_url:
                continue
            try:
                client.set_volume_cover(vol["id"], image_url)
                _log(f"[kavita] cover set: {series_name} vol.{vol_num}")
            except Exception as e:
                _log(f"[kavita] cover failed vol.{vol_num}: {e}")
    except Exception as e:
        _log(f"[kavita] covers error for {series_name}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(series_filter=None, covers_only=False):
    _rotate_log()
    settings = load_settings()
    volume_mode = settings.get("volume_mode", False)
    delay = float(settings.get("download_delay", 1.0))

    kavita_client = None
    kavita_url = settings.get("kavita_url", "").strip()
    kavita_key = settings.get("kavita_api_key", "").strip()
    if kavita_url and kavita_key and (settings.get("auto_scan") or settings.get("auto_covers") or covers_only):
        kavita_client = KavitaClient(kavita_url, kavita_key)

    dirs = [series_filter] if series_filter else all_series_dirs()

    if covers_only:
        for series_dir in dirs:
            config = load_config(series_dir)
            if config and kavita_client:
                kavita_set_covers(kavita_client, series_dir, config)
        return

    any_downloaded = False

    for series_dir in dirs:
        config = load_config(series_dir)
        if not config:
            continue

        series_name = os.path.basename(series_dir)
        since = float(config.get("since", 0))

        if volume_mode:
            vols_on_disk = volumes_on_disk(series_dir)
            last_vol = max(vols_on_disk) if vols_on_disk else 0
            effective_last_vol = max(last_vol, int(since))

            try:
                new_vols = fetch_new_volumes(config, after_vol=effective_last_vol)
            except Exception as e:
                _log(f"[{series_name}] API error: {e}")
                continue

            if not new_vols:
                _log(f"[{series_name}] up-to-date (last vol={effective_last_vol})")
                continue

            downloaded_vols = 0
            for vol_num in new_vols:
                _log(f"[{series_name}] vol.{vol_num}: downloading…")
                if mdx_download_volume(vol_num, config, series_dir, settings):
                    downloaded_vols += 1
                    any_downloaded = True
                time.sleep(delay)

            if downloaded_vols:
                run_fix_pass(series_dir)
                if kavita_client and settings.get("auto_covers"):
                    kavita_set_covers(kavita_client, series_dir, config)

            _log(f"[{series_name}] done downloaded={downloaded_vols} volumes "
                 f"(vol {effective_last_vol} → {max(new_vols)})")

        else:
            on_disk = chapters_on_disk(series_dir)
            last_ch = max(on_disk) if on_disk else 0.0
            effective_last = max(last_ch, since)

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

            vol_groups: dict = {}
            for ch in new_chapters:
                if ch.ch_num not in on_disk:
                    vol_groups.setdefault(ch.volume, []).append(ch)

            sorted_vols = sorted(
                vol_groups.keys(),
                key=lambda v: (v is None, float(v) if v is not None else 0),
            )

            total_downloaded = 0
            total_vol_renames = 0

            for vol_key in sorted_vols:
                chs = vol_groups[vol_key]
                vol_label = f"vol.{int(float(vol_key))}" if vol_key is not None else "unassigned"
                _log(f"[{series_name}] {vol_label}: downloading {len(chs)} chapters…")

                downloaded_vol = []
                for ch in chs:
                    if mdx_download_chapter(ch, config, series_dir, settings):
                        downloaded_vol.append(ch)
                        on_disk.add(ch.ch_num)
                    time.sleep(delay)

                total_downloaded += len(downloaded_vol)

                if downloaded_vol:
                    try:
                        renames = apply_volume_completions(series_dir, config, downloaded_vol)
                        total_vol_renames += renames
                    except Exception as e:
                        _log(f"[{series_name}] {vol_label}: completion error: {e}")

                _log(f"[{series_name}] {vol_label}: {len(downloaded_vol)}/{len(chs)} downloaded")

            if total_downloaded:
                any_downloaded = True
                run_fix_pass(series_dir)
                if kavita_client and settings.get("auto_covers"):
                    kavita_set_covers(kavita_client, series_dir, config)

            _log(
                f"[{series_name}] done downloaded={total_downloaded} "
                f"({effective_last} → {latest.ch_num}) vol_renames={total_vol_renames}"
            )

    if any_downloaded and kavita_client and settings.get("auto_scan"):
        try:
            kavita_client.scan_all()
            _log("[kavita] library scan triggered")
        except Exception as e:
            _log(f"[kavita] scan failed: {e}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--series", help="Absolute path to a single series directory")
    parser.add_argument("--covers-only", action="store_true")
    args = parser.parse_args()
    main(series_filter=args.series, covers_only=args.covers_only)
