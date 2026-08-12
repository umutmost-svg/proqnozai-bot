"""Offline tests for _build_system_prompt: the pure prompt-assembly helper.

The forecast used to request the rich data-dependent sections (recent matches,
injuries, per-team form) even when no real data was attached, so the model
emitted a "data unavailable" placeholder line per section — several
near-identical lines of noise. The no-data branch must instead produce a lean,
odds-only prompt, WITHOUT weakening any anti-fabrication directive.

A PARTIAL forecast is marked inside the probabilities heading — never by a
trailing caveat, which the product no longer prints at all.
"""
import pytest

import db
from handlers.forecast import _build_system_prompt, _DATA_NOTE
from translations import T

ALL_LANGS = sorted(db.SUPPORTED_LANGS)   # see tests/test_translations.py

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
def test_no_data_prompt_keeps_anti_fabrication(lang):
    """The never-invent directives stay. The trailing "(оценочно)" caveat does
    NOT: the forecast now ends on the recommendation, and every closing
    commentary line was removed on request."""
    p = _build_system_prompt(lang, "beginner", has_real_data=False)
    assert "Do NOT invent" in p
    assert "NEVER compute, derive, estimate or invent an odds value" in p
    assert "(оценочно)" not in p


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


# ─── PARTIAL is marked inside the forecast, never under it ────────────────────

@pytest.mark.parametrize("lang", ALL_LANGS)
@pytest.mark.parametrize("has_real_data", [False, True])
def test_partial_marks_the_probabilities_block(lang, has_real_data):
    """PARTIAL has two shapes — odds without verified data, and verified data
    without odds. Both must be marked, so the marking follows readiness rather
    than the data mode."""
    p = _build_system_prompt(lang, "beginner", has_real_data, is_partial=True)
    assert "PARTIAL DATA" in p
    assert "Оценка вероятностей" in p


@pytest.mark.parametrize("lang", ALL_LANGS)
@pytest.mark.parametrize("has_real_data", [False, True])
def test_partial_marking_forbids_a_footer(lang, has_real_data):
    p = _build_system_prompt(lang, "beginner", has_real_data, is_partial=True)
    assert "add NO caveat line, NO warning, NO closing remark, NO" in p
    assert "must end on the ⚡ bet line" in p


@pytest.mark.parametrize("lang", ALL_LANGS)
def test_ready_forecast_carries_no_estimative_marking(lang):
    """READY has both odds and verified data — nothing to qualify."""
    p = _build_system_prompt(lang, "beginner", has_real_data=True, is_partial=False)
    assert "PARTIAL DATA" not in p
    assert "Оценка вероятностей" not in p


@pytest.mark.parametrize("lang", ALL_LANGS)
@pytest.mark.parametrize("has_real_data", [False, True])
@pytest.mark.parametrize("is_partial", [False, True])
def test_anti_hallucination_directives_survive_every_combination(lang, has_real_data,
                                                                 is_partial):
    """Form, lineups, injuries, results and odds may never be invented — in any
    data mode, with or without the partial marking."""
    p = _build_system_prompt(lang, "beginner", has_real_data, is_partial=is_partial)
    assert "NEVER compute, derive, estimate or invent an odds value" in p
    assert "cite an odds value (@X.XX) ONLY if that exact value" in p
    if has_real_data:
        assert "NEVER claim a team has no" in p          # injuries/absences
        assert "using ONLY the provided computed metrics" in p   # form
        assert _DATA_NOTE[lang] in p                     # do not invent results
    else:
        assert "Do NOT invent any of it" in p
        assert "NO real data (form, H2H, injuries, lineups, statistics)" in p
