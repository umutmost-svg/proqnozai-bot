"""The pooled national-team goal model (docs/miniapp/national-model.md) — offline, provider faked."""

from __future__ import annotations

import json
import random

from miniapp.forecast import version
from miniapp.forecast.data import MatchResult
from miniapp.forecast.engine import ForecastEngine, MatchForecast, model_gap
from miniapp.forecast.selection import NO_BET_UNVALIDATED
from miniapp.sports import service as sports_service
from miniapp.sports.cache import SportsCache
from miniapp.sports.client import HttpResponse
from miniapp.sports.football_data import FootballDataAdapter
from miniapp.sports.service import SportsService
from miniapp.tests.conftest import NOW, FakeClock
from miniapp.tests.test_sports import make_client, match, run
from miniapp.tests.test_standings import RoutedTransport
from miniapp.tests.test_tournaments_crests import season_payload

DAY = 86400.0
UNL_NOW = {"id": 3, "startDate": "2026-09-01", "endDate": "2027-06-10", "currentMatchday": 6}
UNL_2024 = {"id": 2, "startDate": "2024-09-05", "endDate": "2025-06-08", "currentMatchday": 6}
UNL_2022 = {"id": 1, "startDate": "2022-06-01", "endDate": "2023-06-18", "currentMatchday": 6}  # before the window
QUFA_NOW = {"id": 4, "startDate": "2025-03-21", "endDate": "2027-03-31", "currentMatchday": 10}
QUFA_2023 = {"id": 5, "startDate": "2023-03-23", "endDate": "2024-11-19", "currentMatchday": 10}

NOT_FOUND = HttpResponse(404, {}, json.dumps({"message": "not found"}).encode())
GER, NED, ESP, FRA = (759, "Germany"), (8601, "Netherlands"), (760, "Spain"), (773, "France")


def nation_match(mid: int, code: str, utc: str, home, away, full) -> dict:
    m = match(mid, "FINISHED", utc=utc, full=full, home=home, away=away)
    m["competition"] = {"id": 1, "code": code, "name": code}
    return m


def payload(code: str, season: dict, *matches: dict) -> dict:
    body = season_payload(*matches, code=code, season=season)
    body["competition"]["emblem"] = None
    return body


def season_list(code: str, *seasons: dict) -> dict:
    return {"code": code, "name": code, "seasons": [{"startDate": s["startDate"], "endDate": s["endDate"]}
                                                      for s in seasons]}


def routes(qufa_past=None) -> dict:
    return {
        "/competitions/UNL/matches": payload("UNL", UNL_NOW, nation_match(3, "UNL", "2026-10-10T18:45:00Z", GER, NED, (2, 1))),
        "/competitions/UNL/matches?season=2024": payload(
            "UNL", UNL_2024, nation_match(2, "UNL", "2024-10-10T18:45:00Z", NED, GER, (1, 1))),
        "/competitions/UNL": season_list("UNL", UNL_NOW, UNL_2024, UNL_2022),
        "/competitions/QUFA/matches": payload(
            "QUFA", QUFA_NOW, nation_match(4, "QUFA", "2025-09-05T18:45:00Z", ESP, FRA, (0, 0))),
        "/competitions/QUFA/matches?season=2023": qufa_past if qufa_past is not None else payload(
            "QUFA", QUFA_2023, nation_match(1, "QUFA", "2024-03-21T18:45:00Z", FRA, ESP, (2, 0))),
        "/competitions/QUFA": season_list("QUFA", QUFA_NOW, QUFA_2023),
        # The club rule's "previous season" (start year - 1) that these biennial competitions do not have.
        "/competitions/UNL/matches?season=2025": NOT_FOUND,
        "/competitions/QUFA/matches?season=2024": NOT_FOUND,
    }


def make(transport, clock: FakeClock) -> SportsService:
    client = make_client(transport, clock, per_minute=100, per_day=10000)
    adapter = FootballDataAdapter(client, ("UNL", "QUFA", "PL"), clock)
    return SportsService(adapter, cache=SportsCache(clock=clock), clock=clock)


def test_the_pool_is_every_national_competition_inside_the_window_once_the_warm_up_cached_it(clock: FakeClock) -> None:
    t = RoutedTransport(routes())
    service = make(t, clock)
    assert run(service.model_history("fd:UNL")).pool is None  # nothing warmed yet: the competition's own seasons
    asked = len(t.calls)
    run(service._search_warm_load("fd:UNL"))
    run(service._search_warm_load("fd:QUFA"))
    warmed = [u.rsplit("/v4", 1)[1] for u in t.calls[asked:]]
    assert "/competitions/UNL" in warmed and "/competitions/QUFA/matches?season=2023" in warmed
    assert not any("season=2022" in u for u in t.calls)  # ended before the window
    pool = run(service.model_history("fd:UNL")).pool
    assert pool is not None and sorted(e.id for e in pool) == ["fd:1", "fd:2", "fd:3", "fd:4"]
    assert sorted(e.id for e in service.cached_model_history("fd:QUFA", any_age=True).pool) == ["fd:1", "fd:2", "fd:3", "fd:4"]
    assert service.cached_model_history("fd:UNL", any_age=True).events[-1].id == "fd:3"  # own events unchanged
    assert service._national_pool("PL") is None  # a club competition never pools


def test_a_season_the_source_lacks_is_no_gap_but_one_that_failed_to_load_is(clock: FakeClock) -> None:
    t = RoutedTransport(routes(qufa_past=NOT_FOUND))
    service = make(t, clock)
    run(service._search_warm_load("fd:UNL"))
    run(service._search_warm_load("fd:QUFA"))
    assert sorted(e.id for e in service._national_pool("UNL")) == ["fd:2", "fd:3", "fd:4"]

    broken = HttpResponse(500, {}, b"{}")
    t2 = RoutedTransport(routes(qufa_past=broken))
    other = make(t2, FakeClock())
    run(other._search_warm_load("fd:UNL"))
    run(other._search_warm_load("fd:QUFA"))
    assert other._national_pool("UNL") is None  # a partial pool would rate a confederation from a fraction


def test_the_sports_layer_repeats_the_window_of_the_forecast_layer() -> None:
    assert sports_service.NATIONAL_WINDOW_DAYS == version.NATIONAL_WINDOW_DAYS


def _results(seed: int = 7) -> list[MatchResult]:
    rng = random.Random(seed)
    out, mid = [], 1
    for day in range(1000, 0, -4):  # ~250 matches over the last ~2.7 years, 16 teams
        for _ in range(1):
            h, a = rng.sample(range(1, 17), 2)
            out.append(MatchResult(mid, NOW - day * DAY, h, a, rng.randint(0, 3), rng.randint(0, 2)))
            mid += 1
    return out


def test_a_pooled_forecast_uses_the_national_parameters_on_its_window_and_never_recommends() -> None:
    results = _results()
    ancient = [MatchResult(10_000 + i, NOW - 1400 * DAY + i * DAY, 1, 2, 9, 0) for i in range(30)]
    engine = ForecastEngine()
    pooled = engine.forecast("UNL", ancient + results, None, 1, 2, NOW, pooled=True)
    assert isinstance(pooled, MatchForecast)
    assert pooled.sample == len(results)  # older than the window: never learned from
    assert pooled.pool == version.NATIONAL_POOL and pooled.parameters.endswith("+" + version.NATIONAL_POOL)
    assert pooled.to_json()["pool"] == version.NATIONAL_POOL and pooled.limited and not pooled.gate_eligible
    assert pooled.selection.pick is None and pooled.selection.no_bet_reason == NO_BET_UNVALIDATED
    club = engine.forecast("UNL", results, None, 1, 2, NOW)
    assert isinstance(club, MatchForecast) and club.pool is None and club.expected_goals != pooled.expected_goals


def test_the_lists_count_a_teams_matches_inside_the_pools_window_only() -> None:
    results = _results()
    old_only = [MatchResult(20_000 + i, NOW - 1300 * DAY + i * DAY, 99, 1, 1, 1) for i in range(5)]
    assert model_gap(old_only + results, 99, 2, NOW) is None  # the club rule has no window
    assert model_gap(old_only + results, 99, 2, NOW, pooled=True) == "team_history"
    assert model_gap(results, 1, 2, NOW, pooled=True) is None
