"""The partner button under a finished forecast.

Two buttons sit side by side there: back into the match menu, and the partner
list. Two things must hold: partners never appear on an error message, and a
deployment with no partners configured sees no change at all. Offline.
"""
import types

import pytest

import db
import handlers.forecast as fc
from translations import T


# ─── the partner list keyboard ────────────────────────────────────────────────

def test_list_has_one_button_per_partner(monkeypatch):
    import config
    monkeypatch.setattr(config, "PARTNERS", [
        ("Mostbet", "https://a.example"), ("Topaz", "https://b.example")])
    monkeypatch.setattr(config, "PARTNER_REDIRECT_BASE", "")
    kb = fc._partner_list_kb(1)
    assert [b.text for row in kb.inline_keyboard for b in row] == ["Mostbet", "Topaz"]


def test_unnamed_partner_uses_the_generic_caption(temp_db, monkeypatch):
    import config
    temp_db.db_ensure(770001, "u", "ru")
    monkeypatch.setattr(config, "PARTNERS", [("", "https://a.example")])
    monkeypatch.setattr(config, "PARTNER_REDIRECT_BASE", "")
    assert fc._partner_list_kb(770001).inline_keyboard[0][0].text == T["ru"]["partners_btn"]


def test_list_buttons_go_through_the_click_redirect_when_enabled(monkeypatch):
    import config
    monkeypatch.setattr(config, "PARTNERS", [("Mostbet", "https://a.example")])
    monkeypatch.setattr(config, "PARTNER_REDIRECT_BASE", "https://dash.example")
    kb = fc._partner_list_kb(42)
    assert kb.inline_keyboard[0][0].url == "https://dash.example/r/Mostbet?u=42"


# ─── the button caption ───────────────────────────────────────────────────────

@pytest.mark.parametrize("lang", sorted(db.SUPPORTED_LANGS))
def test_button_caption_exists_in_every_language(lang):
    assert T[lang]["partners_cta_btn"].strip()


# ─── end to end through _generate_forecast ────────────────────────────────────

class _StatusMsg:
    def __init__(self):
        self.text = None
        self.markup = None

    async def edit_text(self, text, **kw):
        self.text = text
        self.markup = kw.get("reply_markup")


@pytest.fixture
def forecast_env(monkeypatch, temp_db):
    """Stub everything _generate_forecast reaches for except the code path."""
    import config
    monkeypatch.setattr(config, "APIFOOTBALL_KEY", "")
    monkeypatch.setattr(config, "PARTNER_REDIRECT_BASE", "")
    monkeypatch.setattr(config, "PARTNERS", [
        ("Mostbet", "https://a.example"), ("Topaz", "https://b.example")])

    async def _no_search(*a, **k):
        return []
    monkeypatch.setattr(fc, "search_match", _no_search)
    return monkeypatch


def _ctx():
    return types.SimpleNamespace(user_data={
        "pending_content": [{"type": "text", "text": "Barcelona Real"}],
        "pending_text": "Barcelona Real",
        "parsed_teams": ("Barcelona", "Real"),
        "odds_attached": True,
    }, bot=None)


async def test_successful_forecast_carries_partner_links(forecast_env, temp_db):
    uid = 770100
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)

    async def _reply(*a, **k):
        return "Прогноз: победа хозяев"
    forecast_env.setattr(fc, "claude_forecast", _reply)

    msg = _StatusMsg()
    await fc._generate_forecast(uid, _ctx(), msg)

    # Two buttons, one row: the menu CTA and the partner list.
    assert msg.markup.inline_keyboard[-1] == msg.markup.inline_keyboard[-1]
    labels = [b.text for b in msg.markup.inline_keyboard[-1]]
    assert labels == [T["ru"]["ev_more_matches"], T["ru"]["partners_cta_btn"]]
    # The partner list is behind the button, not spilled into the forecast.
    assert "Mostbet" not in msg.text


async def test_failed_forecast_carries_no_partner_links(forecast_env, temp_db):
    """Bookmaker links under an error message would be tone-deaf — and would
    also be counted as a successful forecast by the metrics."""
    uid = 770101
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)

    from translations import tr

    async def _fail(*a, **k):
        return tr(uid, "api_error")
    forecast_env.setattr(fc, "claude_forecast", _fail)

    msg = _StatusMsg()
    await fc._generate_forecast(uid, _ctx(), msg)

    labels = [b.text for row in msg.markup.inline_keyboard for b in row]
    assert T["ru"]["partners_cta_btn"] not in labels


async def test_forecast_unchanged_when_no_partners_configured(forecast_env, temp_db):
    import config
    uid = 770102
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    forecast_env.setattr(config, "PARTNERS", [])

    async def _reply(*a, **k):
        return "Прогноз"
    forecast_env.setattr(fc, "claude_forecast", _reply)

    msg = _StatusMsg()
    await fc._generate_forecast(uid, _ctx(), msg)

    labels = [b.text for row in msg.markup.inline_keyboard for b in row]
    assert labels == [T["ru"]["ev_more_matches"]]


async def test_more_matches_button_still_comes_first(forecast_env, temp_db):
    """Partner links are appended, never at the expense of the existing CTA."""
    uid = 770103
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)

    async def _reply(*a, **k):
        return "Прогноз"
    forecast_env.setattr(fc, "claude_forecast", _reply)

    msg = _StatusMsg()
    await fc._generate_forecast(uid, _ctx(), msg)
    assert msg.markup.inline_keyboard[0][0].text == T["ru"]["ev_more_matches"]
