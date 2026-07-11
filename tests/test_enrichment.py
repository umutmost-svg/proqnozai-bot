"""Offline tests for verified API-Football enrichment (no real network).

All HTTP is served by an httpx.MockTransport; no test hits a real API. The
enrichment key is monkeypatched on so the code path runs while the injected
client controls every response.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import enrichment
from enrichment import (
    EnrichmentBlock, TTLCache, enrich_football_match,
)
from match_validation import Confidence

KO = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


# ─── Fixture-JSON builders ────────────────────────────────────────────────────
def _fixture(fid=101, home="Arsenal", hid=33, away="Chelsea", aid=49,
             league="Premier League", lid=39, date=KO, status="NS",
             gh=None, ga=None):
    return {
        "fixture": {"id": fid, "date": date.isoformat(), "status": {"short": status}},
        "league": {"id": lid, "name": league},
        "teams": {"home": {"id": hid, "name": home}, "away": {"id": aid, "name": away}},
        "goals": {"home": gh, "away": ga},
    }


def _finished_run(team, tid, results):
    """results: list of (opp, scored, conceded). Builds finished fixtures."""
    out = []
    for i, (opp, s, c) in enumerate(results):
        out.append(_fixture(fid=900 + i, home=team, hid=tid, away=opp, aid=1000 + i,
                            status="FT", gh=s, ga=c,
                            date=KO - timedelta(days=7 * (i + 1))))
    return out


def _resp(items):
    return {"response": items}


# ─── Mock client ──────────────────────────────────────────────────────────────
def make_client(router):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        status, body = router(path, params)
        return httpx.Response(status, json=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             headers={"x-apisports-key": "test"})


def default_router(*, candidate=None, home_search_id=33,
                   recent=None, h2h=None, standings=None,
                   injuries=None, lineups=None, stats=None,
                   fail_paths=()):
    """A configurable router. Missing sections default to empty responses."""
    candidate = candidate if candidate is not None else [_fixture()]
    recent = recent or {}

    def router(path, params):
        if path in fail_paths:
            return 500, {"response": []}
        if path == "/teams":
            if home_search_id is None:
                return 200, _resp([])
            name = params.get("search", "")
            return 200, _resp([{"team": {"id": home_search_id, "name": name}}])
        if path == "/fixtures":
            if "date" in params or "live" in params:
                return 200, _resp(candidate)
            if "season" in params:
                tid = params.get("team")
                return 200, _resp(recent.get(str(tid), []))
            return 200, _resp([])
        if path == "/fixtures/headtohead":
            return 200, _resp(h2h or [])
        if path == "/standings":
            return 200, _resp(standings or [])
        if path == "/injuries":
            return 200, _resp(injuries or [])
        if path == "/fixtures/lineups":
            return 200, _resp(lineups or [])
        if path == "/teams/statistics":
            return 200, {"response": stats or {}}
        return 404, {"response": []}

    return router


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(enrichment, "APIFOOTBALL_KEY", "test-key")
    enrichment._cache.clear()
    yield
    enrichment._cache.clear()


def _run(coro):
    return asyncio.run(coro)


def _enrich(client, **kw):
    base = dict(line_id="MB1", home="Arsenal", away="Chelsea", kickoff=KO,
                league="Premier League", is_live=False, client=client, now=NOW)
    base.update(kw)
    return _run(enrich_football_match(**base))


# ─── Identity & confidence ────────────────────────────────────────────────────
def test_exact_match_is_verified_high():
    r = _enrich(make_client(default_router()))
    assert r.verified
    assert r.match_confidence == Confidence.HIGH.value
    assert r.api_football_fixture_id == 101
    assert r.api_football_home_team_id == 33
    assert r.api_football_away_team_id == 49
    assert r.api_football_league_id == 39
    assert r.mostbet_line_id == "MB1"


def test_wrong_team_is_rejected():
    cand = [_fixture(home="Liverpool", hid=40, away="Everton", aid=45)]
    r = _enrich(make_client(default_router(candidate=cand)))
    assert not r.verified
    assert r.match_confidence == Confidence.LOW.value
    assert r.api_football_fixture_id is None


def test_reserve_team_is_rejected():
    # Senior "Arsenal" must not match "Arsenal B".
    cand = [_fixture(home="Arsenal B", hid=333, away="Chelsea", aid=49)]
    r = _enrich(make_client(default_router(candidate=cand)))
    assert not r.verified
    assert "team_tier_mismatch" in r.match_confidence_reasons


def test_women_team_is_rejected():
    cand = [_fixture(home="Arsenal Women", hid=334, away="Chelsea Women", aid=449)]
    r = _enrich(make_client(default_router(candidate=cand)))
    assert not r.verified


def test_wrong_league_is_rejected():
    cand = [_fixture(league="Championship", lid=40)]
    r = _enrich(make_client(default_router(candidate=cand)),
                league="Premier League")
    assert not r.verified
    assert "league_mismatch" in r.match_confidence_reasons


def test_kickoff_outside_tolerance_is_rejected():
    cand = [_fixture(date=KO + timedelta(hours=6))]
    r = _enrich(make_client(default_router(candidate=cand)))
    assert not r.verified
    assert "kickoff_mismatch" in r.match_confidence_reasons


def test_swapped_home_away_is_not_verified():
    # Swapped order grades MEDIUM → not usable for automatic HIGH-only enrichment.
    cand = [_fixture(home="Chelsea", hid=49, away="Arsenal", aid=33)]
    r = _enrich(make_client(default_router(candidate=cand)))
    assert not r.verified
    assert r.match_confidence == Confidence.MEDIUM.value


def test_live_prematch_mismatch_is_rejected():
    # Requested prematch, candidate live → contradiction.
    cand = [_fixture(status="1H")]
    r = _enrich(make_client(default_router(candidate=cand)), is_live=False)
    assert not r.verified
    assert "live_prematch_mismatch" in r.match_confidence_reasons


def test_high_only_enrichment_medium_yields_no_blocks():
    cand = [_fixture(home="Chelsea", hid=49, away="Arsenal", aid=33)]  # swapped→MEDIUM
    r = _enrich(make_client(default_router(candidate=cand)))
    assert r.blocks == {}
    assert r.prompt_text() == ""


# ─── Blocks ───────────────────────────────────────────────────────────────────
def _full_router(**over):
    recent = {
        "33": _finished_run("Arsenal", 33, [("A", 2, 0), ("B", 1, 1), ("C", 3, 2)]),
        "49": _finished_run("Chelsea", 49, [("D", 0, 0), ("E", 1, 2)]),
    }
    h2h = _finished_run("Arsenal", 33, [("Chelsea", 2, 1)])
    standings = [{"league": {"standings": [[
        {"rank": 1, "team": {"name": "Arsenal"}, "points": 80, "goalsDiff": 40},
        {"rank": 2, "team": {"name": "Chelsea"}, "points": 70, "goalsDiff": 30},
    ]]}}]
    injuries = [{"player": {"name": "Saka", "reason": "Knee"},
                 "team": {"name": "Arsenal"}}]
    lineups = [{"team": {"name": "Arsenal"}, "formation": "4-3-3",
                "startXI": [{"player": {"name": "Raya"}}]}]
    stats = {"form": "WWDLW", "fixtures": {"played": {"total": 30},
             "wins": {"total": 20}, "draws": {"total": 5}, "loses": {"total": 5}}}
    kw = dict(recent=recent, h2h=h2h, standings=standings,
              injuries=injuries, lineups=lineups, stats=stats)
    kw.update(over)
    return default_router(**kw)


def test_full_enrichment_blocks_available():
    r = _enrich(make_client(_full_router()))
    assert r.verified
    assert r.blocks["recent_home"].available
    assert r.blocks["h2h"].available
    assert r.blocks["standings"].available
    assert r.blocks["injuries"].available
    assert r.blocks["lineups"].available


def test_partial_enrichment_some_blocks_unavailable():
    # No standings feed → standings unavailable, others still fine.
    r = _enrich(make_client(_full_router(standings=[])))
    assert r.verified
    assert r.blocks["recent_home"].available
    assert not r.blocks["standings"].available
    assert "standings" in r.blocks["standings"].missing


def test_one_failing_block_does_not_break_payload():
    r = _enrich(make_client(_full_router(fail_paths=("/standings",))))
    assert r.verified
    assert not r.blocks["standings"].available
    assert r.blocks["recent_home"].available  # unaffected


def test_empty_injuries_is_unavailable_not_clean():
    r = _enrich(make_client(_full_router(injuries=[])))
    inj = r.blocks["injuries"]
    assert not inj.available
    assert "injuries" in inj.missing
    assert "clean bill of health" in inj.data.lower()


def test_lineup_status_is_unknown_never_confirmed():
    r = _enrich(make_client(_full_router()))
    lu = r.blocks["lineups"]
    assert lu.available
    assert "unknown" in lu.data.lower()
    assert "confirmed" not in lu.data.lower().replace("confirmed/predicted", "")
    assert "lineup_confirmation_status" in lu.missing


def test_metrics_from_completed_matches_only():
    # Include a non-finished fixture in the season feed; it must be excluded.
    recent = {"33": _finished_run("Arsenal", 33, [("A", 2, 0), ("B", 1, 1)])
              + [_fixture(fid=555, home="Arsenal", hid=33, status="NS", gh=None, ga=None)]}
    r = _enrich(make_client(_full_router(recent=recent)))
    block = r.blocks["recent_home"]
    assert block.available
    # 2 finished matches only → "last 2"
    assert "last 2" in block.data


def test_missing_goals_stay_none_not_zero():
    from metrics import compute_team_metrics
    m = compute_team_metrics([{"scored": None, "conceded": None}])
    assert m["matches"] == 0
    assert m["avg_scored"] is None


# ─── Prompt text / no raw payload ─────────────────────────────────────────────
def test_prompt_text_has_no_raw_json():
    r = _enrich(make_client(_full_router()))
    text = r.prompt_text()
    assert text
    # No provider JSON structure leaks into the prompt.
    assert '"fixture"' not in text
    assert '"response"' not in text
    assert "startXI" not in text
    # Well-formed provenance headers present.
    assert "[SOURCE: api-football" in text


def test_no_factual_fallback_when_unverified():
    cand = [_fixture(home="Liverpool", hid=40, away="Everton", aid=45)]
    r = _enrich(make_client(default_router(candidate=cand)))
    assert r.prompt_text() == ""


def test_no_key_returns_unverified(monkeypatch):
    monkeypatch.setattr(enrichment, "APIFOOTBALL_KEY", "")
    r = _run(enrich_football_match(line_id="MB1", home="Arsenal", away="Chelsea",
                                   kickoff=KO, league="Premier League", now=NOW))
    assert not r.verified
    assert r.prompt_text() == ""


# ─── Cache ────────────────────────────────────────────────────────────────────
def test_cache_hit_avoids_second_fetch():
    calls = {"n": 0}

    def counting_router(path, params):
        if path == "/teams":
            calls["n"] += 1
        base = _full_router()
        return base(path, params)

    client = make_client(counting_router)
    _enrich(client)
    first = calls["n"]
    _enrich(client)  # same line_id → fixture served from cache, no team re-resolve
    assert calls["n"] == first  # no additional /teams calls on the cached run


def test_cache_expiry_is_a_miss():
    c = TTLCache()
    c.set("k", "v", ttl=100)
    assert c.get("k") == "v"
    c._store["k"] = (0.0, "v")  # force expiry
    assert c.get("k") is None


def test_cache_bounded_eviction():
    c = TTLCache(maxsize=3)
    for i in range(5):
        c.set(f"k{i}", i, ttl=1000)
    assert c.get("k0") is None  # evicted (LRU)
    assert c.get("k4") == 4


def test_block_to_text_never_none_json():
    b = EnrichmentBlock("x", available=True, data="hello")
    assert "hello" in b.to_text()
    assert "{" not in b.to_text()
