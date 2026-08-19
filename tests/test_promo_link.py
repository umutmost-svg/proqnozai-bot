"""The promo message links the partner it names.

A code the user cannot act on without going and finding the site themselves is
half a bonus, so the partner's name is a link — through the same tracked
redirect its button already uses, so the click lands in the same numbers.
"""
import types

import handlers.promo as promo
import handlers.utils as hu


class _Bot:
    async def get_chat_member(self, chat, uid):
        return types.SimpleNamespace(status="member")


class _Msg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, reply_markup=None, **kw):
        self.sent.append((text, kw))


async def _claim(clean, uid, monkeypatch):
    clean.db_ensure(uid, "u")
    clean.db_set(uid, "is_registered", 1)
    monkeypatch.setattr(promo, "PROMO_CHANNEL", "@chan")
    msg = _Msg()
    update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=uid),
                                   message=msg)
    await promo.promo_cmd(update, types.SimpleNamespace(bot=_Bot()))
    return msg.sent[0]


async def test_the_partner_name_is_a_link(clean, monkeypatch):
    clean.db_partner_add("Mostbet", "https://mostbet.example")
    clean.db_promo_pool_import("Mostbet", ["MBWIN7K"])
    text, kw = await _claim(clean, 995001, monkeypatch)
    assert '<a href="https://mostbet.example">Mostbet</a>' in text
    assert "<code>MBWIN7K</code>" in text
    assert kw["parse_mode"] == "HTML"


async def test_the_link_goes_through_the_tracked_redirect(clean, monkeypatch):
    """With a redirect base configured the link must be OUR url, so the click
    is counted before the user is forwarded on."""
    clean.db_partner_add("Mostbet", "https://mostbet.example")
    clean.db_promo_pool_import("Mostbet", ["MB-1"])
    monkeypatch.setattr("config.PARTNER_REDIRECT_BASE", "https://dash.example")
    monkeypatch.setattr("config.DASHBOARD_TOKEN", "s3cret")
    text, _ = await _claim(clean, 995002, monkeypatch)
    assert "https://dash.example/r/Mostbet?u=995002&amp;s=" in text


async def test_the_signature_matches_the_partner_button(clean, monkeypatch):
    """Both sides must sign identically, or one of them stops being counted."""
    clean.db_partner_add("Mostbet", "https://mostbet.example")
    monkeypatch.setattr("config.PARTNER_REDIRECT_BASE", "https://dash.example")
    monkeypatch.setattr("config.DASHBOARD_TOKEN", "s3cret")
    from partner_links import verify_click
    url = hu.partner_url("Mostbet", "https://mostbet.example", 995003)
    sig = url.split("s=")[1]
    assert verify_click("s3cret", "Mostbet", 995003, sig) is True


async def test_a_partner_without_a_live_row_stays_plain_text(clean, monkeypatch):
    """An orphan campaign has no partner to link to; it must not render a link
    pointing nowhere."""
    clean.db_set_promo_code("Ghost", "GH-1", 5)
    text, _ = await _claim(clean, 995004, monkeypatch)
    assert "GH-1" in text
    assert "<a href" not in text


async def test_an_archived_partner_is_not_linked(clean, monkeypatch):
    pid = clean.db_partner_add("Mostbet", "https://mostbet.example")
    clean.db_set_promo_code("Mostbet", "MB-1", 5)
    clean.db_partner_archive(pid)
    text, _ = await _claim(clean, 995005, monkeypatch)
    assert "MB-1" in text
    assert "<a href" not in text


async def test_a_disabled_partner_is_not_linked(clean, monkeypatch):
    pid = clean.db_partner_add("Mostbet", "https://mostbet.example")
    clean.db_set_promo_code("Mostbet", "MB-1", 5)
    clean.db_partner_update(pid, is_active=False)
    text, _ = await _claim(clean, 995006, monkeypatch)
    assert "<a href" not in text


async def test_several_partners_each_get_their_own_link(clean, monkeypatch):
    clean.db_partner_add("Mostbet", "https://mostbet.example")
    clean.db_partner_add("Topaz", "https://topaz.example")
    clean.db_promo_pool_import("Mostbet", ["MB-1"])
    clean.db_set_promo_code("Topaz", "TZ-1", 5)
    text, _ = await _claim(clean, 995007, monkeypatch)
    assert '<a href="https://mostbet.example">Mostbet</a>' in text
    assert '<a href="https://topaz.example">Topaz</a>' in text


# ── the message is HTML, so its free text has to survive being HTML ──────────

async def test_an_ampersand_in_the_code_does_not_break_the_message(clean, monkeypatch):
    clean.db_partner_add("Mostbet", "https://mostbet.example")
    clean.db_promo_pool_import("Mostbet", ["A&B<C"])
    text, _ = await _claim(clean, 995010, monkeypatch)
    assert "<code>A&amp;B&lt;C</code>" in text


async def test_a_partner_name_with_markup_characters_is_escaped(clean, monkeypatch):
    clean.db_partner_add("A&B", "https://ab.example")
    clean.db_set_promo_code("A&B", "CODE-1", 5)
    text, _ = await _claim(clean, 995011, monkeypatch)
    assert ">A&amp;B</a>" in text


async def test_the_url_is_escaped_inside_the_href(clean, monkeypatch):
    """The redirect carries &s=…; raw, that is an unterminated entity."""
    clean.db_partner_add("Mostbet", "https://mostbet.example")
    clean.db_set_promo_code("Mostbet", "MB-1", 5)
    monkeypatch.setattr("config.PARTNER_REDIRECT_BASE", "https://dash.example")
    monkeypatch.setattr("config.DASHBOARD_TOKEN", "s3cret")
    text, _ = await _claim(clean, 995012, monkeypatch)
    assert "&s=" not in text.replace("&amp;s=", "")


# ── the partner list button must keep working from its new home ─────────────

def test_partner_url_falls_back_to_the_plain_url(monkeypatch):
    monkeypatch.setattr("config.PARTNER_REDIRECT_BASE", "")
    assert hu.partner_url("M", "https://m.example", 1) == "https://m.example"


def test_partner_url_without_a_secret_is_unattributed(monkeypatch):
    monkeypatch.setattr("config.PARTNER_REDIRECT_BASE", "https://dash.example")
    monkeypatch.setattr("config.DASHBOARD_TOKEN", "")
    assert hu.partner_url("M", "https://m.example", 1) == "https://dash.example/r/M"


def test_forecast_still_uses_the_same_builder():
    """It moved to handlers.utils; forecast must not have kept a copy."""
    import handlers.forecast as fc
    assert fc.partner_url is hu.partner_url
