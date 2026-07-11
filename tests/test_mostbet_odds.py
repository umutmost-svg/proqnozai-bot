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
