"""'Our partners' menu button: shown only when partners are configured, opens
one inline link button per partner. Offline."""
import types

import config
import handlers.utils as hu
import handlers.forecast as fc
from translations import T


# ─── env parsing ──────────────────────────────────────────────────────────────

def test_parse_partners_multiple_labeled():
    raw = "Mostbet | https://mostbet.com\n1xBet | https://1xbet.com;Pin | https://pin.example"
    assert config._parse_partners(raw, "") == [
        ("Mostbet", "https://mostbet.com"),
        ("1xBet", "https://1xbet.com"),
        ("Pin", "https://pin.example"),
    ]


def test_parse_partners_bare_url_has_no_label():
    assert config._parse_partners("https://x.example", "") == [("", "https://x.example")]


def test_parse_partners_legacy_single_url_fallback():
    assert config._parse_partners("", "https://legacy.example") == [("", "https://legacy.example")]


def test_parse_partners_drops_non_http():
    assert config._parse_partners("Bad | ftp://x\nOk | https://ok.example", "") == [
        ("Ok", "https://ok.example")]


# ─── menu button visibility ───────────────────────────────────────────────────

def test_partners_button_hidden_without_partners(temp_db, partners):
    temp_db.db_ensure(970001, "u", "ru")
    partners([])
    labels = [b.text for row in hu.main_menu(970001).keyboard for b in row]
    assert T["ru"]["menu_partners"] not in labels


def test_partners_button_shown_with_partners(temp_db, partners):
    temp_db.db_ensure(970002, "u", "ru")
    partners([("A", "https://a.example")])
    labels = [b.text for row in hu.main_menu(970002).keyboard for b in row]
    assert T["ru"]["menu_partners"] in labels


def test_disabled_partner_disappears_from_the_menu(temp_db, partners):
    """Disabling in the dashboard must remove the button for the next render —
    no restart, no cached module constant."""
    temp_db.db_ensure(970005, "u", "ru")
    partners([("A", "https://a.example")])
    assert T["ru"]["menu_partners"] in [
        b.text for row in hu.main_menu(970005).keyboard for b in row]
    pid = temp_db.db_list_partners()[0]["id"]
    temp_db.db_partner_update(pid, is_active=False)
    assert T["ru"]["menu_partners"] not in [
        b.text for row in hu.main_menu(970005).keyboard for b in row]


# ─── tap → link buttons ───────────────────────────────────────────────────────

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


async def test_partners_tap_lists_all_partner_buttons(temp_db, partners):
    uid = 970003
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    partners([
        ("Mostbet", "https://mostbet.com"),
        ("1xBet", "https://1xbet.com"),
        ("Pin", "https://pin.example"),
    ])
    upd = _update(uid, T["ru"]["menu_partners"])
    await fc.handle_msg(upd, types.SimpleNamespace(user_data={}, bot=None))
    assert upd.message.replies
    text, kb = upd.message.replies[0]
    assert text == T["ru"]["partners_text"]
    pairs = [(b.text, b.url) for row in kb.inline_keyboard for b in row]
    assert pairs == [
        ("Mostbet", "https://mostbet.com"),
        ("1xBet", "https://1xbet.com"),
        ("Pin", "https://pin.example"),
    ]


async def test_bare_url_partner_gets_a_host_label(temp_db, partners, monkeypatch):
    """A bare URL in the env (no "Name |" in front) has no label to show. The
    bootstrap turns it into the host name, so the button is still readable —
    the DB never holds a nameless partner."""
    uid = 970004
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    partners([])
    monkeypatch.setattr(config, "PARTNERS", [("", "https://x.example")])
    with temp_db.con() as c:
        c.execute("DELETE FROM _migrations WHERE key='partners_env_bootstrap'")
    temp_db._bootstrap_partners_from_env()
    upd = _update(uid, T["ru"]["menu_partners"])
    await fc.handle_msg(upd, types.SimpleNamespace(user_data={}, bot=None))
    _, kb = upd.message.replies[0]
    btn = kb.inline_keyboard[0][0]
    assert btn.text == "x.example" and btn.url == "https://x.example"
