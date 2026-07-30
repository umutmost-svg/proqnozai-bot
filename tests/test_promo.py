"""Channel-gated promo: ONE shared code with a total-use cap (e.g. one code for
500 users). Offline — get_chat_member is faked. The campaign is a single global
row, so each test resets it first."""
import types

import handlers.promo as promo
from translations import tr


def _reset(temp_db):
    with temp_db.con() as c:
        c.execute("DELETE FROM promo_campaign")
        c.execute("DELETE FROM promo_claims")


# ─── Campaign + capped claims ─────────────────────────────────────────────────

def test_no_campaign_by_default(temp_db):
    _reset(temp_db)
    assert temp_db.db_get_promo_campaign() is None
    assert temp_db.db_claim_promo(1) is None


def test_same_code_to_many_users_until_cap(temp_db):
    _reset(temp_db)
    temp_db.db_set_promo_campaign("WELCOME500", 3)
    assert temp_db.db_claim_promo(101) == "WELCOME500"
    assert temp_db.db_claim_promo(102) == "WELCOME500"   # same shared code
    assert temp_db.db_claim_promo(103) == "WELCOME500"
    assert temp_db.db_claim_promo(104) is None            # cap of 3 reached


def test_claim_is_idempotent_per_user(temp_db):
    _reset(temp_db)
    temp_db.db_set_promo_campaign("CODE", 1)
    assert temp_db.db_claim_promo(200) == "CODE"
    assert temp_db.db_claim_promo(200) == "CODE"          # repeat → same, no extra use
    assert temp_db.db_promo_stats()["claimed"] == 1       # still counts as 1


def test_setting_new_code_resets_the_count(temp_db):
    _reset(temp_db)
    temp_db.db_set_promo_campaign("OLD", 1)
    temp_db.db_claim_promo(300)                            # OLD exhausted
    assert temp_db.db_claim_promo(301) is None
    temp_db.db_set_promo_campaign("NEW", 1)               # fresh campaign
    assert temp_db.db_claim_promo(301) == "NEW"           # counts reset


def test_promo_stats(temp_db):
    _reset(temp_db)
    temp_db.db_set_promo_campaign("S", 500)
    temp_db.db_claim_promo(1); temp_db.db_claim_promo(2)
    assert temp_db.db_promo_stats() == {
        "code": "S", "max_uses": 500, "claimed": 2, "available": 498}


# ─── Gate flow ────────────────────────────────────────────────────────────────

class _FakeBot:
    def __init__(self, status):
        self._status = status

    async def get_chat_member(self, chat, uid):
        if self._status == "error":
            raise RuntimeError("bot is not an admin of the channel")
        return types.SimpleNamespace(status=self._status)


def _ctx(status):
    return types.SimpleNamespace(bot=_FakeBot(status))


async def _run(temp_db, uid, status, monkeypatch, channel="@test"):
    monkeypatch.setattr(promo, "PROMO_CHANNEL", channel)
    sent = []

    async def reply(text, reply_markup=None):
        sent.append((text, reply_markup))

    await promo._run_promo(_ctx(status), uid, reply)
    return sent


async def test_gate_requires_registration(temp_db, monkeypatch):
    uid = 950001
    temp_db.db_ensure(uid, "u", "en")               # exists but is_registered=0
    sent = await _run(temp_db, uid, "member", monkeypatch)
    assert sent[0][0] == tr(uid, "need_reg")


async def test_gate_unavailable_without_active_campaign(temp_db, monkeypatch):
    uid = 950002
    temp_db.db_ensure(uid, "u", "en"); temp_db.db_set(uid, "is_registered", 1)
    _reset(temp_db)                                  # no campaign
    sent = await _run(temp_db, uid, "member", monkeypatch)
    assert sent[0][0] == tr(uid, "promo_unavailable")


async def test_gate_prompts_subscription_when_not_member(temp_db, monkeypatch):
    uid = 950003
    temp_db.db_ensure(uid, "u", "en"); temp_db.db_set(uid, "is_registered", 1)
    _reset(temp_db); temp_db.db_set_promo_campaign("C", 500)
    sent = await _run(temp_db, uid, "left", monkeypatch)
    assert sent[0][0] == tr(uid, "promo_subscribe")
    assert sent[0][1] is not None                    # subscribe keyboard attached


async def test_gate_issues_code_when_subscribed(temp_db, monkeypatch):
    uid = 950004
    temp_db.db_ensure(uid, "u", "en"); temp_db.db_set(uid, "is_registered", 1)
    _reset(temp_db); temp_db.db_set_promo_campaign("WELCOME10", 500)
    sent = await _run(temp_db, uid, "member", monkeypatch)
    assert sent[0][0] == tr(uid, "promo_code", code="WELCOME10")


async def test_gate_reports_cap_reached(temp_db, monkeypatch):
    uid = 950005
    temp_db.db_ensure(uid, "u", "en"); temp_db.db_set(uid, "is_registered", 1)
    _reset(temp_db); temp_db.db_set_promo_campaign("C", 1)
    temp_db.db_claim_promo(999999)                   # someone else took the only use
    sent = await _run(temp_db, uid, "administrator", monkeypatch)
    assert sent[0][0] == tr(uid, "promo_empty")


async def test_gate_unavailable_when_bot_cannot_check(temp_db, monkeypatch):
    uid = 950006
    temp_db.db_ensure(uid, "u", "en"); temp_db.db_set(uid, "is_registered", 1)
    _reset(temp_db); temp_db.db_set_promo_campaign("C", 500)
    sent = await _run(temp_db, uid, "error", monkeypatch)   # get_chat_member raises
    assert sent[0][0] == tr(uid, "promo_unavailable")
