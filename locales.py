"""Locale preference resolution for series titles, filenames and cover art.

Three independent axes (title, filename, cover) each store one preference:

``original``
    Whatever language the series was published in (``originalLanguage``).
``romanized``
    The ``<originalLanguage>-ro`` variant MangaDex exposes as a first-class
    locale (``ja-ro``, ``ko-ro``). Filename axis only - it exists so a user can
    keep their filesystem ASCII without per-series fiddling.
A locale code
    A fixed choice such as ``en``, falling back to the original when a given
    series or volume has nothing in that locale.
``match_title``
    Filename axis only: whatever the title axis resolved to.
``legacy``
    Nothing chosen: keep whatever was already stored. The default on upgrade.

``None`` is not handled here: callers treat it as "inherit the global setting"
and resolve it to one of the above before calling in.
"""

import json

ORIGINAL   = "original"
ROMANIZED  = "romanized"
# Filename axis only: reuse whatever the display title resolved to, so the two
# stay in step unless the user deliberately splits them (e.g. to keep kanji out
# of the filesystem while Kavita still shows the native title).
MATCH_TITLE = "match_title"
# Nothing chosen yet. Existing installs upgrade into this: the cached title
# MangaDex resolution already produced is reused verbatim, so adding locale
# support renames nothing until the user actually picks a preference.
LEGACY = "legacy"

# Locales MangaDex uses for chapter feeds but never for titles or cover art.
# ``all`` marks language-agnostic chapters; it is a valid ``series.language``
# (telemetry sees it in the wild) but resolving a title to it is meaningless.
NON_CONTENT_LOCALES = {"all", "other", "null", "", "page"}

# Recorded as a volume's ``cover_locale`` when merging had no real cover art and
# fell back to the first page of the first chapter. Distinguishes a placeholder
# we chose ourselves from cover art the user actually picked, so the placeholder
# can be replaced the moment real art appears without needing permission.
PLACEHOLDER_COVER = "page"

# Floor for the global settings picker so a fresh install is never empty. The
# UI unions this with locales actually observed across the user's library.
#
# Ordered by observed usage from telemetry (distinct ``series.language`` per
# instance), then the original-language locales. Those never appear in the
# telemetry - users sync *translations*, so a feed language is never the
# original - but they are exactly what ``original`` and ``romanized`` resolve
# to, so the picker has to offer them regardless.
SEED_LOCALES = [
    "en",
    "es", "es-la", "fr", "pt-br", "vi",
    "it", "pl", "ru", "tr", "uk",
    "de", "ar",
    "ja", "ja-ro", "ko", "ko-ro", "zh", "zh-hk", "zh-ro",
]

LOCALE_NAMES = {
    "en": "English",        "ja": "Japanese",       "ja-ro": "Japanese (romanized)",
    "ko": "Korean",         "ko-ro": "Korean (romanized)",
    "zh": "Chinese",        "zh-hk": "Chinese (Traditional)",
    "zh-ro": "Chinese (romanized)",
    "es": "Spanish",        "es-la": "Spanish (LatAm)",
    "pt-br": "Portuguese (Brazil)", "pt": "Portuguese",
    "fr": "French",         "de": "German",         "it": "Italian",
    "ru": "Russian",        "ar": "Arabic",         "th": "Thai",
    "vi": "Vietnamese",     "tr": "Turkish",        "pl": "Polish",
    "uk": "Ukrainian",      "id": "Indonesian",     "hu": "Hungarian",
    "sv": "Swedish",        "fa": "Persian",        "he": "Hebrew",
    "el": "Greek",          "ka": "Georgian",       "kk": "Kazakh",
}


def is_content_locale(code: str | None) -> bool:
    """False for feed-only pseudo-languages such as ``all``."""
    return bool(code) and code.strip().lower() not in NON_CONTENT_LOCALES


def picker_locales(observed=()) -> list[str]:
    """Seed list unioned with locales seen in the library, seed order first."""
    out = list(SEED_LOCALES)
    for code in observed:
        if is_content_locale(code) and code not in out:
            out.append(code)
    return out


def locale_label(code: str) -> str:
    """Human-readable name for a locale code, falling back to the code itself."""
    return LOCALE_NAMES.get(code, (code or "").upper())


def romanized_code(original_language: str | None) -> str | None:
    """MangaDex spells romanizations ``<lang>-ro``; ``ja`` -> ``ja-ro``."""
    lang = (original_language or "").strip()
    return f"{lang}-ro" if lang and not lang.endswith("-ro") else None


def preference_chain(
    preference: str | None,
    original_language: str | None,
    *,
    allow_romanized: bool = False,
) -> list[str]:
    """Ordered locales to try for ``preference``, most to least preferred.

    The chain always degrades towards something displayable: a fixed locale
    falls back to the original language, and the original falls back to its
    romanization then English. Callers append their own "any" fallback.
    """
    pref = (preference or "").strip() or ORIGINAL
    orig = (original_language or "").strip()
    ro   = romanized_code(orig)

    if pref == ROMANIZED and not allow_romanized:
        pref = ORIGINAL

    if pref == ORIGINAL:
        # No English fallback here: when the original language is unknown (a
        # non-MangaDex source without a companion), falling back to "en" would
        # quietly turn "original covers" into English ones.
        chain = [orig, ro]
    elif pref == ROMANIZED:
        chain = [ro, "en", orig]
    else:
        chain = [pref, orig, ro, "en"]

    out: list[str] = []
    for code in chain:
        if is_content_locale(code) and code not in out:
            out.append(code)
    return out


def resolve_locale(
    available,
    preference: str | None,
    original_language: str | None,
    *,
    allow_romanized: bool = False,
) -> str | None:
    """First available locale matching ``preference``, else any, else ``None``.

    ``available`` is any container of locale codes; when it is ordered (a list
    from the API) the "any" fallback keeps that order, so repeated calls for the
    same series are stable rather than set-iteration order.
    """
    available = [c for c in available if is_content_locale(c)]
    if not available:
        return None
    have = set(available)
    for code in preference_chain(preference, original_language,
                                 allow_romanized=allow_romanized):
        if code in have:
            return code
    return next(iter(available), None)


def resolve_title(
    titles: dict[str, str],
    preference: str | None,
    original_language: str | None,
    *,
    allow_romanized: bool = False,
) -> tuple[str | None, str]:
    """Pick a title from a ``locale -> title`` pool.

    Returns ``(locale, title)``; ``(None, "")`` when the pool is empty. Blank
    values are ignored so an empty ``altTitles`` entry cannot win the chain.
    """
    pool = {
        k: v.strip() for k, v in (titles or {}).items()
        if (v or "").strip() and is_content_locale(k)
    }
    code = resolve_locale(list(pool), preference, original_language,
                          allow_romanized=allow_romanized)
    return (code, pool[code]) if code else (None, "")


def effective_preference(override, global_value, fallback: str) -> str:
    """Per-series override wins; ``None``/blank falls through to the global."""
    for value in (override, global_value):
        text = str(value or "").strip()
        if text:
            return text
    return fallback


def filename_preference(override, global_value, title_preference: str) -> str:
    """Filename axis, with ``match_title`` collapsed to the title's preference."""
    pref = effective_preference(override, global_value, MATCH_TITLE)
    return title_preference if pref == MATCH_TITLE else pref


def series_locale_prefs(series_row: dict, settings: dict, meta: dict | None = None) -> dict:
    """Resolve the three locale axes for one series (override, else global).

    ``original_language`` comes from cached metadata; without it ``original``
    and ``romanized`` have nothing to anchor to and degrade to the next link in
    the chain, which is the correct behaviour for a series we have not fetched.
    """
    settings = settings or {}
    title_pref = effective_preference(
        (series_row or {}).get("title_language_override"),
        settings.get("title_language"), LEGACY,
    )
    return {
        "title": title_pref,
        "cover": effective_preference(
            (series_row or {}).get("cover_language_override"),
            settings.get("cover_language"), ORIGINAL,
        ),
        "filename": filename_preference(
            (series_row or {}).get("filename_language_override"),
            settings.get("filename_language"), title_pref,
        ),
        "original_language": (meta or {}).get("original_language"),
    }


def cached_title_pool(meta: dict | None) -> dict[str, str]:
    """``titles_json`` from cached metadata, falling back to the stored title."""
    raw = (meta or {}).get("titles_json")
    if raw:
        try:
            pool = json.loads(raw)
            if isinstance(pool, dict) and pool:
                return pool
        except (ValueError, TypeError):
            pass
    stored = (meta or {}).get("title")
    return {"en": stored} if stored else {}


def resolve_series_titles(
    series_row: dict, meta: dict | None, settings: dict, fallback: str = "",
) -> tuple[str, str]:
    """``(display_title, filename_title)`` for one series.

    Both come from the cached ``locale -> title`` pool, so flipping a setting
    re-resolves offline rather than refetching the library. They differ only
    when the filename axis is set away from ``match_title`` - the escape hatch
    for keeping non-Latin scripts off the filesystem while Kavita still shows
    the native title.
    """
    prefs   = series_locale_prefs(series_row, settings, meta)
    stored  = ((meta or {}).get("title") or "").strip() or fallback
    pool    = cached_title_pool(meta)
    orig    = prefs["original_language"]

    def _pick(pref: str, **kw) -> str:
        # ``legacy`` reuses the already-cached title rather than re-resolving,
        # so an upgrade is a no-op until the user picks a preference.
        if pref == LEGACY or not pool:
            return stored
        return resolve_title(pool, pref, orig, **kw)[1] or stored

    display = _pick(prefs["title"])
    fname   = _pick(prefs["filename"], allow_romanized=True)
    return display, (fname or display)
