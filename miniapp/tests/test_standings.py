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
