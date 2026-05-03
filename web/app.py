#!/usr/bin/env python3
import asyncio
import importlib.util
import json
import os
from urllib import parse, request as urlrequest

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

MANGA_ROOT = os.environ.get("MANGA_ROOT", "/manga")
CONFIG_FILENAME = ".mangadex.json"
SYNC_LOG = os.environ.get("SYNC_LOG", "/logs/.sync.log")
MDEX_BASE = "https://api.mangadex.org"

_spec = importlib.util.spec_from_file_location("manga_fix", "/app/manga-fix.py")
_fix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fix)

app = FastAPI(title="mangadex-kavita-sync")
templates = Jinja2Templates(directory="/app/web/templates")

_sync_running = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def mdex_search(query: str):
    url = f"{MDEX_BASE}/manga?" + parse.urlencode({
        "title": query, "limit": 10,
        "includes[]": "cover_art",
        "order[relevance]": "desc",
    }, doseq=True)
    with urlrequest.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read())
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
        results.append({
            "id": item["id"],
            "title": title,
            "cover_url": cover_url,
            "status": attr.get("status", ""),
            "year": attr.get("year"),
        })
    return results


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
# API
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


@app.post("/api/series")
async def add_series(
    manga_id: str = Form(...),
    title: str = Form(...),
    language: str = Form("en"),
    translator: str = Form(""),
    since: str = Form("0"),
):
    series_dir = os.path.join(MANGA_ROOT, title)
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
async def apply_dup(keep_path: str = Form(...), delete_paths: str = Form(...), needs_rename: str = Form(""), keep_name: str = Form(...)):
    log_path = os.path.join(MANGA_ROOT, _fix.LOG_FILENAME)
    log_data = _fix.load_log(log_path)
    sizes = {}
    for p in [keep_path] + [x for x in delete_paths.split("|") if x]:
        if os.path.exists(p):
            sizes[p] = os.path.getsize(p)
    group = {
        "keep_path": keep_path,
        "keep_name": keep_name,
        "needs_rename": needs_rename == "true",
        "delete_paths": [x for x in delete_paths.split("|") if x],
        "sizes": sizes,
    }
    try:
        _fix.apply_dup_group(group, log_data, log_path)
    except Exception as e:
        raise HTTPException(500, str(e))
    return HTMLResponse("")
