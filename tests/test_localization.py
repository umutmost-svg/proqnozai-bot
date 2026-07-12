"""Offline localization tests for the AZ/RU/TR priority markets and safe
language handling. No network. Guards the brand-language-guide decisions and
the language fallback/normalization contract."""
import pytest

from translations import T, tr
import db

PRIORITY = ("az", "ru", "tr")

# Keys that are LLM prompts, not user-facing labels — excluded from wording
# assertions (prompts are intentionally out of localization scope).
PROMPT_KEYS = {"system_prompt", "live_tip_prompt", "img_prompt"}


def _user_facing_items(lang):
    return {k: v for k, v in T[lang].items() if k not in PROMPT_KEYS}


# ─── Key presence & non-empty for the three priority languages ────────────────
@pytest.mark.parametrize("lang", PRIORITY)
def test_priority_language_has_all_keys(lang):
    ref = set(T["ru"])
    assert set(T[lang]) == ref, f"{lang} key set differs from ru"


@pytest.mark.parametrize("lang", PRIORITY)
def test_priority_language_no_empty_values(lang):
    for key, val in T[lang].items():
        assert isinstance(val, str) and val.strip(), f"T[{lang}][{key}] empty"


# ─── Canonical Azerbaijani terminology (brand guide) ──────────────────────────
def test_odds_uses_azerbaijani_emsal_not_kef():
    # "kef" must never appear in any user-facing Azerbaijani string.
    for key, val in _user_facing_items("az").items():
        assert "kef" not in val.lower(), f"T[az][{key}] still uses 'kef'"
    # The enrichment fallbacks must use the canonical "əmsal".
    assert "əmsal" in T["az"]["enr_football_unavailable"].lower()
    assert "əmsal" in T["az"]["enr_unverified"].lower()


def test_draw_not_labelled_hech_heche():
    # "heç-heçə" is only a colloquial alternative, never the canonical UI term.
    for key, val in _user_facing_items("az").items():
        assert "heç-heçə" not in val.lower(), f"T[az][{key}] uses heç-heçə"


def test_canonical_menu_labels():
    assert T["az"]["menu_forecast"] == "⚽ Proqnoz"
    assert T["ru"]["menu_forecast"] == "⚽ Прогноз"
    assert T["tr"]["menu_forecast"] == "⚽ Tahmin"
    # TR parlay label uses the native "Kombine".
    assert T["tr"]["menu_express"] == "⚡ Kombine"


def test_no_gambling_hype_in_priority_user_facing():
    banned = ("zəmanət", "mütləq qazan", "pul qazan", "гарантирован",
              "выиграй деньги", "garanti", "kesin kazan")
    for lang in PRIORITY:
        for key, val in _user_facing_items(lang).items():
            low = val.lower()
            for b in banned:
                assert b not in low, f"T[{lang}][{key}] contains banned '{b}'"


# ─── Safe language handling ───────────────────────────────────────────────────
def test_default_lang_is_ru():
    assert db.DEFAULT_LANG == "ru"


@pytest.mark.parametrize("raw,expected", [
    ("az", "az"), ("RU", "ru"), (" tr ", "tr"),
    ("en", "en"), ("kz", "kz"), ("uz", "uz"), ("ar", "ar"),
    ("xx", "ru"), ("", "ru"), (None, "ru"), (123, "ru"), ("lang_az", "ru"),
])
def test_normalize_lang(raw, expected):
    assert db.normalize_lang(raw) == expected


def test_unknown_user_falls_back_to_default(temp_db):
    result = tr(999_000_111, "need_reg")
    assert isinstance(result, str) and result  # non-empty
    assert temp_db.db_lang(999_000_111) == "ru"


def test_missing_key_returns_key_not_empty(temp_db):
    temp_db.db_ensure(940001, "u", "az")
    assert tr(940001, "__does_not_exist__") == "__does_not_exist__"


def test_legacy_bad_db_language_normalizes(temp_db):
    # Simulate a legacy/corrupted stored value; db_lang must normalize it.
    temp_db.db_ensure(940002, "u", "az")
    temp_db.db_set(940002, "lang", "zz-INVALID")
    assert temp_db.db_lang(940002) == "ru"
    # And rendering still works via the fallback chain.
    assert tr(940002, "need_reg")


def test_invalid_lang_callback_value_never_persists_unsupported():
    # Mirrors lang_cb's normalization of q.data.split("_", 1)[1].
    for data, expected in (("lang_az", "az"), ("lang_ru", "ru"),
                           ("lang_zz", "ru"), ("lang_", "ru")):
        raw = data.split("_", 1)[1] if "_" in data else ""
        norm = db.normalize_lang(raw)
        assert norm in db.SUPPORTED_LANGS
        assert norm == expected


# ─── callback_data unchanged (language keyboard) ──────────────────────────────
def test_lang_keyboard_callback_data_unchanged():
    from handlers.utils import lang_kb
    kb = lang_kb()
    datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert datas == ["lang_az", "lang_ru", "lang_en",
                     "lang_tr", "lang_kz", "lang_uz", "lang_ar"]
