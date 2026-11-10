"""Tournament tables and the goal model's season history (M3.5) — offline, provider faked."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from miniapp.app import create_app
from miniapp.forecast.data import results_from_events
from miniapp.sports.client import HttpResponse, UpstreamError
from miniapp.sports.football_data import normalize_match, parse_standings
from miniapp.sports.service import build_service
from miniapp.tests.conftest import FakeClock, fresh, login
from miniapp.tests.test_sports import ALLOWED, FETCHED, KEY, FakeTransport, base_settings, make_service, match, \
    no_sleep, run
from miniapp.tests.test_tournaments_crests import season_payload


def row(pos: int, team: int, name: str, pts: int, gd: int | None = 3, **over) -> dict:
    r = {"position": pos, "team": {"id": team, "name": name, "shortName": name.split()[0],
                                   "crest": f"https://crests.football-data.org/{team}.png"},
         "playedGames": 10, "won": 6, "draw": 2, "lost": 2, "points": pts, "goalsFor": 18, "goalsAgainst": 15}
    if gd is not None:
        r["goalDifference"] = gd
    r.update(over)
    return r


def table(*blocks: dict, code: str = "PL") -> dict:
    return {"competition": {"code": code, "emblem": f"https://crests.football-data.org/{code}.png"},
            "season": {"id": 9, "startDate": "2026-08-21", "endDate": "2027-05-30", "currentMatchday": 10},
            "standings": list(blocks)}


def block(*rows: dict, stage: str = "REGULAR_SEASON", group: str | None = "Matchday", kind: str = "TOTAL") -> dict:
    return {"stage": stage, "type": kind, "group": group, "table": list(rows)}


LEAGUE = table(block(row(1, 57, "Arsenal FC", 24), row(2, 65, "Manchester City FC", 24, gd=9),
                     row(3, 61, "Chelsea FC", 21, gd=-2)),
               block(row(1, 65, "Manchester City FC", 15), kind="HOME"))


def test_table_keeps_the_providers_order_and_numbers() -> None:
    s = parse_standings(LEAGUE)
    assert len(s.blocks) == 1  # home / away splits are not shown
    rows = s.blocks[0].rows
    # Arsenal first on equal points: the provider's tie-break, never re-sorted here.
    assert [(r.position, r.team.name, r.points, r.goal_difference) for r in rows] == [
        (1, "Arsenal FC", 24, 3), (2, "Manchester City FC", 24, 9), (3, "Chelsea FC", 21, -2)]
    assert s.season.start == "2026-08-21" and rows[0].team.crest_path == "/api/crests/team/57"
    assert s.rows_by_team()["fd:team:61"].position == 3


def test_group_stages_stay_separate_and_a_competition_without_a_table_is_empty() -> None:
    groups = parse_standings(table(block(row(1, 1, "A FC", 6), stage="GROUP_STAGE", group="GROUP_A"),
                                   block(row(1, 2, "B FC", 4), stage="GROUP_STAGE", group="GROUP_B")))
    assert [(b.stage, b.group, [r.team.name for r in b.rows]) for b in groups.blocks] == [
        ("GROUP_STAGE", "GROUP_A", ["A FC"]), ("GROUP_STAGE", "GROUP_B", ["B FC"])]
    league_phase = parse_standings(table(block(row(1, 5, "Bayern", 15), stage="LEAGUE_STAGE", group="League phase")))
    assert league_phase.blocks[0].stage == "LEAGUE_STAGE" and len(league_phase.blocks) == 1
    assert parse_standings(table()).blocks == ()


def test_a_missing_goal_difference_is_plain_arithmetic_never_a_ranking() -> None:
    s = parse_standings(table(block(row(1, 1, "A FC", 6, gd=None, goalsFor=9, goalsAgainst=4))))
    assert s.blocks[0].rows[0].goal_difference == 5


@pytest.mark.parametrize("broken", [
    table(block(row(1, 1, "A FC", 6, points=None))),
    table(block(row(1, 1, "A FC", 6), row(2, 1, "A FC", 3))),  # the same team twice
    table(block(row(1, 1, "A FC", 6) | {"team": {"id": None, "name": "x"}})),
    table(block(row(1, 1, "A FC", 6, goalDifference="3"))),
    {"standings": "nope"},
    table(block(row(1, 1, "A FC", 6)) | {"type": None}),   # a block of unknown kind
    table({"stage": "REGULAR_SEASON", "group": None, "table": []}),  # truncated: no type at all
    table() | {"season": "2026"},
])
def test_an_unreadable_row_breaks_the_whole_table_instead_of_shifting_positions(broken) -> None:
    with pytest.raises(UpstreamError):
        parse_standings(broken)


def test_standings_are_one_shared_request_for_the_table_screen_and_the_ai(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    t.script = [t.ok(LEAGUE)]
    result = run(service.standings_table("football", "fd:PL"))
    assert result is not None and [r.team.short_name for r in result.standings.blocks[0].rows][0] == "Arsenal"
    assert run(service.standings("fd:PL"))["fd:team:57"].points == 24
    assert len(t.calls) == 1 and t.calls[0][0].endswith("/competitions/PL/standings")
    assert run(service.standings_table("football", "fd:XX")) is None and len(t.calls) == 1


@pytest.mark.parametrize("identity", ["PD", None, "missing", "malformed"])
def test_a_table_without_the_requested_competitions_identity_is_broken(clock: FakeClock, identity) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    payload = table(block(row(1, 1, "A FC", 6)), code="PD" if identity == "PD" else "PL")
    if identity is None:
        payload["competition"] = {"code": None}
    elif identity == "missing":
        del payload["competition"]
    elif identity == "malformed":
        payload["competition"] = "PL"
    t.script = [t.ok(payload)]
    with pytest.raises(UpstreamError):
        run(service.standings_table("football", "fd:PL"))


# --- the goal model's history ----------------------------------------------------------------

PAST = {"id": 8, "startDate": "2025-08-15", "endDate": "2026-05-24", "currentMatchday": 38}


def test_model_history_is_the_current_calendar_plus_the_previous_season(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    current = match(2, "FINISHED", utc="2026-09-01T15:00:00Z", full=(1, 0))
    old = match(1, "FINISHED", utc="2026-01-10T15:00:00Z", full=(3, 1))
    t.script = [t.ok(season_payload(current)), t.ok(season_payload(old, season=PAST))]
    history = run(service.model_history("fd:PL"))
    assert [e.id for e in history.events] == ["fd:1", "fd:2"] and history.previous_season
    assert t.calls[1][0].endswith("/competitions/PL/matches?season=2025")
    assert [(r.home_goals, r.away_goals) for r in results_from_events(history.events)] == [(3, 1), (1, 0)]
    run(service.model_history("fd:PL"))
    assert len(t.calls) == 2  # both cached and shared


def test_a_past_season_answer_for_another_season_is_refused(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    t.script = [t.ok(season_payload(match(2, "FINISHED", full=(1, 0)))), t.ok(season_payload(match(1, "FINISHED", full=(1, 1))))]
    history = run(service.model_history("fd:PL"))  # the provider answered with the current season again
    assert not history.previous_season and [e.id for e in history.events] == ["fd:2"]


def test_without_the_previous_season_the_current_one_still_feeds_the_model(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    t.script = [t.ok(season_payload(match(2, "FINISHED", full=(1, 0)))), HttpResponse(403, {}, b"")]
    history = run(service.model_history("fd:PL"))
    assert not history.previous_season and len(history.events) == 1


def test_only_regular_time_of_matches_played_to_the_end_feeds_the_model() -> None:
    et = match(3, "FINISHED", full=(3, 2))
    et["score"]["duration"], et["score"]["regularTime"] = "EXTRA_TIME", {"home": 2, "away": 2}
    awarded = match(4, "AWARDED", full=(3, 0))
    events = [normalize_match(m, FETCHED, ALLOWED) for m in (et, awarded, match(5, "FINISHED", full=(0, 0)))]
    assert [(r.match_id, r.home_goals, r.away_goals) for r in results_from_events(events)] == [(3, 2, 2), (5, 0, 0)]


def test_calendar_listeners_see_each_loaded_calendar_once(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    seen: list[int] = []
    service.calendar_listeners.append(lambda _tid, events: seen.append(len(events)))
    service.calendar_listeners.insert(0, lambda *_: 1 / 0)  # a failing listener never fails the calendar
    t.script = [t.ok(season_payload(match(2, "FINISHED", full=(1, 0))))]
    run(service.calendar("football", "fd:PL"))
    run(service.calendar("football", "fd:PL"))  # cache hit: not announced again
    assert seen == [1]


# --- HTTP ------------------------------------------------------------------------------------------

@pytest.fixture()
def api(tmp_path, clock: FakeClock):
    t = FakeTransport()
    settings = base_settings(tmp_path, football_data_key=KEY, session_ttl_s=3600)
    service = build_service(settings, clock=clock, transport=t, warm_emblems=False)
    service.football.client._sleep = no_sleep
    return TestClient(create_app(settings, clock=clock, sports=service), base_url="http://127.0.0.1:8000"), t


def test_standings_endpoint(api, clock: FakeClock) -> None:
    client, t = api
    url = "/api/sports/football/tournaments/fd:PL/standings"
    assert client.get(url).status_code == 401 and t.calls == []
    login(client, fresh(clock))
    assert client.get(url + "?x=1").status_code == 400
    assert client.get("/api/sports/football/tournaments/fd:XX/standings").status_code == 404
    t.script = [t.ok(LEAGUE)]
    body = client.get(url).json()
    assert body["season"]["startDate"] == "2026-08-21" and body["stale"] is False
    first = body["blocks"][0]["rows"][0]
    assert first["position"] == 1 and first["team"]["shortName"] == "Arsenal" and first["goalDifference"] == 3
    assert body["coverage"]["attribution"] and KEY not in str(body)
    t.script = [HttpResponse(500, {}, b"")] * 3
    assert client.get("/api/sports/football/tournaments/fd:PD/standings").status_code == 503


def test_a_points_deduction_below_zero_is_kept_as_given() -> None:
    s = parse_standings(table(block(row(1, 1, "A FC", 6), row(2, 2, "B FC", -2, gd=-9))))
    assert [r.points for r in s.blocks[0].rows] == [6, -2]


def test_a_transient_past_season_failure_is_told_apart_from_an_absent_season(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    t.script = [t.ok(season_payload(match(2, "FINISHED", full=(1, 0))))] + [HttpResponse(500, {}, b"")] * 3
    history = run(service.model_history("fd:PL"))
    assert history.previous_season is False and history.previous_missing == "transient"
    ok = run(service.model_history("fd:PL"))  # within the retry pause: still transient, no request
    assert ok.previous_missing == "transient"


def test_an_unavailable_previous_season_is_not_asked_again_for_hours(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    t.script = [t.ok(season_payload(match(2, "FINISHED", full=(1, 0)))), HttpResponse(404, {}, b"")]
    assert run(service.model_history("fd:PL")).previous_missing == "absent"
    clock.advance(60)
    # Calendar cached, past season not retried; still known as absent at the source.
    assert run(service.model_history("fd:PL")).previous_missing == "absent"
    assert len(t.calls) == 2
    clock.advance(6 * 3600)
    t.script = [t.ok(season_payload(match(2, "FINISHED", full=(1, 0)))), HttpResponse(404, {}, b"")]
    run(service.model_history("fd:PL"))
    assert any("season=2025" in url for url, _ in t.calls[2:])


# --- AI evidence latency: the previous season loads alongside a cold calendar, and is warmed ---------

class RoutedTransport:
    """Answers by URL (not by call order), each after an optional gate: shows real overlap."""

    def __init__(self, routes: dict) -> None:
        import asyncio
        self.routes = routes
        self.calls: list[str] = []
        self.started: dict[str, asyncio.Event] = {}
        self.gates: dict[str, asyncio.Event] = {}

    async def __call__(self, url: str, headers, timeout_s: float) -> HttpResponse:
        import asyncio
        self.calls.append(url)
        key = next(k for k in self.routes if url.endswith(k))
        self.started.setdefault(key, asyncio.Event()).set()
        if key in self.gates:
            await self.gates[key].wait()
        answer = self.routes[key]
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, HttpResponse) else HttpResponse(200, {}, __import__("json").dumps(answer).encode())


def _hinted(service, start: str = "2026-08-14") -> None:
    service.football._learn_season({"competition": {"code": "PL"}, "season": {"startDate": start}})


def test_a_cold_calendar_and_the_previous_season_load_together_with_a_season_hint(clock: FakeClock) -> None:
    import asyncio

    current = season_payload(match(2, "FINISHED", utc="2026-09-01T15:00:00Z", full=(1, 0)))
    old = season_payload(match(1, "FINISHED", utc="2026-01-10T15:00:00Z", full=(3, 1)), season=PAST)
    t = RoutedTransport({"/competitions/PL/matches": current, "/competitions/PL/matches?season=2025": old})

    async def scenario():
        service = make_service(t, clock)
        _hinted(service)
        t.gates["/competitions/PL/matches"] = asyncio.Event()
        task = asyncio.ensure_future(service.model_history("fd:PL"))
        # The previous season is asked while the calendar is still loading.
        await asyncio.wait_for(t.started.setdefault("/competitions/PL/matches?season=2025", asyncio.Event()).wait(), 1)
        t.gates["/competitions/PL/matches"].set()
        return await task

    history = run(scenario())
    assert [e.id for e in history.events] == ["fd:1", "fd:2"] and history.previous_season
    assert len(t.calls) == 2


def test_a_wrong_season_hint_never_feeds_the_model_the_wrong_season(clock: FakeClock) -> None:
    current = season_payload(match(2, "FINISHED", utc="2026-09-01T15:00:00Z", full=(1, 0)))
    old = season_payload(match(1, "FINISHED", utc="2026-01-10T15:00:00Z", full=(3, 1)), season=PAST)
    wrong = season_payload(match(9, "FINISHED", utc="2025-01-10T15:00:00Z", full=(0, 0)),
                           season={"id": 7, "startDate": "2024-08-15", "endDate": "2025-05-24", "currentMatchday": 38})
    t = RoutedTransport({"/competitions/PL/matches": current, "/competitions/PL/matches?season=2025": old,
                         "/competitions/PL/matches?season=2024": wrong})
    service = make_service(t, clock)
    _hinted(service, "2025-08-15")  # stale hint: points at 2024
    history = run(service.model_history("fd:PL"))
    assert [e.id for e in history.events] == ["fd:1", "fd:2"] and history.previous_season  # 2025, the calendar's
    assert any(u.endswith("season=2025") for u in t.calls)


def test_a_warm_calendar_makes_no_speculative_season_request(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    current = match(2, "FINISHED", utc="2026-09-01T15:00:00Z", full=(1, 0))
    t.script = [t.ok(season_payload(current)), HttpResponse(404, {}, b"")]
    run(service.model_history("fd:PL"))
    _hinted(service, "2027-08-14")  # a newer hint, but the calendar is cached: nothing speculative
    run(service.model_history("fd:PL"))
    assert len(t.calls) == 2


def test_a_cancelled_analysis_leaves_the_season_loads_running_for_the_next_one(clock: FakeClock) -> None:
    import asyncio

    current = season_payload(match(2, "FINISHED", utc="2026-09-01T15:00:00Z", full=(1, 0)))
    old = season_payload(match(1, "FINISHED", utc="2026-01-10T15:00:00Z", full=(3, 1)), season=PAST)
    t = RoutedTransport({"/competitions/PL/matches": current, "/competitions/PL/matches?season=2025": old})

    async def scenario():
        service = make_service(t, clock)
        _hinted(service)
        gate = t.gates["/competitions/PL/matches"] = asyncio.Event()
        with pytest.raises(TimeoutError):  # the evidence deadline ends this waiter only
            await asyncio.wait_for(service.model_history("fd:PL"), 0.05)
        gate.set()
        history = await service.model_history("fd:PL")
        return history, len(t.calls)

    history, calls = run(scenario())
    assert history.previous_season and calls == 2  # finished by the shielded loads, nothing asked twice


def test_the_search_warm_up_also_warms_the_models_previous_season(clock: FakeClock) -> None:
    current = season_payload(match(2, "FINISHED", utc="2026-09-01T15:00:00Z", full=(1, 0)))
    old = season_payload(match(1, "FINISHED", utc="2026-01-10T15:00:00Z", full=(3, 1)), season=PAST)
    t = RoutedTransport({"/competitions/PL/matches": current, "/competitions/PL/matches?season=2025": old})
    service = make_service(t, clock, per_minute=100, per_day=10000)
    service.search_index.warm_past_seasons = True
    run(service._search_warm_load("fd:PL"))
    assert [u.rsplit("/v4", 1)[1] for u in t.calls] == ["/competitions/PL/matches", "/competitions/PL/matches?season=2025"]
    history = run(service.model_history("fd:PL"))  # the AI's evidence: no request at all now
    assert history.previous_season and len(t.calls) == 2


def test_the_warm_up_leaves_seasons_alone_without_the_budget_and_never_marks_them_missing(clock: FakeClock) -> None:
    current = season_payload(match(2, "FINISHED", utc="2026-09-01T15:00:00Z", full=(1, 0)))
    t = RoutedTransport({"/competitions/PL/matches": current})
    service = make_service(t, clock, per_minute=100, per_day=10000)
    service.search_index.warm_past_seasons = False  # small plan: calendars only
    run(service._search_warm_load("fd:PL"))
    assert len(t.calls) == 1 and not service._past_retry_at


def test_a_low_priority_refusal_of_a_season_is_not_remembered_as_missing(clock: FakeClock) -> None:
    from miniapp.sports.client import LOW_PRIORITY_DAY_SHARE

    current = season_payload(match(2, "FINISHED", utc="2026-09-01T15:00:00Z", full=(1, 0)))
    old = season_payload(match(1, "FINISHED", utc="2026-01-10T15:00:00Z", full=(3, 1)), season=PAST)
    t = RoutedTransport({"/competitions/PL/matches": current, "/competitions/PL/matches?season=2025": old})
    service = make_service(t, clock, per_minute=100, per_day=10)
    run(service.calendar("football", "fd:PL"))
    token = LOW_PRIORITY_DAY_SHARE.set(0.05)  # the background share of the day is used up
    try:
        with pytest.raises(UpstreamError):
            run(service._past_season("fd:PL", 2025))
    finally:
        LOW_PRIORITY_DAY_SHARE.reset(token)
    assert not service._past_retry_at
    assert run(service.model_history("fd:PL")).previous_season  # a user's request still loads it


def test_calendar_warm_up_runs_only_where_enabled(clock: FakeClock) -> None:
    import asyncio

    t = RoutedTransport({"/competitions/PL/matches": season_payload(match(2, "FINISHED", full=(1, 0)))})

    async def scenario(enabled: bool):
        service = make_service(t, clock)
        service._warm_calendars = enabled
        return service.ensure_calendars_warm()

    assert run(scenario(False)) is None
    assert asyncio.run(scenario(True)) is not None


def test_a_transient_warm_up_failure_never_pauses_a_users_season_load(clock: FakeClock) -> None:
    current = season_payload(match(2, "FINISHED", utc="2026-09-01T15:00:00Z", full=(1, 0)))
    old = season_payload(match(1, "FINISHED", utc="2026-01-10T15:00:00Z", full=(3, 1)), season=PAST)
    t = RoutedTransport({"/competitions/PL/matches": current, "/competitions/PL/matches?season=2025": HttpResponse(500, {}, b"")})
    service = make_service(t, clock, per_minute=100, per_day=10000)
    service.search_index.warm_past_seasons = True
    with pytest.raises(UpstreamError):
        run(service._search_warm_load("fd:PL"))  # the warm-up skips this competition, remembers nothing
    assert not service._past_retry_at
    # A user right away: the cache still replays the warm-up's failure, but no long pause follows from it.
    assert run(service.model_history("fd:PL")).previous_missing == "transient"
    assert not service._past_retry_at
    t.routes["/competitions/PL/matches?season=2025"] = old
    clock.advance(31)  # past the cache's failure backoff: the source has recovered
    assert run(service.model_history("fd:PL")).previous_season


def test_a_season_the_source_does_not_have_is_remembered_by_the_warm_up_too(clock: FakeClock) -> None:
    current = season_payload(match(2, "FINISHED", utc="2026-09-01T15:00:00Z", full=(1, 0)))
    t = RoutedTransport({"/competitions/PL/matches": current, "/competitions/PL/matches?season=2025": HttpResponse(404, {}, b"")})
    service = make_service(t, clock, per_minute=100, per_day=10000)
    service.search_index.warm_past_seasons = True
    run(service._search_warm_load("fd:PL"))
    assert run(service.model_history("fd:PL")).previous_missing == "absent" and len(t.calls) == 2


def test_a_discarded_speculative_season_leaves_no_unretrieved_exception(clock: FakeClock) -> None:
    import asyncio
    import gc

    current = season_payload(match(2, "FINISHED", utc="2026-09-01T15:00:00Z", full=(1, 0)))
    t = RoutedTransport({"/competitions/PL/matches": current, "/competitions/PL/matches?season=2024": RuntimeError("boom"),
                         "/competitions/PL/matches?season=2025": HttpResponse(404, {}, b"")})
    unretrieved = []

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: unretrieved.append(ctx))
        service = make_service(t, clock)
        _hinted(service, "2025-08-15")  # a wrong guess whose load fails unexpectedly
        await service.model_history("fd:PL")
        await asyncio.sleep(0.05)
        gc.collect()

    run(scenario())
    assert not unretrieved


def test_a_users_own_failure_after_the_warm_up_window_still_pauses_the_season(clock: FakeClock) -> None:
    current = season_payload(match(2, "FINISHED", utc="2026-09-01T15:00:00Z", full=(1, 0)))
    t = RoutedTransport({"/competitions/PL/matches": current, "/competitions/PL/matches?season=2025": HttpResponse(500, {}, b"")})
    service = make_service(t, clock, per_minute=100, per_day=10000)
    service.search_index.warm_past_seasons = True
    with pytest.raises(UpstreamError):
        run(service._search_warm_load("fd:PL"))
    run(service.model_history("fd:PL"))
    run(service.model_history("fd:PL"))  # twice inside the window: still no long pause
    assert not service._past_retry_at
    clock.advance(31)
    assert run(service.model_history("fd:PL")).previous_missing == "transient"  # its own fresh failure
    assert ("PL", 2025) in service._past_retry_at  # the usual pause: never a request per analysis
