"""The whole chain over HTTP, offline: the bot's signed push (live Mostbet spellings) → the Mini App's
odds store → the match page and the AI analysis → what the user receives.

Everything outside is a fake: football-data answers by URL, the goal model per match, the language
model records its prompts. One app, one push route, one AI route — the production wiring of create_app.
The lines are written here in the bot's wire format; the bot's own selection and payload building
(mostbet_feed.OddsExport → FeedReceiver) are covered by tests/test_mostbet_feed.py in the bot's suite.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

import miniapp.app as app_module
from miniapp.ai.analysis import BASIS_PROMPT, SYSTEM_PROMPT
from miniapp.forecast.engine import MatchForecast, Unavailable
from miniapp.odds.feed import TRANSIT_ALLOWANCE_S
from miniapp.odds.store import MAX_AGE_S
from miniapp.sports.client import HttpResponse
from miniapp.tests.conftest import ORIGIN, FakeClock, fresh, login
from miniapp.tests.forecast_fakes import fake_forecast
from miniapp.tests.test_ai_analysis import FakeModel, answer
from miniapp.tests.test_odds import SECRET, body, iso, post, ts
from miniapp.tests.test_sports import DAY, KEY, base_settings, no_sleep

KICKOFF = f"{DAY}T15:00:00Z"
# The bot's export reads these from the feed as they are (mostbet_feed.OddsExport._payload).
PRICES = {"w1": 1.80, "x": 3.80, "w2": 4.50, "dc_1x": 1.22, "dc_x2": 2.00, "dc_12": 1.25,
          "over25": 1.75, "under25": 2.10, "btts_yes": 1.70, "btts_no": 2.15}

# football-data id, code, home (id, name), away (id, name); team ids are real football-data numbers
# where an alias matters (Ajax 678, PSV 674, Benfica 1903, Porto 503).
MATCHES = {
    1: ("PL", (57, "Arsenal FC"), (61, "Chelsea FC")),             # league, goal model, odds
    2: ("CL", (5, "FC Bayern München"), (524, "Paris Saint-Germain FC")),  # CL, no goal model, odds
    3: ("FAC", (66, "Manchester United FC"), (73, "Tottenham Hotspur FC")),  # domestic cup: no odds source
    4: ("QUFA", (760, "Spain"), (773, "Italy")),                   # World Cup qualifier: no odds source
    5: ("UNL", (759, "Germany"), (770, "England")),                # Nations League: no odds source
    6: ("VEI", (9001, "HJK Helsinki"), (9002, "KuPS")),            # small league: thin form, no odds
    7: ("DED", (678, "AFC Ajax"), (674, "PSV")),                   # odds only (thin form, no model)
    8: ("PPL", (1903, "Sport Lisboa e Benfica"), (503, "FC Porto")),  # PT 'Premier League', model
}
THIN = {9001, 9002, 678, 674}  # teams with fewer than 3 finished matches: form not verified
NO_MODEL = {2, 3, 4, 5, 6, 7}


def _team(tid: int, name: str) -> dict:
    return {"id": tid, "name": name, "shortName": name, "tla": name[:3].upper()}


def _match(mid: int, *, status: str = "TIMED", utc: str = KICKOFF) -> dict:
    code, home, away = MATCHES[mid]
    return {"id": mid, "utcDate": utc, "status": status, "lastUpdated": "2027-01-15T07:59:00Z",
            "competition": {"id": 2000 + mid, "code": code, "name": code},
            "homeTeam": _team(*home), "awayTeam": _team(*away),
            "score": {"winner": None, "duration": "REGULAR", "fullTime": {"home": None, "away": None},
                      "halfTime": {"home": None, "away": None}}}


def _form(team: int) -> dict:
    n = 2 if team in THIN else 5
    return {"matches": [{
        "id": team * 100 + i, "status": "FINISHED", "utcDate": f"2027-01-{13 - i:02d}T15:00:00Z",
        "competition": {"code": "PL"}, "homeTeam": {"id": team, "name": "Us"},
        "awayTeam": {"id": 8000 + i, "name": f"Them {i}"}, "score": {"fullTime": {"home": 2, "away": 1}}}
        for i in range(n)]}


class Provider:
    """football-data by URL path: matches, team forms, tables (a cup or a qualifier has none: 404)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.matches = {mid: _match(mid) for mid in MATCHES}

    async def __call__(self, url: str, headers, timeout_s: float) -> HttpResponse:
        path = url.split("/v4", 1)[1].split("?", 1)[0]
        self.calls.append(path)
        if m := re.fullmatch(r"/matches/(\d+)", path):
            return self._ok(self.matches[int(m[1])]) if int(m[1]) in self.matches else self._missing()
        if m := re.fullmatch(r"/teams/(\d+)/matches", path):
            return self._ok(_form(int(m[1])))
        if path == "/competitions":
            return self._ok({"competitions": []})
        return self._missing()

    @staticmethod
    def _ok(payload: dict) -> HttpResponse:
        return HttpResponse(200, {}, json.dumps(payload).encode())

    @staticmethod
    def _missing() -> HttpResponse:
        return HttpResponse(404, {}, b"{}")


class ByMatch:
    """The goal model per match: a forecast for leagues with history, Unavailable otherwise."""

    def __init__(self) -> None:
        self.calls = 0

    async def forecast(self, event) -> MatchForecast | Unavailable:
        self.calls += 1
        mid = int(event.id.removeprefix("fd:"))
        return Unavailable("small_sample") if mid in NO_MODEL else fake_forecast()


def feed_line(line_id: int, country: str, league: str, home: str, away: str, *, kickoff: str = KICKOFF,
              odds: dict | None = None, live: bool = False) -> dict:
    """One line exactly as the bot pushes it."""
    return {"id": line_id, "country": country, "league": league, "home": home, "away": away, "kickoff": kickoff,
            "live": live, "fetchedAt": None, "odds": PRICES if odds is None else odds}


# Live spellings of the feed (2026-11-12): season suffix, reversed CL country, NL/PT "Premier League".
LIVE_PUSH = [
    feed_line(101, "England", "Premier League. Season 26/27", "Arsenal", "Chelsea"),
    feed_line(102, "Champions League UEFA", "League phase", "Bayern Munich", "PSG",
              odds={"w1": 1.95, "x": 3.90, "w2": 3.60, "over25": 1.55, "under25": 2.45}),
    feed_line(107, "Netherlands", "Premier League", "Ajax", "PSV Eindhoven",
              odds={"w1": 2.40, "x": 3.70, "w2": 2.70}),
    feed_line(108, "Portugal", "Premier League", "Benfica", "FC Porto",
              odds={"w1": 2.05, "x": 3.40, "w2": 3.60}),
    # Competitions that have no odds source: never stored, whatever the names.
    feed_line(103, "England", "FA Cup", "Manchester United", "Tottenham"),
    feed_line(104, "World Cup 2030", "Qualification. UEFA", "Spain", "Italy"),
    feed_line(105, "UEFA Nations League", "League A", "Germany", "England"),
    feed_line(106, "Finland", "Veikkausliiga", "HJK", "KuPS"),
    # Same names, same time, another competition: never prices the English match.
    feed_line(201, "Ukraine", "Premier League. Season 26/27", "Arsenal", "Chelsea", odds={"w1": 9.0, "x": 5.0, "w2": 1.3}),
]


@pytest.fixture()
def chain(tmp_path, clock: FakeClock, monkeypatch):
    provider, model, forecaster = Provider(), FakeModel(answer("home_form", "away_form")), ByMatch()
    real = app_module.build_ai_service

    def with_fakes(settings, sports, **kw):
        sports.football.client._sleep = no_sleep
        sports._known_ids.update(f"fd:{mid}" for mid in MATCHES)
        return real(settings, sports, client=model, forecaster=forecaster, **kw)

    monkeypatch.setattr(app_module, "build_ai_service", with_fakes)
    codes = tuple(dict.fromkeys(code for code, _, _ in MATCHES.values()))
    settings = base_settings(tmp_path, football_data_key=KEY, odds_feed_secret=SECRET.decode(),
                             football_data_competitions=codes, ai_per_user_daily=3, ai_daily_budget=50)
    original = app_module.build_service

    def service(settings, **kw):
        return original(settings, **{**kw, "transport": provider, "warm_standings": False, "warm_calendars": False,
                                     "warm_emblems": False})

    monkeypatch.setattr(app_module, "build_service", service)
    client = TestClient(app_module.create_app(settings, clock=clock), base_url="http://127.0.0.1:8000")
    csrf = login(client, fresh(clock)).json()["session"]["csrf_token"]
    return client, csrf, model, provider


def push(client: TestClient, clock: FakeClock, *lines: dict, nonce: str = "a" * 32) -> dict:
    r = post(client, clock, body(clock, *lines, nonce=nonce))
    assert r.status_code == 200, r.text
    return r.json()


def analyse(client: TestClient, csrf: str, mid: int) -> dict:
    r = client.post("/api/ai/analysis", content=json.dumps({"event_id": f"fd:{mid}"}),
                    headers={"Content-Type": "application/json", "Origin": ORIGIN, "X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    return r.json()


def facts(model: FakeModel, i: int = -1) -> dict:
    return json.loads(model.calls[i][1].split("Facts (JSON):\n", 1)[1])


def page_odds(client: TestClient, mid: int) -> dict | None:
    r = client.get(f"/api/sports/event/fd:{mid}")
    assert r.status_code == 200, r.text
    return r.json()["odds"]


# --- the push itself ----------------------------------------------------------------------------

def test_a_live_spelled_push_stores_only_the_served_competitions(chain, clock: FakeClock) -> None:
    client, _, _, _ = chain
    report = push(client, clock, *LIVE_PUSH)
    assert report == {"received": 9, "accepted": 4, "unknownCompetition": 5, "withoutOdds": 0}
    # The match page shows exactly the pushed prices, from the right line.
    pl = {q["market"]: q["odds"] for q in page_odds(client, 1)["quotes"]}
    assert pl["1"] == 1.80 and pl["X"] == 3.80 and pl["2"] == 4.50 and pl["O2.5"] == 1.75
    assert {q["market"]: q["odds"] for q in page_odds(client, 8)["quotes"]} == {"1": 2.05, "X": 3.40, "2": 3.60}
    for mid in (3, 4, 5, 6):
        assert page_odds(client, mid) is None


# --- the AI analysis per kind of match -----------------------------------------------------------

def test_league_match_with_the_goal_model_and_odds_is_ready_with_the_market_in_the_prompt(chain, clock) -> None:
    client, csrf, model, _ = chain
    push(client, clock, *LIVE_PUSH)
    result = analyse(client, csrf, 1)
    assert result["kind"] == "ready" and result["estimated"] is False and result["reasons"] == []
    assert model.calls[-1][0] == SYSTEM_PROMPT
    market = {q["market"]: q["odds"] for q in facts(model)["market"]}
    assert market["1"] == 1.80 and market["2"] == 4.50  # the real prices go in as data
    shown = {q["market"]: q for q in result["market"]["quotes"]}
    assert shown["1"]["odds"] == 1.80 and shown["1"]["modelProbability"] != shown["1"]["marketProbability"]
    assert result["market"]["fetchedAt"] == iso(clock.now - 60)


def test_portugal_premier_league_is_matched_through_aliases(chain, clock) -> None:
    client, csrf, model, _ = chain
    push(client, clock, *LIVE_PUSH)
    result = analyse(client, csrf, 8)
    assert result["kind"] == "ready" and {q["market"] for q in result["market"]["quotes"]} == {"1", "X", "2"}


def test_cl_match_without_the_goal_model_is_partial_with_the_markets_estimate(chain, clock) -> None:
    client, csrf, model, _ = chain
    push(client, clock, *LIVE_PUSH)
    result = analyse(client, csrf, 2)
    assert result["kind"] == "partial" and result["estimated"] is True and result["pick"] is None
    assert result["reasons"] == ["model_small_sample"] and result["forecast"] is None
    assert sum(result["probabilities"].values()) == 100 and result["probabilities"]["home"] > result["probabilities"]["away"]
    assert result["estimateFrom"]["fetchedAt"] == iso(clock.now - 60)
    assert model.calls[-1][0] == BASIS_PROMPT
    f = facts(model)
    assert {q["market"]: q["odds"] for q in f["market"]} == {"1": 1.95, "X": 3.90, "2": 3.60, "O2.5": 1.55, "U2.5": 2.45}
    assert "Оценка вероятностей" in f["probabilityLabel"]


@pytest.mark.parametrize("mid", [3, 4, 5])
def test_cups_qualifiers_and_the_nations_league_are_partial_without_odds(chain, clock, mid: int) -> None:
    client, csrf, model, _ = chain
    push(client, clock, *LIVE_PUSH)
    result = analyse(client, csrf, mid)
    assert result["kind"] == "partial" and result["estimated"] is True
    assert result["reasons"] == ["model_small_sample", "no_odds_source"]
    assert result["probabilities"] is None and result["market"] is None and result["pick"] is None
    assert model.calls[-1][0] == BASIS_PROMPT and "market" not in facts(model)  # no invented prices


def test_odds_only_match_is_partial_and_free(chain, clock) -> None:
    client, csrf, model, _ = chain
    push(client, clock, *LIVE_PUSH)
    result = analyse(client, csrf, 7)
    assert result["kind"] == "partial" and result["estimated"] is True and result["factors"] == []
    assert result["reasons"] == ["model_small_sample", "no_verified_form"]
    assert sum(result["probabilities"].values()) == 100
    assert model.calls == []


def test_a_match_with_neither_is_insufficient_never_calls_the_model_and_charges_nothing(chain, clock) -> None:
    client, csrf, model, _ = chain
    push(client, clock, *LIVE_PUSH)
    for _ in range(5):  # more than the personal limit of 3
        result = analyse(client, csrf, 6)
        assert result["kind"] == "insufficient" and result["probabilities"] is None and result["pick"] is None
    assert model.calls == []
    # Nothing was charged: the full personal limit is still there for paid analyses.
    for mid in (1, 3, 4):
        clock.advance(61)  # the provider's per-minute request budget, not ours
        assert analyse(client, csrf, mid)["kind"] in ("ready", "partial")
    assert len(model.calls) == 3


# --- staleness and expiry ------------------------------------------------------------------------

def test_prices_expire_and_the_analysis_falls_back_without_a_new_paid_call(chain, clock) -> None:
    client, csrf, model, _ = chain
    push(client, clock, *LIVE_PUSH)
    assert analyse(client, csrf, 1)["kind"] == "ready"
    assert analyse(client, csrf, 7)["kind"] == "partial"
    calls = len(model.calls)
    # The pushed prices were fetched 60 s before the push: they are servable until MAX_AGE_S after that.
    clock.advance(MAX_AGE_S - TRANSIT_ALLOWANCE_S - 60 + 1)
    assert page_odds(client, 1) is None
    stale = analyse(client, csrf, 1)
    assert stale["kind"] == "partial" and stale["market"] is None and stale["reasons"] == ["no_odds_source"]
    assert stale["estimated"] is True and stale["factors"]  # cached explanation, still shown
    # Odds only, and the odds are gone: nothing verified remains.
    assert analyse(client, csrf, 7)["kind"] == "insufficient"
    assert len(model.calls) == calls
    # A fresh push brings them back without paying again.
    push(client, clock, *LIVE_PUSH, nonce="b" * 32)
    assert analyse(client, csrf, 1)["kind"] == "ready" and len(model.calls) == calls


def test_a_live_marker_removes_the_prices_at_once(chain, clock) -> None:
    client, csrf, _, _ = chain
    push(client, clock, *LIVE_PUSH)
    clock.advance(30)
    push(client, clock, feed_line(101, "England", "Premier League. Season 26/27", "Arsenal", "Chelsea",
                                  odds={}, live=True), nonce="c" * 32)
    assert page_odds(client, 1) is None
    assert analyse(client, csrf, 1)["market"] is None


def test_no_prices_in_the_last_fifteen_minutes_before_kickoff(chain, clock) -> None:
    client, _, _, _ = chain
    clock.now = ts(f"{DAY}T14:40:00Z")
    push(client, clock, *LIVE_PUSH)
    assert page_odds(client, 1) is not None and page_odds(client, 1)["validFor"] <= 300
    clock.now = ts(f"{DAY}T14:45:00Z")
    assert page_odds(client, 1) is None


# --- strict binding --------------------------------------------------------------------------------

@pytest.mark.parametrize(("line", "priced"), [
    (feed_line(301, "England", "Premier League. Season 26/27", "Arsenal", "Tottenham"), False),  # wrong team
    (feed_line(302, "England", "Premier League. Season 26/27", "Chelsea", "Arsenal"), False),  # home/away swapped
    (feed_line(303, "England", "Premier League. Season 26/27", "Arsenal", "Chelsea",
               kickoff=f"{DAY}T15:20:00Z"), False),  # 20 minutes off
    (feed_line(304, "England", "Premier League. Season 26/27", "Arsenal", "Chelsea",
               kickoff=f"{DAY}T15:10:00Z"), True),  # 10 minutes off: same match
    (feed_line(305, "England", "Premier League. Season 26/27", "Arsenal U21", "Chelsea U21"), False),
    (feed_line(306, "England", "Premier League 2", "Arsenal", "Chelsea"), False),
    (feed_line(307, "Portugal", "Premier League", "Arsenal", "Chelsea"), False),  # another country's PL
])
def test_only_the_same_match_in_the_same_order_gets_prices(chain, clock, line: dict, priced: bool) -> None:
    client, csrf, model, _ = chain
    push(client, clock, line)
    assert (page_odds(client, 1) is not None) is priced
    result = analyse(client, csrf, 1)
    assert (result["market"] is not None) is priced
    assert ("market" in facts(model)) is priced


def test_a_swapped_twin_next_to_the_right_line_withholds_both(chain, clock) -> None:
    client, _, _, _ = chain
    push(client, clock, LIVE_PUSH[0], feed_line(309, "England", "Premier League. Season 26/27", "Chelsea", "Arsenal"))
    assert page_odds(client, 1) is None
