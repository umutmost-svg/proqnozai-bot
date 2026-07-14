"""mostbet_get_odds market parsing (mocked HTTP, local fixture) and
format_mostbet_odds prompt formatting."""
import json
import os

import pytest

import mostbet
from mostbet import format_mostbet_odds, mostbet_get_odds

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _load_fixture(name: str) -> dict:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.headers: dict = {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient; serves the fixture for any GET."""

    def __init__(self, payload: dict):
        self._payload = payload
        self.requests: list = []

    def __call__(self, *args, **kwargs):  # httpx.AsyncClient(...) call
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return _FakeResponse(self._payload)


@pytest.fixture()
def parsed_odds(monkeypatch, clean_mostbet_cache):
    fake = _FakeAsyncClient(_load_fixture("outcomes_full.json"))
    monkeypatch.setattr(mostbet.httpx, "AsyncClient", fake)

    async def run():
        return await mostbet_get_odds(777001)

    import asyncio
    return asyncio.run(run())


def test_full_time_1x2(parsed_odds):
    assert parsed_odds["w1"] == 2.10
    assert parsed_odds["x"] == 3.40
    assert parsed_odds["w2"] == 3.60


def test_double_chance(parsed_odds):
    assert parsed_odds["dc_1x"] == 1.30
    assert parsed_odds["dc_12"] == 1.32
    assert parsed_odds["dc_x2"] == 1.75


def test_draw_no_bet(parsed_odds):
    assert parsed_odds["dnb_w1"] == 1.55
    assert parsed_odds["dnb_w2"] == 2.45


def test_handicap_picks_most_balanced_line_with_both_sides(parsed_odds):
    # -1.5/+1.5 has both sides; the lone -2.5 must not win.
    assert parsed_odds["hcp_val"] == -1.5
    assert parsed_odds["hcp_w1"] == 3.10
    assert parsed_odds["hcp_w2"] == 1.38


def test_totals_full_time(parsed_odds):
    assert parsed_odds["over15"] == 1.28
    assert parsed_odds["under15"] == 3.70
    assert parsed_odds["over25"] == 1.85
    assert parsed_odds["under25"] == 1.95
    assert parsed_odds["over35"] == 3.20
    assert parsed_odds["under35"] == 1.33


def test_total_105_does_not_pollute_05_or_15_lines(parsed_odds):
    # "over 10.5" must not be misread as the 0.5 or 1.5 line.
    assert parsed_odds["over15"] == 1.28
    assert parsed_odds["h1_over05"] == 1.40  # untouched by the 10.5 outcome


def test_btts(parsed_odds):
    assert parsed_odds["btts_yes"] == 1.72
    assert parsed_odds["btts_no"] == 2.05


def test_first_half_markets_not_swallowed_by_full_time(parsed_odds):
    assert parsed_odds["h1_w1"] == 2.90
    assert parsed_odds["h1_x"] == 2.10
    assert parsed_odds["h1_w2"] == 4.20
    assert parsed_odds["h1_over05"] == 1.40
    assert parsed_odds["h1_under05"] == 2.80
    assert parsed_odds["h1_over15"] == 2.60
    assert parsed_odds["h1_under15"] == 1.48


def test_unrelated_market_groups_ignored(parsed_odds):
    # "Corners over 9.5" must not leak into any goal-total field.
    values = [v for v in parsed_odds.values() if v is not None]
    assert 1.90 not in values


def test_result_is_cached(parsed_odds, clean_mostbet_cache):
    assert "odds_777001" in clean_mostbet_cache


# ── format_mostbet_odds ───────────────────────────────────────────────────────

_FULL = {
    "w1": 2.1, "x": 3.4, "w2": 3.6,
    "dc_1x": 1.3, "dc_12": 1.32, "dc_x2": 1.75,
    "hcp_w1": 3.1, "hcp_w2": 1.38, "hcp_val": -1.5,
    "over15": 1.28, "under15": 3.7,
    "over25": 1.85, "under25": 1.95,
    "over35": 3.2, "under35": 1.33,
    "btts_yes": 1.72, "btts_no": 2.05,
    "h1_w1": 2.9, "h1_x": 2.1, "h1_w2": 4.2,
    "h1_over05": 1.4, "h1_under05": 2.8,
    "h1_over15": 2.6, "h1_under15": 1.48,
    "dnb_w1": 1.55, "dnb_w2": 2.45,
}


def test_format_empty_odds_returns_empty_string():
    assert format_mostbet_odds({k: None for k in _FULL}, "ru") == ""


def test_format_ru_contains_all_markets():
    out = format_mostbet_odds(_FULL, "ru")
    assert "РЕАЛЬНЫЕ КОЭФФИЦИЕНТЫ MOSTBET" in out
    for frag in ("1X2", "Двойной шанс", "Фора", "Тотал 2.5", "Обе забьют", "1-й тайм"):
        assert frag in out, f"missing {frag!r}"


def test_format_lang_fallbacks():
    # kz/uz/tr fall back to Russian, ar falls back to English.
    assert "РЕАЛЬНЫЕ КОЭФФИЦИЕНТЫ" in format_mostbet_odds(_FULL, "kz")
    assert "REAL MOSTBET ODDS" in format_mostbet_odds(_FULL, "ar")


def test_format_partial_lines_are_omitted():
    odds = dict.fromkeys(_FULL, None)
    odds.update({"w1": 2.0, "x": None, "w2": 3.0})  # incomplete 1X2 triple
    odds["over25"] = 1.9
    odds["under25"] = 1.9
    out = format_mostbet_odds(odds, "en")
    assert "1X2" not in out
    assert "Total 2.5" in out


# ── Side markets must not overwrite the main line ────────────────────────────

_POISON_PAYLOAD = {
    "lineMatchOutcomes": [
        # Real main markets first…
        {"groupTitle": "Total goals", "outcomeTitle": "over 2.5", "odd": "1.85"},
        {"groupTitle": "Total goals", "outcomeTitle": "under 2.5", "odd": "1.95"},
        {"groupTitle": "Both teams to score", "outcomeTitle": "yes", "odd": "1.72"},
        {"groupTitle": "Double chance", "outcomeTitle": "1x", "odd": "1.30"},
        {"groupTitle": "Match result", "outcomeTitle": "1", "odd": "2.10"},
        # …then side markets sharing the same keywords, arriving LATER so they
        # would overwrite the real values without the group exclusion.
        {"groupTitle": "Total corners", "outcomeTitle": "over 2.5", "odd": "9.99"},
        {"groupTitle": "Total cards", "outcomeTitle": "under 2.5", "odd": "8.88"},
        {"groupTitle": "Individual total 1", "outcomeTitle": "over 2.5", "odd": "7.77"},
        {"groupTitle": "Player shots total", "outcomeTitle": "over 2.5", "odd": "6.66"},
        # Half-time variants of DC/BTTS must not pollute full-time fields.
        {"groupTitle": "1st half double chance", "outcomeTitle": "1x", "odd": "5.55"},
        {"groupTitle": "1st half both teams to score", "outcomeTitle": "yes", "odd": "4.44"},
    ]
}


@pytest.fixture()
def poisoned_odds(monkeypatch, clean_mostbet_cache):
    fake = _FakeAsyncClient(_POISON_PAYLOAD)
    monkeypatch.setattr(mostbet.httpx, "AsyncClient", fake)

    async def run():
        return await mostbet_get_odds(777002)

    import asyncio
    return asyncio.run(run())


def test_side_totals_do_not_overwrite_goal_totals(poisoned_odds):
    assert poisoned_odds["over25"] == 1.85       # goals, not corners 9.99
    assert poisoned_odds["under25"] == 1.95      # goals, not cards 8.88


def test_side_market_values_leak_nowhere(poisoned_odds):
    values = [v for v in poisoned_odds.values() if v is not None]
    for poison in (9.99, 8.88, 7.77, 6.66, 5.55, 4.44):
        assert poison not in values, f"side-market value {poison} leaked"


def test_half_time_dc_and_btts_do_not_pollute_full_time(poisoned_odds):
    assert poisoned_odds["dc_1x"] == 1.30        # not 5.55 from 1st-half DC
    assert poisoned_odds["btts_yes"] == 1.72     # not 4.44 from 1st-half BTTS


# ── Odds cache freshness ──────────────────────────────────────────────────────

class _ErrorAsyncClient:
    """Every GET raises — simulates Mostbet being unreachable."""

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        raise mostbet.httpx.ConnectError("down")


def test_failed_fetch_not_pinned_for_full_ttl(monkeypatch, clean_mostbet_cache):
    """One network hiccup caches an all-None line only for the short empty TTL;
    after it passes, the next call refetches and gets the real values."""
    import asyncio
    from config import MOSTBET_ODDS_EMPTY_TTL

    monkeypatch.setattr(mostbet.httpx, "AsyncClient", _ErrorAsyncClient())
    empty = asyncio.run(mostbet_get_odds(777003))
    assert all(v is None for v in empty.values())

    # Within the empty TTL the cached failure is served (no refetch)…
    good = _FakeAsyncClient(_load_fixture("outcomes_full.json"))
    monkeypatch.setattr(mostbet.httpx, "AsyncClient", good)
    still_empty = asyncio.run(mostbet_get_odds(777003))
    assert all(v is None for v in still_empty.values())
    assert good.requests == []

    # …but once it expires, the line is refetched — not pinned for 15 minutes.
    ts, data = clean_mostbet_cache["odds_777003"]
    clean_mostbet_cache["odds_777003"] = (ts - MOSTBET_ODDS_EMPTY_TTL - 1, data)
    fresh = asyncio.run(mostbet_get_odds(777003))
    assert fresh["w1"] == 2.10
    assert good.requests


def test_real_odds_refresh_after_odds_ttl_not_list_ttl(monkeypatch, clean_mostbet_cache):
    """Priced lines refresh on MOSTBET_ODDS_TTL (minutes), not the 15-minute
    match-list TTL — the bot's odds must track the site closely."""
    import asyncio
    from config import MOSTBET_ODDS_TTL, MOSTBET_CACHE_TTL

    assert MOSTBET_ODDS_TTL < MOSTBET_CACHE_TTL

    good = _FakeAsyncClient(_load_fixture("outcomes_full.json"))
    monkeypatch.setattr(mostbet.httpx, "AsyncClient", good)
    first = asyncio.run(mostbet_get_odds(777004))
    assert first["w1"] == 2.10
    fetches_after_first = len(good.requests)

    # Age the entry past the odds TTL but well inside the old 15-min window.
    ts, data = clean_mostbet_cache["odds_777004"]
    clean_mostbet_cache["odds_777004"] = (ts - MOSTBET_ODDS_TTL - 1, data)
    asyncio.run(mostbet_get_odds(777004))
    assert len(good.requests) > fetches_after_first   # refetched
