"""Channel-gated promo: one code PER PARTNER, each with its own use cap. A
partner running out hides only that partner's code. Offline — get_chat_member
is faked. Campaign rows are global, so each test resets them first."""
import types

import handlers.promo as promo
from translations import tr


def _reset(temp_db):
    with temp_db.con() as c:
        c.execute("DELETE FROM promo_campaign")
        c.execute("DELETE FROM promo_claims")


# ─── Campaign + capped claims ─────────────────────────────────────────────────

def _codes(granted):
    return {g["partner"]: g["code"] for g in granted}


def test_nothing_configured_by_default(temp_db):
    _reset(temp_db)
    assert temp_db.db_list_promo_codes() == []
    assert temp_db.db_claim_promos(1) == []


def test_user_gets_one_code_per_partner(temp_db):
    _reset(temp_db)
    temp_db.db_set_promo_code("Mostbet", "MB100", 5)
    temp_db.db_set_promo_code("Topaz", "TZ50", 5)
    assert _codes(temp_db.db_claim_promos(101)) == {"Mostbet": "MB100", "Topaz": "TZ50"}


def test_each_partner_has_its_own_cap(temp_db):
    """Mostbet running out must not hide Topaz."""
    _reset(temp_db)
    temp_db.db_set_promo_code("Mostbet", "MB", 1)
    temp_db.db_set_promo_code("Topaz", "TZ", 5)
    temp_db.db_claim_promos(110)                       # Mostbet's only use spent
    assert _codes(temp_db.db_claim_promos(111)) == {"Topaz": "TZ"}


def test_claim_is_idempotent_per_user(temp_db):
    _reset(temp_db)
    temp_db.db_set_promo_code("Mostbet", "CODE", 1)
    first = _codes(temp_db.db_claim_promos(200))
    assert _codes(temp_db.db_claim_promos(200)) == first     # same, no extra use
    assert temp_db.db_list_promo_codes()[0]["claimed"] == 1


def test_replacing_one_partner_code_resets_only_its_count(temp_db):
    _reset(temp_db)
    temp_db.db_set_promo_code("Mostbet", "OLD", 1)
    temp_db.db_set_promo_code("Topaz", "TZ", 1)
    temp_db.db_claim_promos(300)                       # both spent
    assert temp_db.db_claim_promos(301) == []
    temp_db.db_set_promo_code("Mostbet", "NEW", 1)     # fresh code for one partner
    assert _codes(temp_db.db_claim_promos(301)) == {"Mostbet": "NEW"}


def test_deleting_a_partner_code(temp_db):
    _reset(temp_db)
    temp_db.db_set_promo_code("Mostbet", "MB", 5)
    assert temp_db.db_delete_promo_code("Mostbet") is True
    assert temp_db.db_list_promo_codes() == []
    assert temp_db.db_delete_promo_code("Mostbet") is False


def test_promo_stats_per_partner(temp_db):
    _reset(temp_db)
    temp_db.db_set_promo_code("Mostbet", "S", 500)
    temp_db.db_claim_promos(1); temp_db.db_claim_promos(2)
    assert temp_db.db_list_promo_codes() == [
        {"partner": "Mostbet", "code": "S", "max_uses": 500,
         "claimed": 2, "available": 498, "is_active": True, "is_archived": False}]


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

    async def reply(text, reply_markup=None, **kw):
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
    _reset(temp_db); temp_db.db_set_promo_code("Mostbet", "C", 500)
    sent = await _run(temp_db, uid, "left", monkeypatch)
    # What is on offer is stated before the subscription is demanded.
    assert "Mostbet" in sent[0][0]
    assert tr(uid, "promo_subscribe") in sent[0][0]
    assert sent[0][1] is not None                    # subscribe keyboard attached


async def test_gate_issues_code_when_subscribed(temp_db, monkeypatch):
    uid = 950004
    temp_db.db_ensure(uid, "u", "en"); temp_db.db_set(uid, "is_registered", 1)
    _reset(temp_db); temp_db.db_set_promo_code("Mostbet", "WELCOME10", 500)
    sent = await _run(temp_db, uid, "member", monkeypatch)
    assert tr(uid, "promo_codes_title") in sent[0][0]
    assert "Mostbet" in sent[0][0] and "WELCOME10" in sent[0][0]


async def test_gate_reports_cap_reached(temp_db, monkeypatch):
    uid = 950005
    temp_db.db_ensure(uid, "u", "en"); temp_db.db_set(uid, "is_registered", 1)
    _reset(temp_db); temp_db.db_set_promo_code("Mostbet", "C", 1)
    temp_db.db_claim_promos(999999)                  # someone else took the only use
    sent = await _run(temp_db, uid, "administrator", monkeypatch)
    assert sent[0][0] == tr(uid, "promo_empty")


async def test_gate_unavailable_when_bot_cannot_check(temp_db, monkeypatch):
    uid = 950006
    temp_db.db_ensure(uid, "u", "en"); temp_db.db_set(uid, "is_registered", 1)
    _reset(temp_db); temp_db.db_set_promo_code("Mostbet", "C", 500)
    sent = await _run(temp_db, uid, "error", monkeypatch)   # get_chat_member raises
    # Its own message, not the same one as "no campaign configured".
    assert sent[0][0] == tr(uid, "promo_check_failed")
    assert sent[0][0] != tr(uid, "promo_unavailable")


# ─── admin command reports why a code was refused ─────────────────────────────

async def test_setpromo_explains_a_duplicate_code(temp_db, monkeypatch):
    """The duplicate-code guard raises; unhandled it surfaced as a generic
    "error" with no hint about what to do."""
    import types
    _reset(temp_db)
    temp_db.db_set_promo_code("First", "SHARED", 5)
    monkeypatch.setattr(promo, "ADMIN_ID", 1)

    sent = []

    class _M:
        text = "/setpromo Second SHARED 5"
        async def reply_text(self, t, **kw):
            sent.append(t)

    upd = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=1), message=_M())
    await promo.setpromo_cmd(upd, types.SimpleNamespace())

    assert sent and "SHARED" in sent[0] and "First" in sent[0]
    assert len(temp_db.db_list_promo_codes()) == 1      # nothing was overwritten


async def test_setpromo_accepts_a_distinct_code(temp_db, monkeypatch):
    import types
    _reset(temp_db)
    monkeypatch.setattr(promo, "ADMIN_ID", 1)
    sent = []

    class _M:
        text = "/setpromo Mostbet MB-1 100"
        async def reply_text(self, t, **kw):
            sent.append(t)

    upd = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=1), message=_M())
    await promo.setpromo_cmd(upd, types.SimpleNamespace())
    assert sent[0].startswith("✅")
    assert temp_db.db_list_promo_codes()[0]["code"] == "MB-1"
