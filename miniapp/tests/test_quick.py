"""Quick prediction (M3.6): model-only choice, honest counts, never loosened criteria."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from miniapp.forecast.engine import Unavailable
from miniapp.forecast.quick import (frechet, STATUS_NO_BET, STATUS_NOT_ENOUGH, STATUS_OK, Criteria, Option, choose,
                                    suitable_markets)
from miniapp.forecast.selection import NO_BET_EARLY, Candidate, Selection
from miniapp.quick.service import MAX_IN_FLIGHT, PER_USER_PER_MINUTE, QuickBusy, QuickService
from miniapp.sports.client import UpstreamError
from miniapp.sports.football_data import normalize_match
from miniapp.sports.service import EventsResult, ModelHistory, RequestError
from miniapp.tests.forecast_fakes import fake_forecast
from miniapp.tests.test_sports import ALLOWED, DAY, FETCHED, match

NOW = datetime.fromisoformat(f"{DAY}T08:00:00+00:00").timestamp()


def forecast(pick: tuple[str, float, float] | None, *alts: tuple[str, float, float], reason: str | None = None):
    f = fake_forecast()
    cand = [Candidate(m, p, b) for m, p, b in ((pick,) if pick else ()) + alts]
    sel = Selection(cand[0], tuple(cand[1:]), None) if cand else Selection(None, (), reason or "no_qualifying_market")
    return replace(f, selection=sel)


def option(mid: int, f, home: str = "", away: str = "", kickoff: str = f"{DAY}T15:00:00Z") -> Option:
    return Option(f"fd:{mid}", kickoff, (home or f"h{mid}", away or f"a{mid}"), f)


def test_criteria_are_strict() -> None:
    for bad in (("single", "any", "balanced", 2), ("coupon", "any", "balanced", 6), ("coupon", "any", "balanced", 1),
                ("x", "any", "balanced", 1), ("single", "corners", "balanced", 1), ("single", "any", "max", 1)):
        with pytest.raises(ValueError):
            Criteria(*bad)


def test_only_the_selections_own_markets_count_and_risk_is_a_floor() -> None:
    f = forecast(("1", 0.62, 0.45), ("HO0.5", 0.82, 0.72), ("X2", 0.58, 0.55))
    assert [c.market for c in suitable_markets(f, Criteria("single", "any", "elevated", 1))] == ["1", "HO0.5", "X2"]
    assert [c.market for c in suitable_markets(f, Criteria("single", "any", "conservative", 1))] == ["HO0.5"]
    assert [c.market for c in suitable_markets(f, Criteria("single", "double", "balanced", 1))] == []
    # A NO_BET match offers nothing at all, whatever its raw probabilities.
    assert suitable_markets(forecast(None, reason=NO_BET_EARLY), Criteria("single", "any", "elevated", 1)) == []


def test_single_takes_the_strongest_or_says_no_bet() -> None:
    weak, strong = option(1, forecast(("1", 0.60, 0.50))), option(2, forecast(("2", 0.64, 0.40)))
    r = choose([weak, strong], Criteria("single", "any", "balanced", 1))
    assert r.status == STATUS_OK and [leg.option.event_id for leg in r.legs] == ["fd:2"] and r.suitable == 2
    none = choose([weak], Criteria("single", "total", "balanced", 1))  # totals are never selected by the model
    assert none.status == STATUS_NO_BET and none.legs == () and none.suitable == 0


def test_coupon_uses_different_matches_without_shared_teams_and_is_never_loosened() -> None:
    a = option(1, forecast(("1", 0.70, 0.45)), "t1", "t2")
    b = option(2, forecast(("1X", 0.80, 0.60)), "t1", "t3")  # shares t1 with a
    c = option(3, forecast(("2", 0.66, 0.40)), "t4", "t5")
    r = choose([a, b, c], Criteria("coupon", "any", "elevated", 2))
    assert r.status == STATUS_OK and [leg.option.event_id for leg in r.legs] == ["fd:3", "fd:1"]
    assert r.combined == pytest.approx((0.36, 0.66)) and r.suitable == 3  # Fréchet bounds, nothing multiplied
    assert frechet([0.85, 0.88, 0.69]) == pytest.approx((0.42, 0.69)) and frechet([0.6] * 5) == (0.0, 0.6)
    short = choose([a, b, c], Criteria("coupon", "any", "elevated", 3))
    assert short.status == STATUS_NOT_ENOUGH and short.legs == () and short.suitable == 2


def test_coupon_is_found_where_a_greedy_choice_would_fail() -> None:
    ab = option(1, forecast(("1", 0.80, 0.40)), "A", "B")
    ac = option(2, forecast(("1", 0.70, 0.40)), "A", "C")
    bd = option(3, forecast(("1", 0.68, 0.40)), "B", "D")
    r = choose([ab, ac, bd], Criteria("coupon", "any", "elevated", 2))
    assert r.status == STATUS_OK and [leg.option.event_id for leg in r.legs] == ["fd:2", "fd:3"]
    short = choose([ab, ac], Criteria("coupon", "any", "elevated", 2))
    assert short.status == STATUS_NOT_ENOUGH and short.suitable == 1


def test_order_is_deterministic() -> None:
    opts = [option(i, forecast(("1", 0.6, 0.5))) for i in (3, 1, 2)]
    for perm in (opts, list(reversed(opts))):
        r = choose(perm, Criteria("coupon", "any", "balanced", 3))
        assert [leg.option.event_id for leg in r.legs] == ["fd:1", "fd:2", "fd:3"]


# --- service ---------------------------------------------------------------------------------------

def ev(mid: int, status: str = "TIMED", code: str = "PL", utc: str = f"{DAY}T15:00:00Z", home=None, away=None):
    kw = {}
    if home:
        kw["home"], kw["away"] = home, away
    e = normalize_match(match(mid, status, code=code, utc=utc, **kw), FETCHED, ALLOWED)
    assert e is not None
    return e


class StubSports:
    def __init__(self, events, failing=(), day_cost=0, history_cost=0, headroom=8) -> None:
        self._events, self.failing, self.history_calls, self.ranges = events, set(failing), [], []
        self._day_cost, self._history_cost = day_cost, history_cost
        self.headroom = headroom  # the provider client's minute counter

    def upstream_headroom(self):
        return self.headroom

    def tournaments(self, sport):
        from miniapp.sports.football_data import tournaments
        return tournaments(("PL", "PD"))

    connected_ = True

    def connected(self, sport):
        return self.connected_

    def day_cost(self, day):
        return self._day_cost

    def history_cost(self, tournament):
        return self._history_cost

    stale = False

    async def events(self, sport, start, end, tournament):
        self.ranges.append((start, end))
        self.headroom -= self._day_cost
        return EventsResult(sport, [e for e in self._events if start <= at(e.starts_at) < end], [], FETCHED, self.stale)

    transient: frozenset = frozenset()
    cached: dict = {}  # tournament -> ModelHistory available without a request (the preview)

    def cached_model_history(self, tournament):
        return self.cached.get(tournament)

    async def model_history(self, tournament):
        self.history_calls.append(tournament)
        self.headroom -= self._history_cost
        if tournament in self.failing:
            raise UpstreamError("rate_limited")
        if tournament in self.transient:
            return ModelHistory([], "2026-08-01", False, "transient")
        return ModelHistory([], "2026-08-01", True)


class StubForecaster:
    def __init__(self, by_id) -> None:
        self.by_id = by_id

    def compute(self, event, history):
        return self.by_id[event.id]


def service(events, by_id, failing=(), clock=lambda: NOW, **costs) -> tuple[QuickService, StubSports]:
    sports = StubSports(events, failing, **costs)
    return QuickService(sports, clock=clock, forecaster=StubForecaster(by_id)), sports  # type: ignore[arg-type]


def at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


START = at(f"{DAY}T08:00:00Z")
SINGLE = Criteria("single", "any", "balanced", 1)


def pick(svc, end: str, criteria=SINGLE, tournaments=None, start=START, user=1):
    return asyncio.run(svc.pick(user, start, at(end), tournaments, criteria)).to_json()


def test_service_counts_honestly_and_reports_unavailable_sources() -> None:
    events = [ev(1), ev(2, home=(1, "A FC"), away=(2, "B FC")), ev(3, "FINISHED"),
              ev(4, code="PD", home=(5, "E FC"), away=(6, "F FC")),
              ev(5, utc=f"{DAY}T07:00:00Z", home=(7, "G FC"), away=(8, "H FC")),  # kicked off before now
              ev(6, utc="2027-01-17T15:00:00Z", home=(9, "I FC"), away=(10, "J FC"))]  # a later day
    by_id = {"fd:1": forecast(("1", 0.64, 0.45)), "fd:2": forecast(None, reason="no_qualifying_market"),
             "fd:4": forecast(("2", 0.7, 0.4))}
    svc, sports = service(events, by_id, failing={"fd:PD"})
    body = pick(svc, "2027-01-22T08:00:00Z")
    assert body["status"] == STATUS_OK and body["legs"][0]["event"]["id"] == "fd:1"
    assert body["counts"] == {"matches": 4, "forecasts": 2}
    # The later match is never forecast (no history for it): it cannot be recommended yet.
    assert body["reasons"] == {NO_BET_EARLY: 1, "no_qualifying_market": 1, "source_unavailable": 1}
    assert body["unavailableTournaments"] == ["fd:PD"] and body["experimental"] is True and body["uncheckedDays"] == 0
    assert body["nextMatchAt"] == "2027-01-17T15:00:00Z"  # the later match: picked only on its own day
    leg = body["legs"][0]
    assert leg["probability"] == by_id["fd:1"].display["1"] and leg["leagueRate"] == 45
    # One listing per UTC day, today first, from now (not from midnight).
    assert sports.ranges[0][0] == START and len(sports.ranges) == 8
    assert sorted(sports.history_calls) == ["fd:PD", "fd:PL"]


def test_provider_budget_is_planned_and_missing_data_is_retryable_not_no_bet() -> None:
    events = [ev(1), ev(2, code="PD", home=(1, "A FC"), away=(2, "B FC"))]
    by_id = {"fd:1": forecast(None, reason="no_qualifying_market"), "fd:2": forecast(("1", 0.64, 0.45))}
    # 8 requests left this minute, 3 kept for everything else: today (1) + one history (3) + one more day.
    svc, sports = service(events, by_id, day_cost=1, history_cost=3)
    body = pick(svc, "2027-01-22T08:00:00Z")
    assert len(sports.history_calls) == 1 and body["unavailableTournaments"] in (["fd:PD"], ["fd:PL"])
    assert body["uncheckedDays"] == 6 and len(sports.ranges) == 2
    assert body["nextMatchAt"] is None  # days unchecked: no claim about "the next match"
    svc, sports = service(events, by_id, day_cost=6)  # not even today's listing fits
    body = pick(svc, "2027-01-16T00:00:00Z")
    assert body["status"] == "unavailable" and body["uncheckedDays"] == 1 and sports.ranges == []
    # Today's listing loaded and empty, later days not checked: "data missing", not "no matches".
    svc, _ = service([ev(9, utc="2027-01-18T15:00:00Z")], {}, day_cost=1, headroom=4)
    body = pick(svc, "2027-01-22T08:00:00Z")
    assert body["status"] == "unavailable" and body["uncheckedDays"] == 7 and body["counts"]["matches"] == 0
    svc, _ = service(events, {"fd:1": forecast(("1", 0.64, 0.45))}, failing={"fd:PL", "fd:PD"})
    assert pick(svc, "2027-01-16T00:00:00Z")["status"] == "unavailable"


def test_a_match_that_kicks_off_during_the_pick_is_not_offered() -> None:
    times = iter([NOW, NOW, at(f"{DAY}T15:00:01Z").timestamp()])
    svc, _ = service([ev(1)], {"fd:1": forecast(("1", 0.64, 0.45))}, clock=lambda: next(times, at(f"{DAY}T15:00:01Z").timestamp()))
    body = pick(svc, "2027-01-16T00:00:00Z")
    assert body["status"] == "no_bet" and body["legs"] == [] and body["counts"]["matches"] == 0


def test_service_validates_period_and_tournaments_and_limits_each_user() -> None:
    svc, _ = service([], {})
    for start, end in ((START, "2027-01-23T09:00:00Z"), (at("2027-01-16T08:00:00Z"), "2027-01-17T08:00:00Z"),
                       (START, f"{DAY}T08:00:00Z")):
        with pytest.raises(RequestError):
            pick(svc, end, start=start)
    with pytest.raises(RequestError):
        pick(svc, "2027-01-16T00:00:00Z", tournaments=frozenset({"fd:XX"}))
    for _ in range(PER_USER_PER_MINUTE):
        assert pick(svc, "2027-01-16T00:00:00Z", tournaments=frozenset({"fd:PL"}))["status"] == STATUS_NO_BET
    with pytest.raises(QuickBusy):
        pick(svc, "2027-01-16T00:00:00Z")
    assert pick(svc, "2027-01-16T00:00:00Z", user=2)["counts"]["matches"] == 0


def test_model_unavailable_is_a_counted_reason() -> None:
    svc, _ = service([ev(1)], {"fd:1": Unavailable("team_history")})
    body = pick(svc, "2027-01-16T00:00:00Z", Criteria("coupon", "any", "balanced", 2))
    assert body["status"] == STATUS_NOT_ENOUGH and body["reasons"] == {"model_team_history": 1}
    assert body["combined"] is None and body["legs"] == []


def test_missing_data_makes_an_unsuccessful_answer_retryable() -> None:
    events = [ev(1), ev(2, code="PD", home=(1, "A FC"), away=(2, "B FC"))]
    # PL was assessed (its only outcome is not a handicap), PD could not be loaded.
    svc, _ = service(events, {"fd:1": forecast(("1", 0.64, 0.45))}, failing={"fd:PD"})
    body = pick(svc, "2027-01-16T00:00:00Z", Criteria("single", "handicap", "balanced", 1))
    assert body["status"] == "unavailable" and body["counts"]["forecasts"] == 1
    # A coupon short of legs keeps its honest count of what did qualify.
    svc, _ = service(events, {"fd:1": forecast(("1", 0.64, 0.45))}, failing={"fd:PD"})
    short = pick(svc, "2027-01-16T00:00:00Z", Criteria("coupon", "any", "balanced", 2))
    assert short["status"] == "unavailable" and short["suitable"] == 1
    # A previous season that failed for now (not absent at the source) is "retry later" too.
    svc, sports = service(events, {"fd:1": forecast(None, reason="partial_history")})
    sports.transient = frozenset({"fd:PL", "fd:PD"})
    late = pick(svc, "2027-01-16T00:00:00Z")
    assert late["status"] == "unavailable" and late["unavailableTournaments"] == ["fd:PD", "fd:PL"]
    # A found pick stays a pick; the missing competition is still reported next to it.
    svc, _ = service(events, {"fd:1": forecast(("1", 0.64, 0.45))}, failing={"fd:PD"})
    found = pick(svc, "2027-01-16T00:00:00Z")
    assert found["status"] == STATUS_OK and found["unavailableTournaments"] == ["fd:PD"]


def test_coupon_answer_gives_bounds_of_the_shown_percents() -> None:
    events = [ev(1), ev(2, home=(1, "A FC"), away=(2, "B FC"))]
    by_id = {"fd:1": forecast(("1", 0.64, 0.45)), "fd:2": forecast(("1", 0.64, 0.45))}
    svc, _ = service(events, by_id)
    body = pick(svc, "2027-01-16T00:00:00Z", Criteria("coupon", "any", "balanced", 2))
    # Exact 0.64 each: [floor(28), ceil(64)] — computed from the model's numbers, rounded outward.
    assert body["combined"] == [28, 64]


def test_a_pick_crossing_utc_midnight_is_never_a_mixed_answer() -> None:
    before = at(f"{DAY}T23:59:59Z").timestamp()
    after = at("2027-01-16T00:00:05Z").timestamp()
    times = iter([before, before])  # the pick's start and its admission; the end is past midnight
    events = [ev(1, utc=f"{DAY}T23:59:59Z"), ev(2, utc="2027-01-16T15:00:00Z", home=(1, "A FC"), away=(2, "B FC"))]
    svc, _ = service(events, {"fd:1": forecast(("1", 0.64, 0.45))}, clock=lambda: next(times, after))
    body = asyncio.run(svc.pick(1, at(f"{DAY}T23:59:00Z"), at("2027-01-17T00:00:00Z"), None, SINGLE)).to_json()
    assert body["status"] == "unavailable" and body["legs"] == [] and body["nextMatchAt"] is None
    assert body["computedAt"].startswith("2027-01-16")


def test_midnight_right_after_the_first_clock_reading_is_caught_too() -> None:
    before = at(f"{DAY}T23:59:59Z").timestamp()
    after = at("2027-01-16T00:00:01Z").timestamp()
    times = iter([before])  # the one reading that starts the pick; every later one is past midnight
    events = [ev(1, utc="2027-01-16T15:00:00Z")]
    svc, _ = service(events, {"fd:1": forecast(("1", 0.64, 0.45))}, clock=lambda: next(times, after))
    body = asyncio.run(svc.pick(1, at(f"{DAY}T23:59:00Z"), at("2027-01-17T00:00:00Z"), None, SINGLE)).to_json()
    assert body["status"] == "unavailable" and body["nextMatchAt"] is None and body["legs"] == []


def test_a_stale_listing_is_never_a_pick_nor_no_matches() -> None:
    svc, sports = service([ev(1)], {"fd:1": forecast(("1", 0.64, 0.45))})
    sports.stale = True
    body = pick(svc, "2027-01-16T00:00:00Z")
    assert body["status"] == "unavailable" and body["legs"] == [] and body["uncheckedDays"] == 1
    assert sports.history_calls == []


def test_without_a_source_the_answer_is_unavailable_not_no_matches() -> None:
    svc, sports = service([ev(1)], {})
    sports.connected_ = False
    body = pick(svc, "2027-01-16T00:00:00Z")
    assert body["status"] == "unavailable" and sports.ranges == [] and sports.history_calls == []


def test_a_cold_history_loads_on_the_free_plan_and_retries_never_eat_the_reserve(tmp_path) -> None:
    """Real SportsService and provider client (8/minute, 2 retries): a cold pick loads today's
    listing and a competition's two-request history; failing attempts stop at the reserve."""
    from miniapp.sports.client import HttpResponse
    from miniapp.tests.conftest import FakeClock
    from miniapp.tests.test_sports import FakeTransport, make_service
    from miniapp.tests.test_tournaments_crests import season_payload

    clock, t = FakeClock(NOW), FakeTransport()
    sports = make_service(t, clock)
    forecaster = StubForecaster({"fd:1": forecast(("1", 0.64, 0.45))})
    svc = QuickService(sports, clock=clock, forecaster=forecaster)  # type: ignore[arg-type]
    t.script = [t.ok({"matches": [match(1)]}), t.ok(season_payload(match(2, "FINISHED", full=(1, 0)))),
                t.ok(season_payload(match(3, "FINISHED", full=(2, 0)),
                                    season={"id": 1, "startDate": "2025-08-15", "endDate": "2026-05-24", "currentMatchday": 38}))]
    body = pick(svc, "2027-01-16T00:00:00Z")
    assert body["status"] == STATUS_OK and len(t.calls) == 3
    assert sports.cache.peek("football:calendar:PL") is not None
    # A failing provider: retries stop once only the reserve is left (8 − 3 = 5 attempts in the minute).
    clock.advance(3600)
    t.script, t.default = [], HttpResponse(500, {}, b"")
    before = len(t.calls)
    pick(svc, "2027-01-16T00:00:00Z", user=2)
    assert sports.football.client.minute_headroom() >= 3 and len(t.calls) - before <= 5


def test_only_a_few_picks_run_at_once_for_everyone() -> None:
    gate = asyncio.Event()

    class Slow(StubSports):
        async def events(self, sport, start, end, tournament):
            await gate.wait()
            return await super().events(sport, start, end, tournament)

    sports = Slow([ev(1)])
    svc = QuickService(sports, clock=lambda: NOW, forecaster=StubForecaster({"fd:1": forecast(None)}))  # type: ignore[arg-type]

    async def scenario():
        running = [asyncio.create_task(svc.pick(u, START, at("2027-01-16T00:00:00Z"), None, SINGLE))
                   for u in range(MAX_IN_FLIGHT)]
        await asyncio.sleep(0)
        with pytest.raises(QuickBusy):
            await svc.pick(99, START, at("2027-01-16T00:00:00Z"), None, SINGLE)
        gate.set()
        await asyncio.gather(*running)
        assert (await svc.pick(99, START, at("2027-01-16T00:00:00Z"), None, SINGLE)).to_json()["status"] == "no_bet"

    asyncio.run(scenario())


def test_route_requires_a_session_and_strict_parameters(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from miniapp.app import create_app
    from miniapp.sports.service import build_service
    from miniapp.tests.conftest import FakeClock, fresh, login
    from miniapp.tests.test_sports import KEY, FakeTransport, base_settings, no_sleep

    clock, t = FakeClock(NOW), FakeTransport()
    settings = base_settings(tmp_path, football_data_key=KEY, session_ttl_s=3600)
    sports = build_service(settings, clock=clock, transport=t, warm_emblems=False)
    sports.football.client._sleep = no_sleep
    client = TestClient(create_app(settings, clock=clock, sports=sports), base_url="http://127.0.0.1:8000")
    url = f"/api/quick/pick?mode=single&from={DAY}T08:00:00Z&to={DAY}T23:00:00Z"
    assert client.get(url).status_code == 401 and t.calls == []
    login(client, fresh(clock))
    for bad in ("&size=2", "&risk=max", "&x=1", "&market=any&market=1x2", "&tournaments=" + ",".join(["a"] * 21)):
        assert client.get(url + bad).status_code == 400, bad
    assert client.get(url.replace("mode=single", "mode=coupon")).status_code == 400  # no size
    assert client.get(url + "&tournaments=fd:XX").json()["error"] == "unknown_tournament"
    t.script = [t.ok({"matches": [match(1, "FINISHED", full=(1, 0))]})]
    body = client.get(url).json()
    assert body["status"] == STATUS_NO_BET and body["counts"] == {"matches": 0, "forecasts": 0}
    assert body["nextMatchAt"] is None and len(t.calls) == 1 and KEY not in str(body)


def test_no_next_match_claim_while_some_days_are_unchecked() -> None:
    events = [ev(3, utc="2027-01-16T15:00:00Z", home=(1, "A FC"), away=(2, "B FC"))]
    # Room for today's and one more listing only: later days stay unchecked.
    svc, sports = service(events, {}, day_cost=1, headroom=5)
    body = pick(svc, "2027-01-22T08:00:00Z")
    assert len(sports.ranges) == 2 and body["uncheckedDays"] == 6 and body["nextMatchAt"] is None
    svc, _ = service(events, {})
    assert pick(svc, "2027-01-22T08:00:00Z")["nextMatchAt"] == "2027-01-16T15:00:00Z"


def test_unvalidated_competitions_cost_no_history_requests() -> None:
    from miniapp.forecast.selection import NO_BET_UNVALIDATED
    from miniapp.sports.football_data import normalize_match as nm
    from miniapp.tests.test_sports import match as raw_match

    pl = ev(1, code="PL", utc=f"{DAY}T18:00:00Z")
    tsl = nm(raw_match(2, "TIMED", code="TSL", utc=f"{DAY}T19:00:00Z"), FETCHED, frozenset({"PL", "TSL"}))
    assert tsl is not None
    svc, sports = service([pl, tsl], {"fd:1": forecast(None)})
    body = pick(svc, f"{DAY}T23:59:00Z")
    assert sports.history_calls == ["fd:PL"]  # the Süper Lig match was classified without a request
    assert body["reasons"].get(NO_BET_UNVALIDATED) == 1


# --- preview: without a pick, the nearest later matches of validated competitions (cache only) -------

def test_no_pick_shows_the_nearest_later_validated_matches_from_cache_only() -> None:
    today = [ev(1)]
    later = [ev(10 + i, utc=f"2027-01-{16 + i}T15:00:00Z", home=(20 + i, f"H{i} FC"), away=(40 + i, f"A{i} FC"))
             for i in range(7)]
    far = ev(30, utc="2027-02-20T15:00:00Z", home=(60, "Far FC"), away=(61, "Away FC"))  # beyond 14 days
    pd = ev(31, code="PD", utc="2027-01-16T12:00:00Z", home=(62, "P FC"), away=(63, "Q FC"))
    by_id = {"fd:1": forecast(None, reason="no_qualifying_market")}
    by_id |= {e.id: forecast(None, reason="early_forecast") for e in later + [far, pd]}
    svc, sports = service(today, by_id)
    sports.cached = {"fd:PL": ModelHistory(today + later + [far], "2026-08-01", True),
                     "fd:PD": ModelHistory([pd], "2026-08-01", True)}
    body = pick(svc, f"{DAY}T23:00:00Z")
    assert body["status"] == STATUS_NO_BET
    preview = body["preview"]
    # five nearest, later days only, both competitions, sorted by kickoff; nothing for the far one
    assert [p["event"]["id"] for p in preview] == ["fd:31", "fd:10", "fd:11", "fd:12", "fd:13"]
    assert all(p["recommendationDay"] == p["event"]["startsAt"][:10] for p in preview)
    p0 = preview[0]["probabilities"]
    assert set(p0) == {"home", "draw", "away"} and 99 <= sum(p0.values()) <= 101
    assert sorted(sports.history_calls) == ["fd:PL"]  # only today's own competition: the preview asked nothing


def test_preview_respects_the_tournament_filter_and_is_absent_with_a_pick() -> None:
    today = [ev(1)]
    pd = ev(31, code="PD", utc="2027-01-16T12:00:00Z", home=(62, "P FC"), away=(63, "Q FC"))
    later_pl = ev(10, utc="2027-01-17T15:00:00Z", home=(20, "H FC"), away=(40, "A FC"))
    by_id = {"fd:1": forecast(None, reason="no_qualifying_market"), "fd:31": forecast(None, reason="early_forecast"),
             "fd:10": forecast(None, reason="early_forecast")}
    svc, sports = service(today, by_id)
    sports.cached = {"fd:PL": ModelHistory([later_pl], "2026-08-01", True), "fd:PD": ModelHistory([pd], "2026-08-01", True)}
    body = pick(svc, f"{DAY}T23:00:00Z", tournaments=frozenset({"fd:PL"}))
    assert [p["event"]["id"] for p in body["preview"]] == ["fd:10"]
    svc2, sports2 = service(today, {"fd:1": forecast(("1", 0.64, 0.45)), "fd:10": forecast(None, reason="early_forecast")})
    sports2.cached = {"fd:PL": ModelHistory([later_pl], "2026-08-01", True)}
    assert pick(svc2, f"{DAY}T23:00:00Z")["status"] == STATUS_OK
    assert pick(svc2, f"{DAY}T23:00:00Z", user=2)["preview"] == []


def test_without_cached_calendars_there_is_no_preview_and_no_request() -> None:
    svc, sports = service([ev(1)], {"fd:1": forecast(None, reason="no_qualifying_market")})
    body = pick(svc, f"{DAY}T23:00:00Z")
    assert body["preview"] == [] and sports.history_calls == ["fd:PL"]


def test_cached_model_history_never_asks_the_provider(tmp_path) -> None:
    from miniapp.tests.test_sports import FakeTransport, make_service
    from miniapp.tests.test_tournaments_crests import season_payload
    from miniapp.tests.conftest import FakeClock
    clock = FakeClock()
    t = FakeTransport()
    svc = make_service(t, clock)
    assert svc.cached_model_history("fd:PL") is None and t.calls == []
    t.script = [t.ok(season_payload(match(2, "FINISHED", utc="2026-09-01T15:00:00Z", full=(1, 0))))]
    asyncio.run(svc.calendar("football", "fd:PL"))
    h = svc.cached_model_history("fd:PL")
    assert h is not None and [e.id for e in h.events] == ["fd:2"] and h.previous_missing == "transient"
    assert len(t.calls) == 1  # only the calendar load itself
