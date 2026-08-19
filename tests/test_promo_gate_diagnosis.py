"""Why the bonus button failed, and how an operator finds out.

The gate answered "can't verify your subscription" for two unrelated reasons —
Telegram refused the getChatMember call, or no channel was configured at all —
and the second one wrote nothing to the log, so looking there taught the
operator nothing. They are now distinct, in the reply and in the log, and
/promodiag reports the raw answer without touching the host's logs.
"""
import logging
import types

import pytest

import config
import handlers.promo as promo
from translations import tr


class _Bot:
    def __init__(self, exc=None, status="member", is_member=False):
        self.exc, self.status, self.is_member = exc, status, is_member
        self.calls = []

    async def get_chat_member(self, chat, uid):
        self.calls.append((chat, uid))
        if self.exc:
            raise self.exc
        return types.SimpleNamespace(status=self.status, is_member=self.is_member)


class _Msg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, reply_markup=None, **kw):
        self.sent.append(text)


async def _tap_bonus(clean, uid, channel, bot, monkeypatch):
    clean.db_ensure(uid, "u")
    clean.db_set(uid, "is_registered", 1)
    clean.db_set_promo_code("Mostbet", f"CODE-{uid}", 10)
    monkeypatch.setattr(promo, "PROMO_CHANNEL", channel)
    msg = _Msg()
    update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=uid),
                                   message=msg)
    await promo.promo_cmd(update, types.SimpleNamespace(bot=bot))
    return msg.sent


# ── the two causes are no longer one message ─────────────────────────────────

async def test_no_channel_configured_says_unavailable(clean, monkeypatch):
    sent = await _tap_bonus(clean, 991001, "", _Bot(), monkeypatch)
    assert sent[0] == tr(991001, "promo_unavailable")


async def test_no_channel_configured_is_logged(clean, monkeypatch, caplog):
    """This branch used to return silently — the operator grepped the logs,
    found nothing, and concluded the logs were fine."""
    caplog.set_level(logging.WARNING)
    await _tap_bonus(clean, 991002, "", _Bot(), monkeypatch)
    assert any("PROMO_CHANNEL unset" in r.message for r in caplog.records)


async def test_a_telegram_failure_still_says_check_failed(clean, monkeypatch):
    bot = _Bot(exc=RuntimeError("Chat not found"))
    sent = await _tap_bonus(clean, 991003, "@chan", bot, monkeypatch)
    assert sent[0] == tr(991003, "promo_check_failed")


async def test_a_telegram_failure_logs_the_channel_and_the_reason(
        clean, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    bot = _Bot(exc=RuntimeError("Member list is inaccessible"))
    await _tap_bonus(clean, 991004, "@chan", bot, monkeypatch)
    logged = " ".join(r.message for r in caplog.records)
    assert "Member list is inaccessible" in logged
    assert "@chan" in logged                      # which channel it tried


# ── membership statuses ──────────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["member", "administrator", "creator"])
async def test_subscribed_statuses_get_the_code(clean, monkeypatch, status):
    uid = 991010 + len(status)
    sent = await _tap_bonus(clean, uid, "@chan", _Bot(status=status), monkeypatch)
    assert f"CODE-{uid}" in sent[0]


async def test_a_restricted_but_still_present_member_is_subscribed(clean, monkeypatch):
    """Muted in the channel is still in the channel; it was refused as if the
    user had left."""
    uid = 991020
    bot = _Bot(status="restricted", is_member=True)
    sent = await _tap_bonus(clean, uid, "@chan", bot, monkeypatch)
    assert f"CODE-{uid}" in sent[0]


async def test_a_restricted_member_who_left_is_not_subscribed(clean, monkeypatch):
    uid = 991021
    bot = _Bot(status="restricted", is_member=False)
    sent = await _tap_bonus(clean, uid, "@chan", bot, monkeypatch)
    assert f"CODE-{uid}" not in sent[0]


@pytest.mark.parametrize("status", ["left", "kicked"])
async def test_absent_statuses_are_asked_to_subscribe(clean, monkeypatch, status):
    uid = 991030 + len(status)
    sent = await _tap_bonus(clean, uid, "@chan", _Bot(status=status), monkeypatch)
    assert sent[0] != tr(uid, "promo_check_failed")     # not an error
    assert f"CODE-{uid}" not in sent[0]                 # and no code


# ── the value an operator typed ──────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("@proqnozai",                 "@proqnozai"),
    ("proqnozai",                  "@proqnozai"),      # forgotten "@"
    ('"@proqnozai"',               "@proqnozai"),      # quotes from a paste
    ("'@proqnozai'",               "@proqnozai"),
    ("  @proqnozai  ",             "@proqnozai"),
    ("https://t.me/proqnozai",     "@proqnozai"),      # the link, not the name
    ("t.me/proqnozai",             "@proqnozai"),
    ("-1001234567890",             "-1001234567890"),  # a private channel id
    ("",                           ""),
    ("   ",                        ""),
    ("https://t.me/+AbCdEf",       ""),                # invite link: not a target
])
def test_the_channel_value_is_normalised(raw, expected):
    assert config._normalize_channel(raw) == expected


def test_an_invite_link_is_not_mistaken_for_a_channel():
    """It identifies no chat getChatMember can resolve, so treating it as one
    would produce "Chat not found" forever. It belongs in PROMO_CHANNEL_URL."""
    assert config._normalize_channel("https://t.me/joinchat/AAAA") == ""


# ── /promodiag ───────────────────────────────────────────────────────────────

async def _diag(clean, monkeypatch, channel, bot, admin=True):
    uid = 991100
    monkeypatch.setattr(promo, "PROMO_CHANNEL", channel)
    monkeypatch.setattr(promo, "ADMIN_ID", uid if admin else uid + 1)
    msg = _Msg()
    update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=uid),
                                   message=msg)
    await promo.promodiag_cmd(update, types.SimpleNamespace(bot=bot))
    return msg.sent


async def test_promodiag_is_admin_only(clean, monkeypatch):
    assert await _diag(clean, monkeypatch, "@chan", _Bot(), admin=False) == []


async def test_promodiag_reports_a_working_gate(clean, monkeypatch):
    clean.db_set_promo_code("Mostbet", "MB-1", 10)
    out = (await _diag(clean, monkeypatch, "@chan", _Bot(status="member")))[0]
    assert "@chan" in out
    assert "getChatMember: OK" in out
    assert "the gate works" in out


async def test_promodiag_reports_the_raw_telegram_error(clean, monkeypatch):
    bot = _Bot(exc=RuntimeError("Member list is inaccessible"))
    out = (await _diag(clean, monkeypatch, "@chan", bot))[0]
    assert "FAILED" in out
    assert "Member list is inaccessible" in out
    assert "not an ADMIN" in out                # the usual cause, named


async def test_promodiag_names_an_unset_channel(clean, monkeypatch):
    out = (await _diag(clean, monkeypatch, "", _Bot()))[0]
    assert "not set" in out
    assert "no channel configured" in out


async def test_promodiag_lists_a_pool_campaign(clean, monkeypatch):
    clean.db_promo_pool_import("Mostbet", ["A-1", "A-2", "A-3"])
    out = (await _diag(clean, monkeypatch, "@chan", _Bot(status="member")))[0]
    assert "pool" in out
    assert "0/3 used" in out


async def test_promodiag_says_when_no_campaign_is_live(clean, monkeypatch):
    out = (await _diag(clean, monkeypatch, "@chan", _Bot(status="member")))[0]
    assert "none live" in out
