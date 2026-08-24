"""Locale preference resolution - synthetic pools, no network."""

import locales as L

# Shape mirrors MangaDex: a romanized primary plus native and translated alts.
POOL = {
    "ja-ro": "Romanized Title",
    "ja":    "原題",
    "en":    "English Title",
    "pt-br": "Titulo",
}


def test_fixed_locale_wins_when_present():
    assert L.resolve_title(POOL, "en", "ja") == ("en", "English Title")
    assert L.resolve_title(POOL, "pt-br", "ja") == ("pt-br", "Titulo")


def test_fixed_locale_falls_back_to_original():
    assert L.resolve_title({"ja": "原題"}, "en", "ja") == ("ja", "原題")


def test_original_prefers_native_over_romanized():
    assert L.resolve_title(POOL, "original", "ja") == ("ja", "原題")


def test_original_falls_back_to_romanization_then_english():
    pool = {"ja-ro": "Romanized Title", "en": "English Title"}
    assert L.resolve_title(pool, "original", "ja") == ("ja-ro", "Romanized Title")
    assert L.resolve_title({"en": "English Title"}, "original", "ja") == ("en", "English Title")


def test_romanized_only_honoured_where_allowed():
    """The romanized sentinel is filename-only; elsewhere it degrades to original."""
    assert L.resolve_title(POOL, "romanized", "ja", allow_romanized=True) == \
        ("ja-ro", "Romanized Title")
    assert L.resolve_title(POOL, "romanized", "ja") == ("ja", "原題")


def test_romanized_code_follows_original_language():
    assert L.romanized_code("ko") == "ko-ro"
    assert L.romanized_code("ja-ro") is None
    assert L.romanized_code("") is None


def test_unknown_preference_still_resolves():
    """A locale nothing in the pool has must not yield an empty title."""
    code, title = L.resolve_title(POOL, "sv", "ja")
    assert code == "ja" and title


def test_blank_entries_never_win():
    pool = {"en": "   ", "ja": "原題"}
    assert L.resolve_title(pool, "en", "ja") == ("ja", "原題")


def test_empty_pool_is_survivable():
    assert L.resolve_title({}, "en", "ja") == (None, "")
    assert L.resolve_locale([], "en", "ja") is None


def test_cover_locale_falls_back_per_volume():
    """The vol-15 case: most volumes localized, the newest only in the original."""
    assert L.resolve_locale(["ja", "en", "es-la"], "en", "ja") == "en"
    assert L.resolve_locale(["ja"], "en", "ja") == "ja"


def test_any_fallback_keeps_input_order():
    assert L.resolve_locale(["es-la", "th"], "en", "ja") == "es-la"


def test_chain_is_deduped():
    chain = L.preference_chain("ja", "ja")
    assert len(chain) == len(set(chain))


def test_seed_locales_cover_the_common_cases():
    """A fresh install must offer something before any library is linked."""
    for code in ("en", "ja", "ja-ro", "ko-ro", "es-la", "pt-br"):
        assert code in L.SEED_LOCALES
    assert all(L.locale_label(c) for c in L.SEED_LOCALES)


def test_locale_label_falls_back_to_code():
    assert L.locale_label("xx") == "XX"


def test_feed_pseudo_languages_never_resolve():
    """``all`` is a valid series.language but is not a title or cover locale."""
    assert L.resolve_title({"all": "Whatever", "ja": "原題"}, "en", "ja") == ("ja", "原題")
    assert L.resolve_locale(["all"], "en", "ja") is None
    assert L.resolve_title({"all": "Whatever"}, "en", "ja") == (None, "")
    assert "all" not in L.preference_chain("all", "ja")
    assert not L.is_content_locale("all")


def test_picker_unions_seed_with_observed():
    out = L.picker_locales(["es-la", "th", "all"])
    assert out[:len(L.SEED_LOCALES)] == L.SEED_LOCALES  # seed keeps its order
    assert "th" in out                                  # library-observed appended
    assert out.count("es-la") == 1                      # already seeded, not duped
    assert "all" not in out


def test_seed_reflects_observed_usage_and_original_languages():
    for code in ("en", "es-la", "pt-br", "vi", "pl", "tr", "uk"):   # from telemetry
        assert code in L.SEED_LOCALES
    for code in ("ja", "ja-ro", "ko", "ko-ro"):                     # never a feed lang
        assert code in L.SEED_LOCALES
    assert all(L.is_content_locale(c) for c in L.SEED_LOCALES)


def test_override_beats_global_and_blanks_fall_through():
    assert L.effective_preference("ja", "en", "original") == "ja"
    assert L.effective_preference(None, "en", "original") == "en"
    assert L.effective_preference("  ", "", "original") == "original"


def test_filename_defaults_to_following_the_title():
    assert L.filename_preference(None, None, "ja") == "ja"
    assert L.filename_preference(None, "match_title", "ja") == "ja"
    assert L.filename_preference("ja-ro", "match_title", "ja") == "ja-ro"
    assert L.filename_preference(None, "romanized", "ja") == "romanized"


def test_unset_preference_means_legacy():
    """Upgrading with nothing configured must not re-resolve anything."""
    assert L.effective_preference(None, "", L.LEGACY) == L.LEGACY
    assert L.effective_preference(None, "en", L.LEGACY) == "en"
    assert L.filename_preference(None, "", L.LEGACY) == L.LEGACY


def test_original_never_silently_means_english():
    """Unknown original language must not resolve 'original' to English."""
    assert L.preference_chain("original", None) == []
    assert L.resolve_locale(["ja", "en"], "original", None) == "ja"   # input order
    # With the original known, behaviour is unchanged.
    assert L.resolve_locale(["ja", "en"], "original", "ja") == "ja"
    assert L.resolve_locale(["en"], "original", "ja") == "en"         # only option
