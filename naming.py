"""Shared filename template logic used by both manga-sync and the web app."""
import math
import re


def safe_filename_token(value) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", str(value or ""))


def format_num(value) -> str:
    if value is None:
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(num)) if num.is_integer() else str(num)


def floor_int_str(value) -> str:
    """Floor a numeric value and return it as a string (e.g. 3.5 → '3'). Falls back to format_num."""
    try:
        return str(math.floor(float(value)))
    except (TypeError, ValueError):
        return format_num(value)


def apply_naming_template(
    template: str,
    *,
    language: str = "",
    group: str = "",
    title: str = "",
    volume_num=None,
    chapter_num=None,
    chapter_title: str = "",
    chapter_range: str = "",
) -> str:
    """Apply %%1-%%6 filename template substitutions.

    %%1=language, %%2=group, %%3=series title, %%4=volume, %%5=chapter or range,
    %%6=chapter title.
    """
    result = template or ""
    if volume_num is None:
        result = re.sub(r"\bvol(?:ume)?\.?\s*%4\b", "", result, flags=re.IGNORECASE)
        result = re.sub(r"\bvol(?:ume)?\.?\s*$", "", result, flags=re.IGNORECASE)
    if chapter_num is None and not chapter_range:
        result = re.sub(r"\bch\.?\s*%5\b", "", result, flags=re.IGNORECASE)
    result = result.replace("%1", safe_filename_token(language))
    result = result.replace("%2", safe_filename_token(group))
    result = result.replace("%3", safe_filename_token(title))
    result = result.replace("%4", format_num(volume_num))
    result = result.replace("%5", chapter_range or format_num(chapter_num))
    result = result.replace("%6", safe_filename_token(chapter_title))
    result = re.sub(r"\(\s*\)", "", result)
    result = re.sub(r"\[\s*\]", "", result)
    result = re.sub(r"\{\s*\}", "", result)
    result = re.sub(r"\bch\.?\s*$", "", result, flags=re.IGNORECASE)
    result = re.sub(r"\bvol(?:ume)?\.?\s*$", "", result, flags=re.IGNORECASE)
    result = result.replace("..", ".")
    result = re.sub(r"\s+([)\]}])", r"\1", result)
    result = re.sub(r"([(\[{])\s+", r"\1", result)
    result = re.sub(r"\s{2,}", " ", result)
    return result.strip()
