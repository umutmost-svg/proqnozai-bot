"""Offline tests for wiring verified enrichment into the football menu flow.

No network: enrich_football_match, Claude, Mostbet odds and Telegram are all
stubbed. These guard the HIGH-only gate, the honest-note behaviour and that
Mostbet odds survive regardless of enrichment outcome.
"""
import types
from datetime import datetime, timezone

import handlers.forecast as fc
from enrichment import EnrichmentBlock, EnrichmentResult
from event_list import EventItem

KO = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)


def _event(sport="Football", is_live=False):
    return EventItem(
        fixture_id="MB1", provider="mostbet", home="Arsenal", away="Chelsea",
        league_name="Premier League", country="England", kickoff_utc=KO,
        is_live=is_live, status=None, sport=sport)


class _FakeMsg:
    def __init__(self):
        self.text = None
        self.reply_markup = None

    async def edit_text(self, text, **kw):
        self.text = text
        self.reply_markup = kw.get("reply_markup")


class _FakeBot:
    def __init__(self):
        self.status_msg = _FakeMsg()

    async def send_message(self, chat_id, text, **kw):
        self.status_msg.text = text
        return self.status_msg

    async def send_chat_action(self, chat_id, action):
        pass


class _FakeQuery:
    def __init__(self, uid, data):
        self.from_user = types.SimpleNamespace(id=uid)
        self.data = data
        self.message = _FakeMsg()

    async def answer(self):
        pass

    async def edit_message_text(self, text, **kw):
        self.message.text = text


def _ctx(bot, **user_data):
    return types.SimpleNamespace(user_data=dict(user_data), bot=bot)


async def _stub_common(monkeypatch):
    async def _fake_forecast(uid, content, sys_prompt, max_tok):
        return "FORECAST"

    async def _fake_get_odds(line_id):
        return {"w1": "1.5"}

    monkeypatch.setattr(fc, "claude_forecast", _fake_forecast)
    monkeypatch.setattr(fc, "mostbet_get_odds", _fake_get_odds)
    monkeypatch.setattr(fc, "format_mostbet_odds", lambda odds, lang: "ODDS")
    monkeypatch.setattr(fc, "APIFOOTBALL_KEY", "test-key")


def _verified_result():
    r = EnrichmentResult(mostbet_line_id="MB1", api_football_fixture_id=101,
                         api_football_home_team_id=33, api_football_away_team_id=49,
                         api_football_league_id=39, match_confidence="high")
    r.blocks["recent_home"] = EnrichmentBlock("recent_home", available=True,
                                              data="Arsenal last 5: ...")
    r.missing_fields = ["standings"]  # verified fixture, one block unavailable
    return r


async def test_football_high_enrichment_attaches_verified_data(monkeypatch, temp_db):
    uid = 910001
    temp_db.db_ensure(uid, "u", "en")
    await _stub_common(monkeypatch)

    async def _fake_enrich(**kw):
        assert kw["line_id"] == "MB1"  # Mostbet identity forwarded, not fabricated
        return _verified_result()
    monkeypatch.setattr(fc, "enrich_football_match", _fake_enrich)

    bot = _FakeBot()
    ctx = _ctx(bot, fm_matches=[_event()])
    q = _FakeQuery(uid, "fm_mt_0")
    await fc.fm_match_cb(types.SimpleNamespace(callback_query=q), ctx)

    # Verified real data attached; Mostbet odds still present.
    texts = [c["text"] for c in ctx.user_data["pending_content"]]
    assert any("VERIFIED FOOTBALL DATA" in t for t in texts)
    assert "ODDS" in texts
    assert ctx.user_data["has_real_data"] is True
    # Honest note for the one unavailable verified block appears in the reply.
    assert bot.status_msg.text.endswith(fc.tr(uid, "enr_standings_unavailable"))


async def test_football_unverified_keeps_odds_no_fallback(monkeypatch, temp_db):
    uid = 910002
    temp_db.db_ensure(uid, "u", "en")
    await _stub_common(monkeypatch)

    async def _fake_enrich(**kw):
        return EnrichmentResult(mostbet_line_id="MB1", match_confidence="low")
    monkeypatch.setattr(fc, "enrich_football_match", _fake_enrich)

    bot = _FakeBot()
    ctx = _ctx(bot, fm_matches=[_event()])
    q = _FakeQuery(uid, "fm_mt_0")
    await fc.fm_match_cb(types.SimpleNamespace(callback_query=q), ctx)

    texts = [c["text"] for c in ctx.user_data["pending_content"]]
    assert not any("VERIFIED FOOTBALL DATA" in t for t in texts)
    assert "ODDS" in texts  # odds preserved
    assert ctx.user_data["has_real_data"] is False  # no factual fallback
    assert bot.status_msg.text.endswith(fc.tr(uid, "enr_football_unavailable"))


async def test_enrichment_failure_does_not_break_forecast(monkeypatch, temp_db):
    uid = 910003
    temp_db.db_ensure(uid, "u", "en")
    await _stub_common(monkeypatch)

    async def _boom(**kw):
        raise RuntimeError("provider down")
    monkeypatch.setattr(fc, "enrich_football_match", _boom)

    bot = _FakeBot()
    ctx = _ctx(bot, fm_matches=[_event()])
    q = _FakeQuery(uid, "fm_mt_0")
    await fc.fm_match_cb(types.SimpleNamespace(callback_query=q), ctx)

    # Forecast still produced; odds preserved; honest note shown.
    assert "FORECAST" in bot.status_msg.text
    texts = [c["text"] for c in ctx.user_data["pending_content"]]
    assert "ODDS" in texts
    assert ctx.user_data["has_real_data"] is False


async def test_non_football_skips_enrichment(monkeypatch, temp_db):
    uid = 910004
    temp_db.db_ensure(uid, "u", "en")
    await _stub_common(monkeypatch)

    called = {"enrich": 0}

    async def _fake_enrich(**kw):
        called["enrich"] += 1
        return _verified_result()

    async def _fake_real_data(t1, t2, hint):
        return "LEGACY-DATA"
    monkeypatch.setattr(fc, "enrich_football_match", _fake_enrich)
    monkeypatch.setattr(fc, "fetch_real_data", _fake_real_data)

    bot = _FakeBot()
    ctx = _ctx(bot, fm_matches=[_event(sport="Basketball")])
    q = _FakeQuery(uid, "fm_mt_0")
    await fc.fm_match_cb(types.SimpleNamespace(callback_query=q), ctx)

    assert called["enrich"] == 0  # enrichment is football-only
    texts = [c["text"] for c in ctx.user_data["pending_content"]]
    assert "LEGACY-DATA" in texts


def test_enrichment_gap_note_maps_blocks(temp_db):
    uid = 910005
    temp_db.db_ensure(uid, "u", "en")
    note = fc._enrichment_gap_note(uid, ["recent_home", "standings", "lineups"])
    assert fc.tr(uid, "enr_standings_unavailable") in note
    assert fc.tr(uid, "enr_lineups_unavailable") in note
    # recent_home is not a user-facing gap key.
    assert "recent_home" not in note


def test_enrichment_gap_note_none_when_no_user_blocks(temp_db):
    uid = 910006
    temp_db.db_ensure(uid, "u", "en")
    assert fc._enrichment_gap_note(uid, ["recent_home", "stats_away"]) is None
