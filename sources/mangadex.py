"""MangaDex source adapter.

Consolidates all MangaDex API calls previously scattered across manga-sync.py
and web/app.py into a single, testable class.
"""
import json
import time
from urllib import error as urlerror, parse, request

from .base import MangaResult, SeriesMetadata

MDEX_BASE       = "https://api.mangadex.org"
MDEX_COVERS     = "https://uploads.mangadex.org/covers"
CONTENT_RATINGS = ["safe", "suggestive", "erotica", "pornographic"]

_UA = "manga-sync/1.0"


# ---------------------------------------------------------------------------
# Module-level helpers (used internally and by manga-sync.py helpers)
# ---------------------------------------------------------------------------

def _group_name_from_rel(chapter_data: dict) -> str:
    for rel in chapter_data.get("relationships", []):
        if rel["type"] == "scanlation_group":
            return rel.get("attributes", {}).get("name", "") or ""
    return ""


def _tankobon_count_from_aggregate(agg) -> int:
    """Keys ``none`` and ``0`` are not numbered tankōbon volumes."""
    if not agg:
        return 0
    vols = agg.get("volumes") or {}
    return len([k for k in vols if k not in ("none", "0")])


def _parse_last_volume(attr: dict | None) -> int:
    if not attr:
        return 0
    raw = (attr.get("lastVolume") or "").strip()
    if not raw:
        return 0
    try:
        n = int(float(raw))
        return n if n > 0 else 0
    except (ValueError, TypeError):
        return 0


class _ChapterData:
    """Parsed chapter entry from the MangaDex feed."""
    __slots__ = (
        "ch_id", "ch_str", "ch_num", "volume", "group", "title", "publish_date",
        "external_url",
    )

    def __init__(self, data: dict):
        attr = data["attributes"]
        self.ch_id        = data["id"]
        self.ch_str       = attr.get("chapter") or "0"
        self.ch_num       = float(self.ch_str)
        self.volume       = attr.get("volume")
        self.group        = _group_name_from_rel(data)
        self.title        = (attr.get("title") or "").strip() or None
        self.publish_date = (attr.get("publishAt") or "")[:10] or None
        ex = (attr.get("externalUrl") or "").strip()
        self.external_url = ex or None

    @property
    def is_mangadex_hosted(self) -> bool:
        """True when ``externalUrl`` is unset (chapter metadata still lives on MangaDex)."""
        return self.external_url is None


# ---------------------------------------------------------------------------
# Source adapter
# ---------------------------------------------------------------------------

class MangaDexSource:
    """Implements the Source protocol for MangaDex."""

    key  = "mangadex"
    name = "MangaDex"
    lang = "mul"  # multilingual

    # ------------------------------------------------------------------
    # HTTP transport
    # ------------------------------------------------------------------

    def _api_get(self, path: str, params: dict, timeout: int = 30) -> dict:
        url = f"{MDEX_BASE}{path}?" + parse.urlencode(params, doseq=True)
        req = request.Request(url, headers={"User-Agent": _UA})
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urlerror.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} for {url}") from e

    # ------------------------------------------------------------------
    # Protocol: search
    # ------------------------------------------------------------------

    def search(self, query: str) -> list[MangaResult]:
        data = self._api_get("/manga", {
            "title": query, "limit": 10,
            "includes[]": "cover_art", "order[relevance]": "desc",
        }, timeout=15)
        results: list[MangaResult] = []
        for item in data.get("data", []):
            attr  = item["attributes"]
            title = (attr.get("title") or {}).get("en") or \
                    next(iter((attr.get("title") or {}).values()), "Unknown")
            fname = None
            for rel in item.get("relationships", []):
                if rel["type"] == "cover_art":
                    fname = (rel.get("attributes") or {}).get("fileName")
            # thumbnail_url uses the web-app proxy path so the browser can
            # load it without a Referer header (MangaDex CDN requires one).
            thumbnail_url = f"/api/proxy/cover/{item['id']}/{fname}" if fname else None
            results.append(MangaResult(
                source_key="mangadex",
                source_name="MangaDex",
                manga_id=item["id"],
                title=title,
                thumbnail_url=thumbnail_url,
                status=attr.get("status", ""),
                year=attr.get("year"),
            ))
        return results

    # ------------------------------------------------------------------
    # Protocol: metadata
    # ------------------------------------------------------------------

    def get_metadata(self, manga_id: str) -> SeriesMetadata:
        data = self._api_get(f"/manga/{manga_id}", {
            "includes[]": ["author", "artist", "cover_art"],
        })
        attr = data["data"]["attributes"]

        titles = attr.get("title") or {}
        title  = titles.get("en") or next(iter(titles.values()), "")

        desc_map    = attr.get("description") or {}
        description = desc_map.get("en") or next(iter(desc_map.values()), "") or None

        tags = [
            t["attributes"]["name"]["en"]
            for t in (attr.get("tags") or [])
            if t.get("attributes", {}).get("name", {}).get("en")
        ]

        authors, artists = [], []
        cover_filename   = None
        for rel in data["data"].get("relationships", []):
            rtype = rel.get("type")
            rname = (rel.get("attributes") or {}).get("name", "")
            if rname:
                if rtype == "author":
                    authors.append(rname)
                elif rtype == "artist":
                    artists.append(rname)
            if rtype == "cover_art":
                cover_filename = (rel.get("attributes") or {}).get("fileName")

        # Canonical tankōbon count: prefer attributes.lastVolume; fall back
        # to /aggregate without language filter so we count all volumes, not
        # just translated ones.
        total_vols = _parse_last_volume(attr)
        if not total_vols:
            try:
                agg = self.get_aggregate(manga_id)
                total_vols = _tankobon_count_from_aggregate(agg)
            except Exception:
                total_vols = 0

        return SeriesMetadata(
            title=title,
            description=description,
            tags=tags,
            authors=authors,
            artists=artists,
            year=attr.get("year"),
            status=attr.get("status"),
            content_rating=attr.get("contentRating"),
            total_volumes=total_vols,
            cover_filename=cover_filename,
        )

    # ------------------------------------------------------------------
    # Protocol: web URL
    # ------------------------------------------------------------------

    def get_web_url(self, manga_id: str) -> str:
        return f"https://mangadex.org/title/{manga_id}"

    def get_aggregate(self, manga_id: str, language: str | None = None) -> dict:
        """Fetch canonical chapter-to-volume buckets, optionally by language."""
        params = {"translatedLanguage[]": language} if language else {}
        return self._api_get(f"/manga/{manga_id}/aggregate", params, timeout=15)

    def get_chapter_web_url(self, chapter_id: str) -> str:
        return f"https://mangadex.org/chapter/{chapter_id}"

    # ------------------------------------------------------------------
    # MangaDex-specific extras (not in base protocol)
    # ------------------------------------------------------------------

    def iter_feed(self, manga_id: str, lang: str, params_extra: dict | None = None):
        """Paginated iterator over the MangaDex chapter feed."""
        params: dict = {
            "translatedLanguage[]": lang,
            "limit": 100,
            "includes[]": "scanlation_group",
            "contentRating[]": CONTENT_RATINGS,
        }
        if params_extra:
            params.update(params_extra)
        offset = 0
        while True:
            params["offset"] = offset
            data  = self._api_get(f"/manga/{manga_id}/feed", params)
            items = data.get("data", [])
            total = data.get("total", 0)
            yield from items
            offset += len(items)
            if offset >= total or not items:
                break
            time.sleep(0.4)

    def get_volume_covers(self, manga_id: str) -> dict[str, str]:
        """Map volume number string → MangaDex cover URL (.512.jpg).

        Paginates: the MD ``/cover`` list can exceed ``limit`` (variants, locales,
        re-uploads), so the first page alone may omit high-numbered volumes.
        """
        covers: dict[str, str] = {}
        offset, limit = 0, 100
        while True:
            data = self._api_get("/cover", {
                "manga[]": manga_id,
                "limit": limit,
                "offset": offset,
                "order[volume]": "asc",
            })
            batch = data.get("data", [])
            for item in batch:
                attr  = item["attributes"]
                vol   = attr.get("volume")
                fname = attr.get("fileName")
                if vol and fname:
                    covers[str(vol)] = f"{MDEX_COVERS}/{manga_id}/{fname}.512.jpg"
            total = int(data.get("total") or 0)
            offset += len(batch)
            if offset >= total or not batch:
                break
            time.sleep(0.25)
        return covers

    def get_lang_chapter_count(self, manga_id: str, lang: str) -> tuple[str, int]:
        data = self._api_get(f"/manga/{manga_id}/feed", {
            "translatedLanguage[]": lang, "limit": 0,
            "contentRating[]": CONTENT_RATINGS,
        }, timeout=15)
        return lang, data.get("total", 0)

    def resolve_total_volumes(self, attr: dict | None, manga_id: str) -> int:
        """Best-effort canonical tankōbon count (for web UI parity display)."""
        n = _parse_last_volume(attr)
        if n:
            return n
        try:
            agg = self.get_aggregate(manga_id)
        except Exception:
            return 0
        return _tankobon_count_from_aggregate(agg)

    def get_groups(self, manga_id: str, language: str) -> dict:
        """Synchronous scanlation group scan (run in executor from async web handlers)."""
        groups: dict[str, set] = {}
        offset, limit = 0, 100
        total  = 1
        params: dict = {
            "translatedLanguage[]": language, "limit": limit,
            "includes[]": "scanlation_group", "order[chapter]": "asc",
            "contentRating[]": CONTENT_RATINGS,
        }
        while offset < total and offset < 500:
            params["offset"] = offset
            try:
                data = self._api_get(f"/manga/{manga_id}/feed", params, timeout=15)
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
                        gname = (rel.get("attributes") or {}).get("name") or "Unknown"
                        groups.setdefault(gname, set()).add(ch_num)
            offset += len(items)
            if not items:
                break
            time.sleep(0.2)

        try:
            agg = self.get_aggregate(manga_id, language)
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

        threshold = max(total_unique - 2, int(total_unique * 0.97)) if total_unique else 0
        sorted_groups = sorted(
            [(name, len(chs), len(chs) >= threshold > 0) for name, chs in groups.items()],
            key=lambda x: x[1], reverse=True,
        )
        return {"groups": sorted_groups, "total_unique": total_unique}
