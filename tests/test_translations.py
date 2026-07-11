"""Seven-language consistency of the translations table."""
import pytest

from translations import EXP_LABELS, OB_SPORTS, SPORTS_LABELS, T, tr

ALL_LANGS = ["az", "ru", "en", "tr", "kz", "uz", "ar"]


def test_all_seven_languages_present():
    assert sorted(T) == sorted(ALL_LANGS)


def test_key_sets_identical_across_languages():
    """Every language must expose exactly the same keys — a key added to one
    language only would crash T[lang][key] lookups for the others."""
    reference = set(T["ru"])
    for lang in ALL_LANGS:
        missing = reference - set(T[lang])
        extra = set(T[lang]) - reference
        assert not missing, f"{lang} is missing keys: {sorted(missing)}"
        assert not extra, f"{lang} has extra keys: {sorted(extra)}"


def test_no_empty_values():
    for lang in ALL_LANGS:
        for key, val in T[lang].items():
            assert isinstance(val, str) and val.strip(), f"T[{lang}][{key}] is empty"


def test_system_prompts_are_substantial():
    for lang in ALL_LANGS:
        assert len(T[lang]["system_prompt"]) > 50, lang


def test_format_placeholders_subset_of_russian_reference():
    """Call sites pass kwargs sized for the ru string; str.format ignores
    extra kwargs, but a placeholder present only in some other language
    would raise KeyError for those users only."""
    import string
    fmt = string.Formatter()

    def placeholders(s: str) -> set:
        try:
            return {name for _, name, _, _ in fmt.parse(s) if name}
        except ValueError:
            return set()  # literal braces in text (e.g. JSON examples)

    for key in T["ru"]:
        ref = placeholders(T["ru"][key])
        for lang in ALL_LANGS:
            got = placeholders(T[lang][key])
            assert got <= ref, f"T[{lang}][{key}] has placeholders {got - ref} absent from ru"


def test_no_betting_brand_in_user_facing_strings():
    user_facing = ["welcome_intro", "post_onboarding", "menu_forecast", "menu_express",
                   "menu_history", "menu_profile", "express_ask", "express_title"]
    for lang in ALL_LANGS:
        for key in user_facing:
            assert "mostbet" not in T[lang].get(key, "").lower(), f"T[{lang}][{key}]"


def test_onboarding_labels_cover_all_languages():
    for table in (SPORTS_LABELS, EXP_LABELS, OB_SPORTS):
        for lang in table:
            assert lang in ALL_LANGS


def test_tr_falls_back_for_unknown_user(temp_db):
    # Unknown uid → default language, still a non-empty string.
    result = tr(999999999, "need_reg")
    assert isinstance(result, str) and result


def test_tr_formats_kwargs(temp_db):
    temp_db.db_ensure(820001, "trtest", "ru")
    result = tr(820001, "rate_limit", w=30, v=2, max=3)
    assert "30" in result


@pytest.mark.parametrize("lang", ALL_LANGS)
def test_menu_keys_unique_within_language(lang):
    """Menu routing compares message text to labels — duplicates would make
    two menu buttons trigger the same handler."""
    labels = [T[lang][k] for k in ("menu_forecast", "menu_express",
                                   "menu_history", "menu_profile", "menu_support")]
    assert len(labels) == len(set(labels)), lang
