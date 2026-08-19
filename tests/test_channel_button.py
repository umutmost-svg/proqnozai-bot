"""The "our channel" menu button.

A reply-keyboard button cannot carry a url — only inline buttons can — so this
one opens a message holding an inline link. The button is therefore shown only
when there is an address to open: PROMO_CHANNEL_URL, or a "@name" channel that
can be linked without one. A "-100…" id is a private channel with no public
address, and promising a link that does not exist is worse than no button.
"""
import types

import pytest

import handlers.utils as hu
import handlers.forecast as fc
from translations import T, tr


def _labels(uid):
    return [b.text for row in hu.main_menu(uid).keyboard for b in row]


class _Msg:
    def __init__(self, text):
        self.text = text
        self.caption = None
        self.sent = []

    async def reply_text(self, text, reply_markup=None, **kw):
        self.sent.append((text, reply_markup))


def _update(uid, text):
    return types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=uid, username="u",
                                             full_name="U", language_code="ru"),
        message=_Msg(text), effective_chat=types.SimpleNamespace(id=uid))


LANGS = ["az", "ru", "en", "tr", "kz", "uz", "ar"]


# ── channel_url resolution ───────────────────────────────────────────────────

def test_explicit_url_wins(monkeypatch):
    monkeypatch.setattr(hu, "PROMO_CHANNEL_URL", "https://t.me/joinchat/abc")
    monkeypatch.setattr(hu, "PROMO_CHANNEL", "@public")
    assert hu.channel_url() == "https://t.me/joinchat/abc"


def test_a_named_channel_is_linked_without_an_explicit_url(monkeypatch):
    monkeypatch.setattr(hu, "PROMO_CHANNEL_URL", "")
    monkeypatch.setattr(hu, "PROMO_CHANNEL", "@proqnozai")
    assert hu.channel_url() == "https://t.me/proqnozai"


def test_an_id_only_private_channel_has_no_link(monkeypatch):
    monkeypatch.setattr(hu, "PROMO_CHANNEL_URL", "")
    monkeypatch.setattr(hu, "PROMO_CHANNEL", "-1001234567890")
    assert hu.channel_url() == ""


def test_nothing_configured_means_no_link(monkeypatch):
    monkeypatch.setattr(hu, "PROMO_CHANNEL_URL", "")
    monkeypatch.setattr(hu, "PROMO_CHANNEL", "")
    assert hu.channel_url() == ""


def test_the_promo_gate_resolves_the_same_link(temp_db, monkeypatch):
    """One home for the rule: the subscription gate and the menu button must
    not be able to point at different places."""
    import handlers.promo as promo
    monkeypatch.setattr(hu, "PROMO_CHANNEL_URL", "https://t.me/shared")
    kb = promo._subscribe_kb(972000)
    urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
    assert urls == ["https://t.me/shared"]


# ── the button in the keyboard ───────────────────────────────────────────────

def test_the_button_appears_when_a_link_exists(temp_db, monkeypatch):
    uid = 972001
    temp_db.db_ensure(uid, "u", "ru")
    monkeypatch.setattr(hu, "PROMO_CHANNEL_URL", "https://t.me/proqnozai")
    assert T["ru"]["menu_channel"] in _labels(uid)


def test_the_button_is_absent_without_a_link(temp_db, monkeypatch):
    uid = 972002
    temp_db.db_ensure(uid, "u", "ru")
    monkeypatch.setattr(hu, "PROMO_CHANNEL_URL", "")
    monkeypatch.setattr(hu, "PROMO_CHANNEL", "-1001234567890")
    assert T["ru"]["menu_channel"] not in _labels(uid)


def test_the_button_does_not_depend_on_a_promo_campaign(temp_db, monkeypatch):
    """Unlike the bonus button: the channel is worth showing whether or not
    there is a code to hand out."""
    uid = 972003
    temp_db.db_ensure(uid, "u", "ru")
    monkeypatch.setattr(hu, "PROMO_CHANNEL_URL", "https://t.me/proqnozai")
    with temp_db.con() as c:
        c.execute("DELETE FROM promo_campaign")
    labels = _labels(uid)
    assert T["ru"]["menu_channel"] in labels
    assert T["ru"]["menu_get_promo"] not in labels


@pytest.mark.parametrize("lang", LANGS)
def test_every_language_has_the_label_and_the_intro(lang):
    assert T[lang]["menu_channel"].strip()
    assert T[lang]["channel_intro"].strip()


@pytest.mark.parametrize("lang", LANGS)
def test_the_button_renders_in_every_language(temp_db, monkeypatch, lang):
    uid = 972010 + LANGS.index(lang)
    temp_db.db_ensure(uid, "u")
    # db_ensure takes a TELEGRAM language_code and runs it through detect_lang,
    # which folds kz/uz/ar onto ru — the app language has to be set directly.
    temp_db.db_set(uid, "lang", lang)
    monkeypatch.setattr(hu, "PROMO_CHANNEL_URL", "https://t.me/proqnozai")
    assert T[lang]["menu_channel"] in _labels(uid)


# ── tapping it ───────────────────────────────────────────────────────────────

async def test_tapping_sends_the_link_as_an_inline_button(temp_db, monkeypatch):
    uid = 972101
    temp_db.db_ensure(uid, "u", "ru")
    temp_db.db_set(uid, "is_registered", 1)
    monkeypatch.setattr(hu, "PROMO_CHANNEL_URL", "https://t.me/proqnozai")

    update = _update(uid, T["ru"]["menu_channel"])
    await fc.handle_msg(update, types.SimpleNamespace(bot=None, user_data={}))
    text, markup = update.message.sent[0]
    assert text == tr(uid, "channel_intro")
    urls = [b.url for row in markup.inline_keyboard for b in row if b.url]
    assert urls == ["https://t.me/proqnozai"]


async def test_a_stale_button_without_a_link_answers_nothing(temp_db, monkeypatch):
    """A keyboard already sitting in a chat keeps sending the label after the
    channel is unconfigured; it must not raise or send a dead button."""
    uid = 972102
    temp_db.db_ensure(uid, "u", "ru")
    temp_db.db_set(uid, "is_registered", 1)
    monkeypatch.setattr(hu, "PROMO_CHANNEL_URL", "")
    monkeypatch.setattr(hu, "PROMO_CHANNEL", "")

    update = _update(uid, T["ru"]["menu_channel"])
    await fc.handle_msg(update, types.SimpleNamespace(bot=None, user_data={}))
    assert update.message.sent == []
