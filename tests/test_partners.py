"""'Our partners' menu button: shown only when PARTNERS_URL is set, and opens
the configured link. Offline."""
import types

import handlers.utils as hu
import handlers.forecast as fc
from translations import T


def test_partners_button_hidden_without_url(temp_db, monkeypatch):
    temp_db.db_ensure(970001, "u", "ru")
    monkeypatch.setattr(hu, "PARTNERS_URL", "")
    labels = [b.text for row in hu.main_menu(970001).keyboard for b in row]
    assert T["ru"]["menu_partners"] not in labels


def test_partners_button_shown_with_url(temp_db, monkeypatch):
    temp_db.db_ensure(970002, "u", "ru")
    monkeypatch.setattr(hu, "PARTNERS_URL", "https://partner.example")
    labels = [b.text for row in hu.main_menu(970002).keyboard for b in row]
    assert T["ru"]["menu_partners"] in labels


class _Msg:
    def __init__(self, text):
        self.text = text; self.caption = None; self.photo = None
        self.replies = []
        self.chat = types.SimpleNamespace(send_action=self._noop)

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw.get("reply_markup")))

    async def _noop(self, *a, **k):
        pass


def _update(uid, text):
    return types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=uid, username="u", full_name="U", language_code="ru"),
        message=_Msg(text))


async def test_partners_tap_sends_link_button(temp_db, monkeypatch):
    import config
    uid = 970003
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    monkeypatch.setattr(config, "PARTNERS_URL", "https://partner.example")
    upd = _update(uid, T["ru"]["menu_partners"])
    await fc.handle_msg(upd, types.SimpleNamespace(user_data={}, bot=None))
    assert upd.message.replies
    text, kb = upd.message.replies[0]
    assert text == T["ru"]["partners_text"]
    assert kb is not None
    url = kb.inline_keyboard[0][0].url
    assert url == "https://partner.example"
