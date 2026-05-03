#!/usr/bin/env python3
import asyncio
import importlib.util
import json
import os
import shutil
import sys
from urllib import parse, request as urlrequest

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

MANGA_ROOT = os.environ.get("MANGA_ROOT", "/manga")
CONFIG_FILENAME = ".mangadex.json"
SYNC_LOG = os.environ.get("SYNC_LOG", "/data/logs/sync.log")
MDEX_BASE = "https://api.mangadex.org"
CONTENT_RATINGS = ["safe", "suggestive", "erotica", "pornographic"]

_spec = importlib.util.spec_from_file_location("manga_fix", "/app/manga-fix.py")
_fix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fix)

sys.path.insert(0, "/app")
from sync_config import load_settings, save_settings  # noqa: E402
from kavita import KavitaClient                        # noqa: E402

app = FastAPI(title="mangadex-kavita-sync")
templates = Jinja2Templates(directory="/app/web/templates")

_sync_running = False


# ---------------------------------------------------------------------------
# Library helpers
# ---------------------------------------------------------------------------

def get_subdirs():
    """Immediate subdirectories of MANGA_ROOT, used as location options."""
    try:
        return sorted(
            d for d in os.listdir(MANGA_ROOT)
            if os.path.isdir(os.path.join(MANGA_ROOT, d)) and not d.startswith(".")
        )
    except OSError:
        return []


def get_all_series():
    series = []
    for root, subdirs, files in os.walk(MANGA_ROOT):
        subdirs.sort()
        if CONFIG_FILENAME in files:
            try:
                with open(os.path.join(root, CONFIG_FILENAME)) as f:
                    config = json.load(f)
            except Exception:
                config = {}
            chapters = set()
            for fname in os.listdir(root):
                if os.path.splitext(fname)[1].lower() in _fix.MANGA_EXTENSIONS:
                    m = _fix.CH_RE.search(fname)
                    if m:
                        chapters.add(float(m.group(1)))
            series.append({
                "path": os.path.relpath(root, MANGA_ROOT),
                "name": os.path.basename(root),
                "config": config,
                "last_chapter": max(chapters) if chapters else None,
                "chapter_count": len(chapters),
            })
            subdirs.clear()
    return sorted(series, key=lambda s: s["name"].lower())


# ---------------------------------------------------------------------------
# MangaDex API helpers
# ---------------------------------------------------------------------------

def _mdex_get(path: str, params: dict) -> dict:
    url = f"{MDEX_BASE}{path}?" + parse.urlencode(params, doseq=True)
    with urlrequest.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read())


def mdex_search(query: str):
    data = _mdex_get("/manga", {"title": query, "limit": 10,
                                "includes[]": "cover_art", "order[relevance]": "desc"})
    results = []
    for item in data.get("data", []):
        attr = item["attributes"]
        title = (attr.get("title") or {}).get("en") or next(iter((attr.get("title") or {}).values()), "Unknown")
        cover_url = None
        for rel in item.get("relationships", []):
            if rel["type"] == "cover_art":
                fname = (rel.get("attributes") or {}).get("fileName")
                if fname:
                    cover_url = f"https://uploads.mangadex.org/covers/{item['id']}/{fname}.256.jpg"
        results.append({"id": item["id"], "title": title, "cover_url": cover_url,
                        "status": attr.get("status", ""), "year": attr.get("year")})
    return results


def _lang_chapter_count(manga_id: str, lang: str) -> tuple[str, int]:
    data = _mdex_get(f"/manga/{manga_id}/feed",
                     {"translatedLanguage[]": lang, "limit": 0,
                      "contentRating[]": CONTENT_RATINGS})
    return lang, data.get("total", 0)


async def _fetch_groups(manga_id: str, language: str) -> dict:
    """Fetch all chapters, aggregate by group. Returns groups list + canonical chapter count."""
    loop = asyncio.get_event_loop()
    groups: dict[str, set] = {}
    offset, limit = 0, 100
    total = 1
    params = {"translatedLanguage[]": language, "limit": limit,
              "includes[]": "scanlation_group", "order[chapter]": "asc",
              "contentRating[]": CONTENT_RATINGS}

    while offset < total and offset < 500:
        params["offset"] = offset
        try:
            data = await loop.run_in_executor(None, lambda p=dict(params): _mdex_get(f"/manga/{manga_id}/feed", p))
        except Exception:
            break
        total = data.get("total", 0)
        items = data.get("data", [])
        for item in items:
            ch_str = item["attributes"].get("chapter")
            if not ch_str:
                continue
            try:
                ch_num = float(ch_str)
            except ValueError:
                continue
            for rel in item.get("relationships", []):
                if rel["type"] == "scanlation_group":
                    name = (rel.get("attributes") or {}).get("name") or "Unknown"
                    groups.setdefault(name, set()).add(ch_num)
        offset += len(items)
        if not items:
            break
        await asyncio.sleep(0.2)

    # Use the aggregate endpoint for the canonical chapter count — avoids
    # inflation from obscure groups with extra oddly-numbered chapters.
    try:
        def _agg():
            return _mdex_get(f"/manga/{manga_id}/aggregate", {"translatedLanguage[]": language})
        agg = await loop.run_in_executor(None, _agg)
        canonical: set[float] = set()
        for vol in agg.get("volumes", {}).values():
            for ch_key in vol.get("chapters", {}).keys():
                try:
                    canonical.add(float(ch_key))
                except ValueError:
                    pass
        total_unique = len(canonical)
    except Exception:
        total_unique = len(set().union(*groups.values())) if groups else 0

    # A group is "complete" if it has at least 97% of canonical chapters —
    # avoids penalising groups that skip one obscure special/cover chapter.
    threshold = max(total_unique - 2, int(total_unique * 0.97)) if total_unique else 0
    sorted_groups = sorted(
        [(name, len(chs), len(chs) >= threshold > 0) for name, chs in groups.items()],
        key=lambda x: x[1], reverse=True,
    )
    return {"groups": sorted_groups, "total_unique": total_unique}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="index.html",
        context={"series": get_all_series(), "active": "dashboard"})


@app.get("/series", response_class=HTMLResponse)
async def series_page(request: Request):
    return templates.TemplateResponse(request=request, name="series.html",
        context={"series": get_all_series(), "active": "series"})


@app.get("/sync", response_class=HTMLResponse)
async def sync_page(request: Request):
    return templates.TemplateResponse(request=request, name="sync.html",
        context={"active": "sync"})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse(request=request, name="settings.html",
        context={"settings": load_settings(), "active": "settings"})


@app.post("/api/settings")
async def save_settings_endpoint(
    file_format: str = Form("cbz"),
    chapter_naming: str = Form("%3 ch.%5"),
    volume_naming: str = Form("%3 vol.%4"),
    download_delay: float = Form(1.0),
    volume_mode: str = Form("false"),
    auto_scan: str = Form("false"),
    auto_covers: str = Form("false"),
    kavita_url: str = Form(""),
    kavita_api_key: str = Form(""),
):
    save_settings({
        "file_format": file_format,
        "chapter_naming": chapter_naming,
        "volume_naming": volume_naming,
        "download_delay": download_delay,
        "volume_mode": volume_mode == "true",
        "auto_scan": auto_scan == "true",
        "auto_covers": auto_covers == "true",
        "kavita_url": kavita_url.strip(),
        "kavita_api_key": kavita_api_key.strip(),
    })
    return RedirectResponse("/settings", status_code=303)


@app.post("/api/settings/test-kavita")
async def test_kavita(request: Request):
    body = await request.json()
    url = body.get("kavita_url", "").strip()
    key = body.get("kavita_api_key", "").strip()
    if not url or not key:
        return JSONResponse({"ok": False, "error": "URL and API key are required"})
    loop = asyncio.get_event_loop()
    try:
        client = KavitaClient(url, key)
        ok = await loop.run_in_executor(None, client.ping)
        return JSONResponse({"ok": ok, "error": None if ok else "No response from server"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/fix", response_class=HTMLResponse)
async def fix_page(request: Request):
    issues = _fix.scan(MANGA_ROOT)
    dup_groups = _fix.scan_duplicates(MANGA_ROOT)
    return templates.TemplateResponse(request=request, name="fix.html",
        context={"issues": issues, "dup_groups": dup_groups,
                 "active": "fix", "total": len(issues) + len(dup_groups)})


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    sync_lines = []
    if os.path.exists(SYNC_LOG):
        with open(SYNC_LOG) as f:
            sync_lines = list(reversed(f.readlines()[-200:]))
    fix_log_path = os.path.join(MANGA_ROOT, _fix.LOG_FILENAME)
    fix_log = {"renames": [], "deletes": []}
    if os.path.exists(fix_log_path):
        with open(fix_log_path) as f:
            fix_log = json.load(f)
    return templates.TemplateResponse(request=request, name="logs.html",
        context={"sync_lines": sync_lines,
                 "renames": list(reversed(fix_log.get("renames", [])))[:50],
                 "deletes": list(reversed(fix_log.get("deletes", [])))[:20],
                 "active": "logs"})


# ---------------------------------------------------------------------------
# API — search & manga info
# ---------------------------------------------------------------------------

@app.get("/api/search", response_class=HTMLResponse)
async def search(request: Request, q: str = ""):
    if len(q) < 2:
        return HTMLResponse("")
    try:
        results = mdex_search(q)
    except Exception as e:
        return HTMLResponse(f'<p class="text-red-500 text-sm">Search failed: {e}</p>')
    return templates.TemplateResponse(request=request, name="partials/search_results.html",
        context={"results": results})


@app.get("/api/manga/{manga_id}/setup", response_class=HTMLResponse)
async def get_manga_setup(request: Request, manga_id: str):
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, lambda: _mdex_get(
            f"/manga/{manga_id}", {"includes[]": "cover_art"}))
    except Exception as e:
        return HTMLResponse(f'<p class="text-red-500 text-sm">Could not load manga: {e}</p>')

    manga_data = data["data"]
    attr = manga_data["attributes"]
    title = (attr.get("title") or {}).get("en") or next(iter((attr.get("title") or {}).values()), "Unknown")
    status = attr.get("status", "unknown")
    year = attr.get("year")
    available_langs = attr.get("availableTranslatedLanguages") or []

    cover_url = None
    for rel in manga_data.get("relationships", []):
        if rel["type"] == "cover_art":
            fname = (rel.get("attributes") or {}).get("fileName")
            if fname:
                cover_url = f"https://uploads.mangadex.org/covers/{manga_id}/{fname}.256.jpg"

    # Fetch chapter counts per language in parallel (cap at 10 languages)
    lang_counts: dict[str, int] = {}
    if available_langs:
        tasks = [loop.run_in_executor(None, _lang_chapter_count, manga_id, lang)
                 for lang in available_langs[:10]]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, tuple) and r[1] > 0:
                lang_counts[r[0]] = r[1]
    lang_counts = dict(sorted(lang_counts.items(), key=lambda x: x[1], reverse=True))

    # Fetch groups for the most-translated language
    default_lang = next(iter(lang_counts), "en")
    groups_data = await _fetch_groups(manga_id, default_lang)

    return templates.TemplateResponse(request=request, name="partials/manga_setup.html",
        context={"manga_id": manga_id, "title": title, "status": status, "year": year,
                 "cover_url": cover_url, "lang_counts": lang_counts,
                 "default_lang": default_lang, "subdirs": get_subdirs(), **groups_data})


@app.get("/api/manga/{manga_id}/langs")
async def get_manga_langs(manga_id: str):
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, lambda: _mdex_get(
            f"/manga/{manga_id}", {}))
    except Exception as e:
        raise HTTPException(502, str(e))
    available_langs = data["data"]["attributes"].get("availableTranslatedLanguages") or []
    tasks = [loop.run_in_executor(None, _lang_chapter_count, manga_id, lang)
             for lang in available_langs[:10]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    counts = sorted(
        [(lang, cnt) for r in results if isinstance(r, tuple) for lang, cnt in [r] if cnt > 0],
        key=lambda x: x[1], reverse=True,
    )
    from fastapi.responses import JSONResponse
    return JSONResponse(counts)


@app.get("/api/manga/{manga_id}/groups", response_class=HTMLResponse)
async def get_manga_groups(request: Request, manga_id: str, language: str = "en"):
    groups_data = await _fetch_groups(manga_id, language)
    return templates.TemplateResponse(request=request, name="partials/group_picker.html",
        context=groups_data)


# ---------------------------------------------------------------------------
# API — series CRUD
# ---------------------------------------------------------------------------

@app.post("/api/series")
async def add_series(
    manga_id: str = Form(...),
    title: str = Form(...),
    subfolder: str = Form(""),
    language: str = Form("en"),
    translator: str = Form(""),
    since: str = Form("0"),
):
    parts = [p for p in [subfolder.strip("/"), title] if p]
    series_dir = os.path.join(MANGA_ROOT, *parts)
    os.makedirs(series_dir, exist_ok=True)
    config = {"id": manga_id, "language": language}
    if translator.strip():
        config["translator"] = translator.strip()
    try:
        config["since"] = float(since) if since else 0
    except ValueError:
        config["since"] = 0
    with open(os.path.join(series_dir, CONFIG_FILENAME), "w") as f:
        json.dump(config, f, indent=2)
    return RedirectResponse("/series", status_code=303)


@app.delete("/api/series/{path:path}", response_class=HTMLResponse)
async def delete_series(path: str):
    config_path = os.path.join(MANGA_ROOT, path, CONFIG_FILENAME)
    if not os.path.exists(config_path):
        raise HTTPException(404, "Series not found")
    os.remove(config_path)
    return HTMLResponse("")


@app.get("/api/series/{path:path}/edit", response_class=HTMLResponse)
async def edit_series_form(request: Request, path: str):
    config_path = os.path.join(MANGA_ROOT, path, CONFIG_FILENAME)
    if not os.path.exists(config_path):
        raise HTTPException(404, "Series not found")
    with open(config_path) as f:
        config = json.load(f)

    parts = path.split("/")
    folder_name = parts[-1]
    subfolder = "/".join(parts[:-1])

    return templates.TemplateResponse(request=request, name="partials/series_edit.html",
        context={"path": path, "manga_id": config.get("id", ""),
                 "manga_title": folder_name,
                 "current_lang": config.get("language", "en"),
                 "current_translator": config.get("translator", ""),
                 "current_since": config.get("since", 0),
                 "folder_name": folder_name, "subfolder": subfolder,
                 "subdirs": get_subdirs()})


@app.put("/api/series/{path:path}")
async def update_series(
    path: str,
    manga_id: str = Form(...),
    title: str = Form(...),
    subfolder: str = Form(""),
    language: str = Form("en"),
    translator: str = Form(""),
    since: str = Form("0"),
):
    old_dir = os.path.join(MANGA_ROOT, path)
    if not os.path.exists(os.path.join(old_dir, CONFIG_FILENAME)):
        raise HTTPException(404, "Series not found")

    parts = [p for p in [subfolder.strip("/"), title] if p]
    new_dir = os.path.join(MANGA_ROOT, *parts)

    if os.path.abspath(old_dir) != os.path.abspath(new_dir):
        os.makedirs(os.path.dirname(new_dir) or MANGA_ROOT, exist_ok=True)
        shutil.move(old_dir, new_dir)

    config = {"id": manga_id, "language": language}
    if translator.strip():
        config["translator"] = translator.strip()
    try:
        config["since"] = float(since) if since else 0
    except ValueError:
        config["since"] = 0
    with open(os.path.join(new_dir, CONFIG_FILENAME), "w") as f:
        json.dump(config, f, indent=2)

    return Response(status_code=204, headers={"HX-Redirect": "/series"})


# ---------------------------------------------------------------------------
# API — sync
# ---------------------------------------------------------------------------

@app.get("/api/sync/stream")
async def sync_stream():
    global _sync_running

    async def generate():
        global _sync_running
        if _sync_running:
            yield "data: ⚠ Sync already in progress\n\n"
            yield "data: [done]\n\n"
            return
        _sync_running = True
        try:
            proc = await asyncio.create_subprocess_exec(
                "python3", "/app/manga-sync.py",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "MANGA_ROOT": MANGA_ROOT},
            )
            async for line in proc.stdout:
                text = line.decode().strip()
                if text:
                    yield f"data: {text}\n\n"
            await proc.wait()
        finally:
            _sync_running = False
        yield "data: [done]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# API — fix
# ---------------------------------------------------------------------------

@app.post("/api/fix/apply", response_class=HTMLResponse)
async def apply_fix(
    old_path: str = Form(...),
    new_name: str = Form(...),
    issue_name: str = Form(...),
):
    log_path = os.path.join(MANGA_ROOT, _fix.LOG_FILENAME)
    log_data = _fix.load_log(log_path)
    try:
        _fix.do_rename(old_path, new_name, issue_name, log_data, log_path)
    except Exception as e:
        raise HTTPException(500, str(e))
    return HTMLResponse("")


@app.post("/api/fix/apply-dup", response_class=HTMLResponse)
async def apply_dup(keep_path: str = Form(...), delete_paths: str = Form(...),
                    needs_rename: str = Form(""), keep_name: str = Form(...)):
    log_path = os.path.join(MANGA_ROOT, _fix.LOG_FILENAME)
    log_data = _fix.load_log(log_path)
    sizes = {p: os.path.getsize(p) for p in [keep_path] + [x for x in delete_paths.split("|") if x]
             if os.path.exists(p)}
    group = {"keep_path": keep_path, "keep_name": keep_name,
              "needs_rename": needs_rename == "true",
              "delete_paths": [x for x in delete_paths.split("|") if x], "sizes": sizes}
    try:
        _fix.apply_dup_group(group, log_data, log_path)
    except Exception as e:
        raise HTTPException(500, str(e))
    return HTMLResponse("")
