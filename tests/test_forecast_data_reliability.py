"""Offline tests for the data-reliability release: no invented factual data,
duplicate-odds prevention, and validated live-match attachment. No network."""
import types

import football_api
import handlers.forecast as fc


# ─── No invented factual data ─────────────────────────────────────────────────

def test_sonnet_form_estimate_is_removed():
    # The LLM form-invention fallback must no longer exist anywhere.
    assert not hasattr(football_api, "_sonnet_form_estimate")


async def test_fetch_real_data_returns_empty_when_no_provider(monkeypatch):
    # With no provider keys, fetch_real_data must return "" (caller then flags
    # has_real_data=False) rather than fabricating form via the LLM.
    async def _fake_normalize(t1, t2):
        return t1, t2
    monkeypatch.setattr(football_api, "_normalize_names", _fake_normalize)
    monkeypatch.setattr(football_api, "FOOTBALL_KEY", "")
    monkeypatch.setattr(football_api, "APIFOOTBALL_KEY", "")

    result = await football_api.fetch_real_data("Arsenal", "Chelsea", "Premier League")
    assert result == ""


# ─── Validated watch-candidate selection ──────────────────────────────────────

def test_pick_watch_candidate_rejects_wrong_match():
    ref = {"home": "Arsenal", "away": "Chelsea", "is_live": False}
    candidates = [
        {"id": "1", "name": "Liverpool vs Everton", "home": "Liverpool",
         "away": "Everton", "live": False},
    ]
    assert fc._pick_watch_candidate(candidates, ref) is None


def test_pick_watch_candidate_accepts_matching():
    ref = {"home": "Arsenal", "away": "Chelsea", "is_live": True}
    candidates = [
        {"id": "9", "name": "Arsenal vs Chelsea", "home": "Arsenal",
         "away": "Chelsea", "live": True},
    ]
    picked = fc._pick_watch_candidate(candidates, ref)
    assert picked and picked["id"] == "9"


def test_pick_watch_candidate_no_ref_keeps_first():
    candidates = [{"id": "3", "name": "A vs B", "home": "A", "away": "B", "live": False}]
    assert fc._pick_watch_candidate(candidates, None)["id"] == "3"


# ─── Duplicate-odds prevention in the menu flow ───────────────────────────────

class _FakeMsg:
    def __init__(self):
        self.text = None

    async def edit_text(self, text, **kw):
        self.text = text


def _ctx(**user_data):
    return types.SimpleNamespace(user_data=dict(user_data))


async def _stub_common(monkeypatch, calls):
    async def _fake_forecast(uid, content, sys_prompt, max_tok):
        return "FORECAST"

    async def _fake_find(t1, t2):
        calls.append(("find", t1, t2))
        return None

    async def _fake_get_odds(line_id):
        calls.append(("odds", line_id))
        return {}

    monkeypatch.setattr(fc, "claude_forecast", _fake_forecast)
    monkeypatch.setattr(fc, "mostbet_find_match", _fake_find)
    monkeypatch.setattr(fc, "mostbet_get_odds", _fake_get_odds)
    monkeypatch.setattr(fc, "format_mostbet_odds", lambda odds, lang: "ODDS")


async def test_menu_flow_does_not_refetch_odds(monkeypatch, temp_db):
    """When fm_match_cb has already attached odds (odds_attached=True), the
    forecast must NOT run a second fuzzy Mostbet lookup/injection."""
    uid = 900001
    temp_db.db_ensure(uid, "u", "en")
    calls: list = []
    await _stub_common(monkeypatch, calls)

    content = [{"type": "text", "text": "Match: Arsenal vs Chelsea"},
               {"type": "text", "text": "ODDS"}]
    ctx = _ctx(pending_content=content, pending_text="Arsenal Chelsea",
               parsed_teams=("Arsenal", "Chelsea"), odds_attached=True,
               has_real_data=False)

    await fc._generate_forecast(uid, ctx, _FakeMsg())

    assert ("find", "Arsenal", "Chelsea") not in calls
    assert not any(c[0] == "odds" for c in calls)
    # No second odds block was appended.
    assert sum(1 for c in content if c.get("text") == "ODDS") == 1


async def test_non_menu_flow_still_fetches_odds(monkeypatch, temp_db):
    """Without odds_attached, the fuzzy Mostbet lookup still runs (unchanged)."""
    uid = 900002
    temp_db.db_ensure(uid, "u", "en")
    calls: list = []
    await _stub_common(monkeypatch, calls)

    ctx = _ctx(pending_content=[{"type": "text", "text": "x"}],
               pending_text="Arsenal Chelsea",
               parsed_teams=("Arsenal", "Chelsea"), has_real_data=False)

    await fc._generate_forecast(uid, ctx, _FakeMsg())

    assert ("find", "Arsenal", "Chelsea") in calls


async def test_odds_attached_does_not_persist_between_forecasts(monkeypatch, temp_db):
    """A stale odds_attached=True must not survive into the NEXT forecast on the
    same context: it is consumed (popped) on first use, so a later non-menu
    forecast fetches odds normally."""
    uid = 900003
    temp_db.db_ensure(uid, "u", "en")
    calls: list = []
    await _stub_common(monkeypatch, calls)

    ctx = _ctx(pending_content=[{"type": "text", "text": "ODDS"}],
               pending_text="Arsenal Chelsea",
               parsed_teams=("Arsenal", "Chelsea"), odds_attached=True,
               has_real_data=False)

    # First forecast: odds already attached → no re-fetch, flag consumed.
    await fc._generate_forecast(uid, ctx, _FakeMsg())
    assert not any(c[0] == "find" for c in calls)
    assert "odds_attached" not in ctx.user_data

    # Second forecast on the same context: flag is gone → odds fetched again.
    calls.clear()
    await fc._generate_forecast(uid, ctx, _FakeMsg())
    assert ("find", "Arsenal", "Chelsea") in calls
