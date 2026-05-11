"""Suwayomi-Server source adapter.

SuwayomiClient handles GraphQL transport (with optional JWT auth).
SuwayomiSource wraps a single installed Suwayomi extension source and
implements the Source protocol so manga-sync.py can use it identically to
MangaDexSource.

Page download: fetchChapterPages returns relative REST paths
(/api/v1/manga/{id}/chapter/{sourceOrder}/page/{n}).  We proxy these through
Suwayomi so the server handles auth / Cloudflare bypass, then zip into a CBZ.
"""
import io
import json
import re
import time
import zipfile
from pathlib import Path
from urllib import error as urlerror, parse, request as urlrequest

from .base import ChapterInfo, MangaResult, SeriesMetadata

# ---------------------------------------------------------------------------
# Chapter name parser
# ---------------------------------------------------------------------------
# Suwayomi returns name like "Vol.2 Ch.14.5 - Some Title" plus a separate
# chapterNumber float.  We extract volume and title from the name string.

_VOL_RE   = re.compile(r"Vol\.(\d+(?:\.\d+)?)", re.IGNORECASE)
_TITLE_RE = re.compile(r"(?:Ch\.\S+\s*)?-\s*(.+)$")


def _parse_chapter_name(name: str, chapter_number: float | None):
    """Return (volume_num, chapter_num, title) extracted from a Suwayomi chapter name."""
    vol_m   = _VOL_RE.search(name or "")
    title_m = _TITLE_RE.search(name or "")
    volume  = float(vol_m.group(1)) if vol_m else None
    ch_num  = chapter_number if (chapter_number is not None and chapter_number >= 0) else None
    title   = title_m.group(1).strip() if title_m else None
    return volume, ch_num, title


# ---------------------------------------------------------------------------
# GraphQL client
# ---------------------------------------------------------------------------

_GQL_SOURCES = """
query Sources {
  sources {
    nodes { id name lang displayName iconUrl supportsLatest }
  }
}
"""

_GQL_SEARCH = """
mutation FetchSourceManga($source: LongString!, $query: String!, $page: Int!) {
  fetchSourceManga(input: { source: $source, type: SEARCH, query: $query, page: $page }) {
    mangas { id title thumbnailUrl }
    hasNextPage
  }
}
"""

_GQL_FETCH_MANGA = """
mutation FetchManga($id: Int!) {
  fetchManga(input: { id: $id }) {
    manga {
      id title description author artist genre status thumbnailUrl
    }
  }
}
"""

_GQL_FETCH_CHAPTERS = """
mutation FetchChapters($mangaId: Int!) {
  fetchChapters(input: { mangaId: $mangaId }) {
    chapters {
      id name chapterNumber scanlator uploadDate sourceOrder
    }
  }
}
"""

_GQL_FETCH_PAGES = """
mutation FetchChapterPages($chapterId: Int!) {
  fetchChapterPages(input: { chapterId: $chapterId }) {
    pages
  }
}
"""


class SuwayomiClient:
    """Thin GraphQL transport for Suwayomi-Server."""

    def __init__(self, base_url: str, username: str = "", password: str = ""):
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token: str | None = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _authenticate(self) -> str:
        """Obtain a JWT bearer token via the REST login endpoint."""
        payload = json.dumps({"username": self._username, "password": self._password}).encode()
        req = urlrequest.Request(
            f"{self.base_url}/api/v1/auth/login",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return data["token"]

    def _auth_header(self) -> dict:
        if not self._username:
            return {}
        if not self._token:
            self._token = self._authenticate()
        return {"Authorization": f"Bearer {self._token}"}

    # ------------------------------------------------------------------
    # GraphQL transport
    # ------------------------------------------------------------------

    def _gql(self, query: str, variables: dict | None = None) -> dict:
        payload = json.dumps({"query": query, "variables": variables or {}}).encode()
        headers = {"Content-Type": "application/json", **self._auth_header()}
        req = urlrequest.Request(
            f"{self.base_url}/api/graphql",
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with urlrequest.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read())
        except urlerror.HTTPError as e:
            if e.code == 401 and self._username:
                # Token may have expired - refresh once
                self._token = self._authenticate()
                headers["Authorization"] = f"Bearer {self._token}"
                req = urlrequest.Request(
                    f"{self.base_url}/api/graphql",
                    data=payload,
                    headers=headers,
                    method="POST",
                )
                with urlrequest.urlopen(req, timeout=30) as resp:
                    body = json.loads(resp.read())
            else:
                raise RuntimeError(f"Suwayomi HTTP {e.code}") from e
        if "errors" in body:
            raise RuntimeError(f"Suwayomi GQL error: {body['errors']}")
        return body["data"]

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        try:
            self._gql(_GQL_SOURCES)
            return True
        except Exception:
            return False

    def list_sources(self) -> list[dict]:
        """Return list of installed source dicts: {id, name, lang, displayName, iconUrl}."""
        data = self._gql(_GQL_SOURCES)
        return data.get("sources", {}).get("nodes", [])

    def fetch_source_manga(self, source_id: str, query: str, page: int = 1) -> tuple[list[dict], bool]:
        """Search a source. Returns (manga_list, has_next_page)."""
        data = self._gql(_GQL_SEARCH, {"source": source_id, "query": query, "page": page})
        result = data.get("fetchSourceManga", {})
        return result.get("mangas", []), result.get("hasNextPage", False)

    def fetch_manga(self, manga_id: int) -> dict:
        data = self._gql(_GQL_FETCH_MANGA, {"id": manga_id})
        return data["fetchManga"]["manga"]

    def fetch_chapters(self, manga_id: int) -> list[dict]:
        data = self._gql(_GQL_FETCH_CHAPTERS, {"mangaId": manga_id})
        return data["fetchChapters"]["chapters"]

    def fetch_chapter_pages(self, chapter_id: int) -> list[str]:
        """Return list of page paths like /api/v1/manga/{id}/chapter/{idx}/page/{n}."""
        data = self._gql(_GQL_FETCH_PAGES, {"chapterId": chapter_id})
        return data.get("fetchChapterPages", {}).get("pages", [])

    def download_page(self, page_path: str, timeout: int = 60) -> bytes:
        """Download a single page image through the Suwayomi proxy."""
        url = f"{self.base_url}{page_path}"
        req = urlrequest.Request(url, headers=self._auth_header())
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return resp.read()


# ---------------------------------------------------------------------------
# Source adapter
# ---------------------------------------------------------------------------

_SUWAYOMI_STATUS_MAP = {
    "ONGOING":   "ongoing",
    "COMPLETED": "completed",
    "LICENSED":  "licensed",
    "CANCELLED": "cancelled",
    "HIATUS":    "hiatus",
    "UNKNOWN":   "unknown",
}


class SuwayomiSource:
    """Source adapter wrapping one Suwayomi extension source."""

    def __init__(self, client: SuwayomiClient, source_id: str, source_name: str, lang: str):
        self._client   = client
        self._source_id = source_id  # numeric string, e.g. "1998854268"
        self.key  = f"suwayomi:{source_id}"
        self.name = source_name
        self.lang = lang

    # ------------------------------------------------------------------
    # Protocol: search
    # ------------------------------------------------------------------

    def search(self, query: str) -> list[MangaResult]:
        mangas, _ = self._client.fetch_source_manga(self._source_id, query)
        results: list[MangaResult] = []
        for m in mangas:
            thumb = m.get("thumbnailUrl")
            # Route thumbnail through our proxy so the browser can load it
            if thumb:
                thumb = f"/api/proxy/suwayomi/thumbnail/{m['id']}"
            results.append(MangaResult(
                source_key=self.key,
                source_name=self.name,
                manga_id=str(m["id"]),
                title=m.get("title", ""),
                thumbnail_url=thumb,
                status=None,
                year=None,
            ))
        return results

    # ------------------------------------------------------------------
    # Protocol: metadata
    # ------------------------------------------------------------------

    def get_metadata(self, manga_id: str) -> SeriesMetadata:
        manga = self._client.fetch_manga(int(manga_id))
        genre_raw = manga.get("genre") or []
        tags = genre_raw if isinstance(genre_raw, list) else [genre_raw]
        status_raw = (manga.get("status") or "").upper()
        return SeriesMetadata(
            title=manga.get("title") or "",
            description=manga.get("description") or None,
            tags=tags,
            authors=[manga["author"]] if manga.get("author") else [],
            artists=[manga["artist"]] if manga.get("artist") else [],
            year=None,
            status=_SUWAYOMI_STATUS_MAP.get(status_raw),
            content_rating=None,
            total_volumes=0,
            cover_filename=None,
        )

    # ------------------------------------------------------------------
    # Protocol: URLs
    # ------------------------------------------------------------------

    def get_web_url(self, manga_id: str) -> str:
        return f"{self._client.base_url}/manga/{manga_id}"

    def get_chapter_web_url(self, chapter_id: str) -> str:
        return f"{self._client.base_url}/chapter/{chapter_id}"

    # ------------------------------------------------------------------
    # Chapter iteration (used by manga-sync.py)
    # ------------------------------------------------------------------

    def iter_chapters(self, manga_id: str) -> list[ChapterInfo]:
        """Fetch and parse all chapters for this manga from Suwayomi."""
        raw = self._client.fetch_chapters(int(manga_id))
        chapters: list[ChapterInfo] = []
        for ch in raw:
            volume, ch_num, title = _parse_chapter_name(
                ch.get("name", ""), ch.get("chapterNumber")
            )
            if ch_num is None:
                continue
            date_raw = ch.get("uploadDate")
            pub_date = None
            if date_raw:
                try:
                    # uploadDate is Unix ms timestamp
                    import datetime
                    pub_date = datetime.datetime.fromtimestamp(
                        int(date_raw) / 1000, tz=datetime.timezone.utc
                    ).strftime("%Y-%m-%d")
                except Exception:
                    pass
            chapters.append(ChapterInfo(
                chapter_id=str(ch["id"]),
                chapter_num=ch_num,
                volume_num=volume,
                title=title,
                group_name=ch.get("scanlator") or None,
                publish_date=pub_date,
                language=self.lang,
            ))
        return chapters

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def download_chapter(
        self,
        chapter_id: str,
        dest_dir: str,
        file_format: str,
        file_stem: str,
        delay: float = 0.0,
    ) -> bool:
        """Download all pages and zip into a CBZ/CBR at dest_dir/file_stem.file_format.

        Returns True on success, False on failure.
        """
        try:
            pages = self._client.fetch_chapter_pages(int(chapter_id))
        except Exception as exc:
            print(f"[suwayomi] fetchChapterPages failed for ch {chapter_id}: {exc}")
            return False

        if not pages:
            print(f"[suwayomi] no pages returned for ch {chapter_id}")
            return False

        ext = f".{file_format.lstrip('.')}"
        dest = Path(dest_dir) / f"{file_stem}{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            for i, page_path in enumerate(pages):
                try:
                    data = self._client.download_page(page_path)
                except Exception as exc:
                    print(f"[suwayomi] page {i} download failed: {exc}")
                    return False
                # Determine image extension from URL, default jpg
                page_ext = page_path.split(".")[-1].lower() if "." in page_path else "jpg"
                if page_ext not in ("jpg", "jpeg", "png", "webp", "gif"):
                    page_ext = "jpg"
                zf.writestr(f"{i:04d}.{page_ext}", data)
                if delay and i < len(pages) - 1:
                    time.sleep(delay)

        dest.write_bytes(buf.getvalue())
        return True
