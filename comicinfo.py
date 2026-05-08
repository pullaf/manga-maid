"""ComicInfo.xml generation, CBZ injection, and chapter-to-volume merging."""
import os
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}


def read_first_image_bytes(cbz_path: str) -> bytes | None:
    """Return raw bytes of the first sorted image inside a CBZ, or None."""
    try:
        with zipfile.ZipFile(cbz_path, "r") as zin:
            image_names = sorted(
                n for n in zin.namelist()
                if os.path.splitext(n)[1].lower() in _IMAGE_EXTS
                and not n.startswith("__MACOSX")
            )
            if not image_names:
                return None
            return zin.read(image_names[0])
    except Exception:
        return None

_AGE_RATING = {
    "safe":          "Everyone",
    "suggestive":    "Teen",
    "erotica":       "Mature 17+",
    "pornographic":  "Adults Only 18+",
}


def build_comicinfo_xml(
    series_title: str,
    number,
    volume_num=None,
    chapter_title: str = None,
    description: str = None,
    authors: list = None,
    artists: list = None,
    group_name: str = None,
    language: str = None,
    year: int = None,
    tags: list = None,
    content_rating: str = None,
    page_count: int = None,
    web: str = None,
    count: int = None,
) -> str:
    def _esc(s: str) -> str:
        return (str(s)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))

    fields = [
        ("Series",      series_title),
        ("Number",      number),
        ("Volume",      int(volume_num) if volume_num is not None else None),
        ("Title",       chapter_title),
        ("Summary",     description),
        ("Writer",      ", ".join(authors)  if authors  else None),
        ("Penciller",   ", ".join(artists)  if artists  else None),
        ("Publisher",   group_name),
        ("Genre",       ", ".join(tags)     if tags     else None),
        ("Count",       count),
        ("Web",         web),
        ("LanguageISO", language),
        ("Year",        year),
        ("Manga",       "YesAndRightToLeft"),
        ("AgeRating",   _AGE_RATING.get(content_rating or "")),
        ("PageCount",   page_count),
    ]

    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xmlns:xsd="http://www.w3.org/2001/XMLSchema">',
    ]
    for tag, val in fields:
        if val is not None and val != "":
            lines.append(f"  <{tag}>{_esc(val)}</{tag}>")
    lines.append("</ComicInfo>")
    return "\n".join(lines)


def count_pages(cbz_path: str) -> int:
    try:
        with zipfile.ZipFile(cbz_path, "r") as z:
            return sum(
                1 for n in z.namelist()
                if os.path.splitext(n)[1].lower() in _IMAGE_EXTS
                and not n.startswith("__MACOSX")
            )
    except Exception:
        return 0


def inject_comicinfo(cbz_path: str, xml_content: str, overwrite: bool = False) -> bool:
    """Add or replace ComicInfo.xml inside a CBZ. Returns True on success."""
    if not os.path.exists(cbz_path):
        return False
    tmp_path = None
    try:
        with zipfile.ZipFile(cbz_path, "r") as zin:
            names   = zin.namelist()
            ci_name = next((n for n in names if n.upper() == "COMICINFO.XML"), None)
            if ci_name and not overwrite:
                return False

            fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(cbz_path), suffix=".tmp"
            )
            os.close(fd)

            with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_STORED) as zout:
                for name in names:
                    if name.upper() == "COMICINFO.XML":
                        continue
                    zout.writestr(name, zin.read(name))
                zout.writestr("ComicInfo.xml", xml_content.encode("utf-8"))

        shutil.move(tmp_path, cbz_path)
        return True
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return False


def merge_chapters_into_volume(
    chapter_paths: list[str],
    output_path: str,
    comicinfo_xml: str = None,
    cover_image_bytes: bytes = None,
) -> bool:
    """Merge sorted chapter CBZs into a single volume CBZ. Returns True on success.

    If cover_image_bytes is provided it is written as 0000.jpg so readers
    display the proper volume cover instead of the first manga page.
    """
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(output_path), suffix=".tmp"
        )
        os.close(fd)

        page_num = 1
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_STORED) as zout:
            if cover_image_bytes:
                zout.writestr("0000.jpg", cover_image_bytes)
            for cbz_path in chapter_paths:
                with zipfile.ZipFile(cbz_path, "r") as zin:
                    image_names = sorted(
                        n for n in zin.namelist()
                        if os.path.splitext(n)[1].lower() in _IMAGE_EXTS
                        and not n.startswith("__MACOSX")
                    )
                    for name in image_names:
                        ext = os.path.splitext(name)[1]
                        zout.writestr(f"{page_num:04d}{ext}", zin.read(name))
                        page_num += 1
            if comicinfo_xml:
                zout.writestr("ComicInfo.xml", comicinfo_xml.encode("utf-8"))

        shutil.move(tmp_path, output_path)
        return True
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return False


def read_comicinfo_xml(cbz_path: str) -> str | None:
    """Return ComicInfo.xml content from a CBZ, if present."""
    if not os.path.exists(cbz_path):
        return None
    try:
        with zipfile.ZipFile(cbz_path, "r") as zin:
            name = next((n for n in zin.namelist() if n.upper() == "COMICINFO.XML"), None)
            if not name:
                return None
            return zin.read(name).decode("utf-8", errors="replace")
    except Exception:
        return None


def parse_comicinfo_fields(xml_content: str | None) -> dict[str, str]:
    """Parse first-level ComicInfo tags into a dict of strings."""
    if not xml_content:
        return {}
    try:
        root = ET.fromstring(xml_content)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for child in list(root):
        tag = child.tag.rsplit("}", 1)[-1]
        val = (child.text or "").strip()
        if tag and val:
            out[tag] = val
    return out
