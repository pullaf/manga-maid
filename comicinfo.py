"""ComicInfo.xml generation, CBZ injection, and chapter-to-volume merging."""
import contextlib
import os
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from file_permissions import apply_file_permission_mask

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

    def _trim_chapter_num(value):
        """Drop trailing ``.0`` for whole chapters; keep ``.5`` style fractions."""
        if value is None:
            return None
        try:
            num = float(value)
        except (TypeError, ValueError):
            return value
        return int(num) if num.is_integer() else num

    fields = [
        ("Series",      series_title),
        ("Number",      _trim_chapter_num(number)),
        ("Volume",      int(volume_num) if volume_num is not None else None),
        ("Title",       chapter_title),
        ("Summary",     description),
        ("Writer",      ", ".join(authors)  if authors  else None),
        ("Penciller",   ", ".join(artists)  if artists  else None),
        ("Translator",  group_name),
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


def inject_comicinfo(
    cbz_path: str,
    xml_content: str,
    overwrite: bool = False,
    file_permission_mask: str | None = None,
) -> bool:
    """Add or replace ComicInfo.xml inside a CBZ. Returns True on success."""
    if not os.path.exists(cbz_path):
        return False
    tmp_path: str | None = None
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
        tmp_path = None
        apply_file_permission_mask(cbz_path, file_permission_mask)
        return True
    except Exception:
        return False
    finally:
        if tmp_path:
            with contextlib.suppress(OSError):
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)


def replace_volume_cover(
    cbz_path: str,
    cover_image_bytes: bytes,
    file_permission_mask: str | None = None,
    insert_if_missing: bool = False,
) -> bool:
    """Swap the embedded cover page inside an existing volume CBZ.

    ``merge_chapters_into_volume`` writes the cover as ``0000.jpg``, ahead of the
    ``0001``-numbered pages, so volumes this app built always have a slot to
    replace. Archives imported from elsewhere may not; ``insert_if_missing``
    adds one, which is what makes a cover-language choice apply to a library
    that was scanned in rather than merged here.

    Page order and the ComicInfo block are otherwise untouched.
    """
    if not os.path.exists(cbz_path) or not cover_image_bytes:
        return False
    tmp_path: str | None = None
    try:
        with zipfile.ZipFile(cbz_path, "r") as zin:
            names = zin.namelist()
            cover_name = next(
                (n for n in names
                 if os.path.splitext(os.path.basename(n))[0] == "0000"
                 and os.path.splitext(n)[1].lower() in _IMAGE_EXTS),
                None,
            )
            if not cover_name and not insert_if_missing:
                return False

            fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(cbz_path), suffix=".tmp"
            )
            os.close(fd)

            with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_STORED) as zout:
                if not cover_name:
                    # Written first so it sorts ahead of the existing pages.
                    zout.writestr("0000.jpg", cover_image_bytes)
                for name in names:
                    if cover_name and name == cover_name:
                        zout.writestr(name, cover_image_bytes)
                    else:
                        zout.writestr(name, zin.read(name))

        shutil.move(tmp_path, cbz_path)
        tmp_path = None
        apply_file_permission_mask(cbz_path, file_permission_mask)
        return True
    except Exception:
        return False
    finally:
        if tmp_path:
            with contextlib.suppress(OSError):
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)


def merge_chapters_into_volume(
    chapter_paths: list[str],
    output_path: str,
    comicinfo_xml: str = None,
    cover_image_bytes: bytes = None,
    file_permission_mask: str | None = None,
) -> bool:
    """Merge sorted chapter CBZs into a single volume CBZ. Returns True on success.

    If cover_image_bytes is provided it is written as 0000.jpg so readers
    display the proper volume cover instead of the first manga page.
    """
    tmp_path: str | None = None
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
        tmp_path = None
        apply_file_permission_mask(output_path, file_permission_mask)
        return True
    except Exception:
        return False
    finally:
        if tmp_path:
            with contextlib.suppress(OSError):
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)


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
