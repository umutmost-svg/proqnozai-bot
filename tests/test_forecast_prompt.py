"""Offline tests for _build_system_prompt: the pure prompt-assembly helper.

The forecast used to request the rich data-dependent sections (recent matches,
injuries, per-team form) even when no real data was attached, so the model
emitted a "data unavailable" placeholder line per section — several
near-identical lines of noise. The no-data branch must instead produce a lean,
odds-only prompt with a single estimative marker, WITHOUT weakening any
anti-fabrication directive.
"""
import pytest

from handlers.forecast import _build_system_prompt, _DATA_NOTE
from translations import T

ALL_LANGS = ["az", "ru", "en", "tr", "kz", "uz", "ar"]

# Substrings that appear ONLY in the rich (real-data) block.
_RICH_ONLY = ("Extend the format with these sections", "list each team's last 5", "~18-24 lines")
# Substrings that appear ONLY in the lean (no-data) block.
_LEAN_ONLY = ("OMIT the per-team", "~8–10 lines total")


@pytest.mark.parametrize("lang", ALL_LANGS)
def test_no_data_prompt_is_lean_and_omits_placeholder_sections(lang):
    p = _build_system_prompt(lang, "beginner", has_real_data=False)
    # No rich data-dependent section is requested → no per-section placeholders.
    for marker in _RICH_ONLY:
        assert marker not in p, f"[{lang}] leaked rich marker: {marker!r}"
    assert "OMIT the per-team" in p


@pytest.mark.parametrize("lang", ALL_LANGS)
def test_no_data_prompt_keeps_anti_fabrication_and_estimative_marker(lang):
    p = _build_system_prompt(lang, "beginner", has_real_data=False)
    # Honesty guarantee must survive the trim (CLAUDE.md: no invented facts +
    # an explicit "(оценочно)"/estimative marker is required when data is absent).
    assert "Do NOT invent" in p
    assert "(оценочно)" in p  # (оценочно)


@pytest.mark.parametrize("lang", ALL_LANGS)
def test_real_data_prompt_keeps_rich_sections_and_data_note(lang):
    p = _build_system_prompt(lang, "beginner", has_real_data=True)
    for marker in _RICH_ONLY:
        assert marker in p, f"[{lang}] missing rich marker: {marker!r}"
    for marker in _LEAN_ONLY:
        assert marker not in p, f"[{lang}] leaked lean marker: {marker!r}"
    assert _DATA_NOTE[lang] in p


@pytest.mark.parametrize("lang", ALL_LANGS)
def test_both_modes_include_base_prompt_and_language_directive(lang):
    base = T[lang]["system_prompt"]
    for has_data in (True, False):
        p = _build_system_prompt(lang, "beginner", has_data)
        assert p.startswith(base)
        assert "OUTPUT LANGUAGE" in p


@pytest.mark.parametrize("lang", ALL_LANGS)
def test_odds_integrity_directive_present_in_both_modes(lang):
    """The model must never fabricate/derive an odds number — it may only echo a
    value from the provided real-odds block, and must omit the figure when none
    was supplied. This guard must hold whether or not enrichment data exists."""
    for has_data in (True, False):
        p = _build_system_prompt(lang, "beginner", has_data)
        assert "ODDS INTEGRITY" in p
        assert "NEVER compute, derive, estimate or invent an odds value" in p


def test_unknown_language_falls_back_to_russian_base():
    p = _build_system_prompt("xx", "beginner", has_real_data=False)
    assert p.startswith(T["ru"]["system_prompt"])
    assert "OMIT the per-team" in p


def test_experience_hint_applied_and_optional():
    # A known experience adds the profile hint; an unknown one degrades cleanly.
    expert = _build_system_prompt("en", "expert", has_real_data=False)
    unknown = _build_system_prompt("en", "nonesuch", has_real_data=False)
    assert "Profile: expert" in expert
    assert "Profile:" not in unknown
