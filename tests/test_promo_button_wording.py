"""The promo button now offers a partner bonus for a bet, not "a promo code".

The label lives on a reply keyboard, so the old text keeps arriving from chats
the menu broadcast hasn't reached — those taps must still open the promo flow.
Offline.
"""
import types

import pytest

import db
import handlers.forecast as fc
import handlers.utils as hu
from translations import T


# ─── wording ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("lang", sorted(db.SUPPORTED_LANGS))
def test_every_language_has_the_new_label(lang):
    label = T[lang]["menu_get_promo"]
    assert label.strip()
    assert label not in fc._LEGACY_PROMO_LABELS, f"{lang} still uses the old wording"


@pytest.mark.parametrize("lang", sorted(db.SUPPORTED_LANGS))
def test_label_no_longer_promises_a_code(lang):
    """The button is about a partner bonus now; "promo code" wording is gone."""
    low = T[lang]["menu_get_promo"].lower()
    for stale in ("промокод", "promokod", "promo kod", "promo code", "رمز ترويجي"):
        assert stale not in low


def test_russian_wording():
    assert T["ru"]["menu_get_promo"] == "🎁 Бонус от партнёров на ставку"


# ─── menu placement is unchanged ──────────────────────────────────────────────

def test_button_shown_only_when_the_gate_channel_is_configured(temp_db, monkeypatch):
    uid = 940101
    temp_db.db_ensure(uid, "u", "ru")

    monkeypatch.setattr(hu, "PROMO_CHANNEL", "")
    labels = [b.text for row in hu.main_menu(uid).keyboard for b in row]
    assert T["ru"]["menu_get_promo"] not in labels

    monkeypatch.setattr(hu, "PROMO_CHANNEL", "@channel")
    labels = [b.text for row in hu.main_menu(uid).keyboard for b in row]
    assert T["ru"]["menu_get_promo"] in labels


# ─── routing: new and stale keyboards both work ───────────────────────────────

class _Msg:
    def __init__(self, text):
        self.text = text; self.caption = None; self.photo = None
        self.replies = []
        self.chat = types.SimpleNamespace(send_action=self._noop)

    async def reply_text(self, text, **kw):
        self.replies.append(text)

    async def _noop(self, *a, **k):
        pass


def _update(uid, text):
    return types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=uid, username="u", full_name="U",
                                             first_name="U", language_code="ru"),
        message=_Msg(text))


@pytest.fixture
def promo_calls(monkeypatch):
    seen = []

    async def _promo_cmd(update, context):
        seen.append(update.effective_user.id)
        await update.message.reply_text("promo flow")

    import handlers.promo as promo
    monkeypatch.setattr(promo, "promo_cmd", _promo_cmd)
    return seen


async def test_new_label_opens_the_promo_flow(temp_db, promo_calls):
    uid = 940102
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    upd = _update(uid, T["ru"]["menu_get_promo"])
    await fc.handle_msg(upd, types.SimpleNamespace(user_data={}, bot=None))
    assert promo_calls == [uid]


@pytest.mark.parametrize("stale", sorted(fc._LEGACY_PROMO_LABELS))
async def test_stale_keyboard_label_still_opens_the_promo_flow(temp_db, promo_calls, stale):
    """Until the broadcast lands, this is what most users are still tapping."""
    uid = 940103
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    upd = _update(uid, stale)
    await fc.handle_msg(upd, types.SimpleNamespace(user_data={}, bot=None))
    assert promo_calls == [uid]


def test_legacy_set_covers_every_language():
    """One stale label per language — a missing one means those users' taps
    silently fall through to the forecast flow."""
    assert len(fc._LEGACY_PROMO_LABELS) == len(db.SUPPORTED_LANGS)


# ─── the new keyboard actually gets delivered ─────────────────────────────────

def test_menu_broadcast_key_was_bumped():
    """The reply keyboard only changes when the bot sends a new one, and the
    broadcast runs once per key."""
    import main
    assert main.MENU_BROADCAST_KEY == "menu_broadcast_2026_08_promo_bonus_wording"
