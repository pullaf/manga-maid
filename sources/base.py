"""Shared types for multi-source manga support."""
from typing import Protocol, TypedDict


class MangaResult(TypedDict):
    source_key: str        # "mangadex" | "suwayomi:1998854268"
    source_name: str       # "MangaDex" | "MangaPlus"
    manga_id: str          # opaque ID within the source
    title: str
    thumbnail_url: str | None
    status: str | None
    year: int | None


class ChapterInfo(TypedDict):
    chapter_id: str
    chapter_num: float
    volume_num: float | None
    title: str | None
    group_name: str | None
    publish_date: str | None   # YYYY-MM-DD
    language: str | None


class SeriesMetadata(TypedDict):
    title: str
    description: str | None
    tags: list[str]
    authors: list[str]
    artists: list[str]
    year: int | None
    status: str | None
    content_rating: str | None
    total_volumes: int
    cover_filename: str | None


class Source(Protocol):
    key: str   # "mangadex" | "suwayomi:12345"
    name: str
    lang: str

    def search(self, query: str) -> list[MangaResult]: ...
    def get_metadata(self, manga_id: str) -> SeriesMetadata: ...
    def get_web_url(self, manga_id: str) -> str: ...
