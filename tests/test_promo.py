"""Channel-gated promo codes: pool management + the registration/subscription
gate. Offline — get_chat_member is faked. promo_codes is a shared global pool,
so each test resets it first."""
import types

import handlers.promo as promo
from translations import tr


def _reset(temp_db):
    with temp_db.con() as c:
        c.execute("DELETE FROM promo_codes")


# ─── Pool management ──────────────────────────────────────────────────────────

def test_add_promo_dedup_and_count(temp_db):
    _reset(temp_db)
    assert temp_db.db_add_promo_codes(["A1", "A2", "A1", "", "  "]) == 2
    assert temp_db.db_add_promo_codes(["A2", "A3"]) == 1        # A2 already there
    assert temp_db.db_promo_stats() == {"total": 3, "claimed": 0, "available": 3}


def test_claim_one_per_user_idempotent(temp_db):
    _reset(temp_db)
    temp_db.db_add_promo_codes(["C1", "C2"])
    a = temp_db.db_claim_promo(111)
    assert a in ("C1", "C2")
    assert temp_db.db_claim_promo(111) == a          # repeat → same code
    b = temp_db.db_claim_promo(222)
    assert b in ("C1", "C2") and b != a              # other user → other code


def test_claim_returns_none_when_pool_empty(temp_db):
    _reset(temp_db)
    temp_db.db_add_promo_codes(["ONLY"])
    assert temp_db.db_claim_promo(1) == "ONLY"
    assert temp_db.db_claim_promo(2) is None         # exhausted


def test_promo_stats_counts_claimed(temp_db):
    _reset(temp_db)
    temp_db.db_add_promo_codes(["S1", "S2", "S3"])
    temp_db.db_claim_promo(1)
    assert temp_db.db_promo_stats() == {"total": 3, "claimed": 1, "available": 2}


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


async def test_gate_prompts_subscription_when_not_member(temp_db, monkeypatch):
    uid = 950002
    temp_db.db_ensure(uid, "u", "en"); temp_db.db_set(uid, "is_registered", 1)
    sent = await _run(temp_db, uid, "left", monkeypatch)
    assert sent[0][0] == tr(uid, "promo_subscribe")
    assert sent[0][1] is not None                    # subscribe keyboard attached


async def test_gate_issues_code_when_subscribed(temp_db, monkeypatch):
    uid = 950003
    temp_db.db_ensure(uid, "u", "en"); temp_db.db_set(uid, "is_registered", 1)
    _reset(temp_db); temp_db.db_add_promo_codes(["WELCOME10"])
    sent = await _run(temp_db, uid, "member", monkeypatch)
    assert sent[0][0] == tr(uid, "promo_code", code="WELCOME10")


async def test_gate_reports_empty_pool(temp_db, monkeypatch):
    uid = 950004
    temp_db.db_ensure(uid, "u", "en"); temp_db.db_set(uid, "is_registered", 1)
    _reset(temp_db)                                   # no codes
    sent = await _run(temp_db, uid, "administrator", monkeypatch)
    assert sent[0][0] == tr(uid, "promo_empty")


async def test_gate_unavailable_when_channel_not_set(temp_db, monkeypatch):
    uid = 950005
    temp_db.db_ensure(uid, "u", "en"); temp_db.db_set(uid, "is_registered", 1)
    sent = await _run(temp_db, uid, "member", monkeypatch, channel="")
    assert sent[0][0] == tr(uid, "promo_unavailable")


async def test_gate_unavailable_when_bot_cannot_check(temp_db, monkeypatch):
    uid = 950006
    temp_db.db_ensure(uid, "u", "en"); temp_db.db_set(uid, "is_registered", 1)
    sent = await _run(temp_db, uid, "error", monkeypatch)   # get_chat_member raises
    assert sent[0][0] == tr(uid, "promo_unavailable")
