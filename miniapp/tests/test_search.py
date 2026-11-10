"""Global football search (M3.4): offline, from stored season calendars."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from miniapp.app import create_app
from miniapp.sports import search as search_module
from miniapp.sports.client import UpstreamError
from miniapp.sports.football_data import normalize_match
from miniapp.sports.search import SearchIndex, normalize
from miniapp.sports.service import build_service
from miniapp.tests.conftest import FakeClock
from miniapp.tests.test_sports import KEY, FakeTransport, base_settings, match, no_sleep, run, signed_in

FETCHED = "2027-01-15T08:00:00Z"
CODES = frozenset({"PL", "PD", "SA", "BL1", "FL1", "CL", "ELC", "DED", "PPL", "BSA"})


def ev(mid: int, home: tuple, away: tuple, *, code: str = "PD", utc: str = "2027-01-20T20:00:00Z",
       status: str = "TIMED", full: tuple | None = None):
    event = normalize_match(match(mid, status, code=code, utc=utc, home=home, away=away, full=full), FETCHED, CODES)
    assert event is not None
    return event


BARCA, REAL, ATLETI, BETIS = (81, "FC Barcelona"), (86, "Real Madrid CF"), (78, "Club Atlético de Madrid"), (90, "Real Betis Balompié")
CITY, UNITED, ARSENAL = (65, "Manchester City FC"), (66, "Manchester United FC"), (57, "Arsenal FC")
# A different club whose name also contains "Barcelona" (another id): must never be merged.
ESPANYOL = (80, "RCD Espanyol de Barcelona")


def index(clock: FakeClock, seasons: dict | None = None) -> SearchIndex:
    ids = [f"fd:{c}" for c in ("PL", "PD", "CL")]
    idx = SearchIndex(lambda tid: None, lambda: ids, clock)
    for tid, events in (seasons or {}).items():
        idx.store(tid, events, clock.now)
    return idx


def seasons():
    return {
        "fd:PD": [
            ev(1, BARCA, REAL, utc="2027-01-25T20:00:00Z"),
            ev(2, ATLETI, BARCA, utc="2027-01-18T20:00:00Z"),
            ev(3, BARCA, BETIS, utc="2027-01-05T20:00:00Z", status="FINISHED", full=(2, 0)),
            ev(4, ESPANYOL, BETIS, utc="2027-01-19T20:00:00Z"),
        ],
        "fd:PL": [ev(5, CITY, UNITED, code="PL"), ev(6, ARSENAL, CITY, code="PL", utc="2027-01-02T15:00:00Z",
                                                     status="FINISHED", full=(1, 1))],
        "fd:CL": [ev(7, BARCA, CITY, code="CL", utc="2027-01-28T20:00:00Z")],
    }


def test_normalize_ignores_case_accents_and_punctuation() -> None:
    assert normalize("Atlético  de Madrid!") == "atletico de madrid"
    assert normalize("BARÇA") == "barca"
    assert normalize("Bodø/Glimt") == "bodo glimt"


@pytest.mark.parametrize("query", ["Barcelona", "barca", "Barça", "BARC", "fc barcelona"])
def test_alternative_names_find_the_same_club_by_id(clock: FakeClock, query: str) -> None:
    result = index(clock, seasons()).search(query)
    assert result.teams[0].team.id == "fd:team:81"


def test_a_namesake_is_a_separate_team_not_merged(clock: FakeClock) -> None:
    result = index(clock, seasons()).search("Barcelona")
    ids = [h.team.id for h in result.teams]
    assert ids[0] == "fd:team:81" and "fd:team:80" in ids  # Espanyol listed on its own, after the exact match
    assert result.teams[0].tournaments == ("fd:CL", "fd:PD")


@pytest.mark.parametrize(("query", "team"), [
    ("Atleti", 78), ("atletico", 78), ("Man City", 65), ("manchester city", 65),
    ("Man United", 66), ("man utd", 66), ("real madrid", 86),
])
def test_common_short_forms(clock: FakeClock, query: str, team: int) -> None:
    assert index(clock, seasons()).search(query).teams[0].team.id == f"fd:team:{team}"


def test_matches_of_found_teams_upcoming_soonest_first_then_recent_results(clock: FakeClock) -> None:
    result = index(clock, seasons()).search("barca")
    ids = [e.id for e in result.events if "fd:team:81" in (e.home.id, e.away.id)]
    assert ids == ["fd:2", "fd:1", "fd:7", "fd:3"]  # 18th, 25th, 28th (other competition), then the played 5th


def test_no_result_is_an_empty_answer(clock: FakeClock) -> None:
    result = index(clock, seasons()).search("zzzz")
    assert result.teams == [] and result.events == []


def test_coverage_says_how_much_is_indexed(clock: FakeClock) -> None:
    partial = index(clock, {"fd:PD": seasons()["fd:PD"]}).search("barca")
    assert partial.indexed == ["fd:PD"] and partial.total == 3 and not partial.complete
    assert index(clock, seasons()).search("barca").complete


def test_team_page_lists_all_its_matches(clock: FakeClock) -> None:
    hit, events = index(clock, seasons()).team("fd:team:65")
    assert hit is not None and hit.team.short_name == "Manchester City"
    assert [e.id for e in events] == ["fd:5", "fd:7", "fd:6"]
    assert index(clock, seasons()).team("fd:team:1")[0] is None


def test_warm_up_loads_missing_calendars_one_by_one_with_pauses_and_survives_errors(clock: FakeClock) -> None:
    loads, sleeps = [], []

    async def load(tid):
        loads.append(tid)
        if tid == "fd:PL":
            raise UpstreamError("rate_limited")
        idx.store(tid, [], clock.now)

    async def sleep(seconds):
        sleeps.append(seconds)

    idx = SearchIndex(load, lambda: ["fd:PL", "fd:PD", "fd:CL"], clock, sleep=sleep)

    async def scenario():
        task = idx.ensure_warm()
        assert idx.ensure_warm() is task  # one warm-up at a time
        await task

    run(scenario())
    assert loads == ["fd:PL", "fd:PD", "fd:CL"] and sleeps == [search_module.WARM_SPACING_S] * 2
    assert idx.indexed() == ["fd:PD", "fd:CL"]


def test_fresh_calendars_are_not_reloaded_and_old_ones_are(clock: FakeClock) -> None:
    loads = []

    async def load(tid):
        loads.append(tid)
        idx.store(tid, [], clock.now)

    idx = SearchIndex(load, lambda: ["fd:PL"], clock, sleep=no_sleep)
    idx.store("fd:PL", [], clock.now)
    assert idx.ensure_warm() is None
    clock.advance(search_module.REFRESH_AFTER_S + 1)

    async def scenario():
        await idx.ensure_warm()

    run(scenario())
    assert loads == ["fd:PL"]


def test_a_cold_search_waits_only_briefly(clock: FakeClock) -> None:
    async def hang(tid):
        await asyncio.sleep(10)

    idx = SearchIndex(hang, lambda: ["fd:PL"], clock)

    async def scenario():
        loop = asyncio.get_running_loop()
        start = loop.time()
        await idx.wait_for_some(timeout_s=0.1)
        waited = loop.time() - start
        idx._warm.cancel()
        return waited

    assert run(scenario()) < 1.0


# --- HTTP API -----------------------------------------------------------------------------

@pytest.fixture()
def api(tmp_path, clock: FakeClock):
    t = FakeTransport()
    settings = base_settings(tmp_path, football_data_key=KEY, session_ttl_s=3600)
    service = build_service(settings, clock=clock, transport=t, warm_emblems=False)
    service.football.client._sleep = no_sleep
    for tid, events in seasons().items():
        service.search_index.store(tid, events, clock.now)
    # Every other served competition counts as indexed-but-empty, so no warm-up runs.
    for tournament in service.tournaments("football"):
        if tournament.id not in seasons():
            service.search_index.store(tournament.id, [], clock.now)
    return TestClient(create_app(settings, clock=clock, sports=service), base_url="http://127.0.0.1:8000"), t


def test_search_api_needs_a_session(api, clock: FakeClock) -> None:
    client, t = api
    assert client.get("/api/sports/football/search?q=barca").status_code == 401
    assert client.get("/api/sports/football/teams/fd:team:81/matches").status_code == 401
    assert t.calls == []


def test_search_api_is_global_and_spends_no_upstream_request(api, clock: FakeClock) -> None:
    client, t = api
    signed_in(client, clock)
    r = client.get("/api/sports/football/search?q=Barça")
    assert r.status_code == 200
    body = r.json()
    assert body["teams"][0]["id"] == "fd:team:81" and body["teams"][0]["name"] == "FC Barcelona"
    assert body["teams"][0]["shortName"] == "Barcelona" and body["teams"][0]["tournaments"] == ["fd:CL", "fd:PD"]
    assert {e["tournamentId"] for e in body["events"]} >= {"fd:PD", "fd:CL"}
    assert body["coverage"]["complete"] is True and body["coverage"]["total"] == 10
    assert t.calls == []


@pytest.mark.parametrize("q", ["", "a", "  ", "!!", "x" * 41])
def test_search_api_rejects_too_short_or_long_queries(api, clock: FakeClock, q: str) -> None:
    client, _ = api
    signed_in(client, clock)
    assert client.get("/api/sports/football/search", params={"q": q}).status_code == 400


def test_search_api_rejects_extra_parameters(api, clock: FakeClock) -> None:
    client, _ = api
    signed_in(client, clock)
    assert client.get("/api/sports/football/search?q=barca&day=2027-01-15").status_code == 400


def test_other_sports_search_nothing_and_call_nothing(api, clock: FakeClock) -> None:
    client, t = api
    signed_in(client, clock)
    body = client.get("/api/sports/tennis/search?q=nadal").json()
    assert body["teams"] == [] and body["events"] == [] and t.calls == []


def test_team_matches_api(api, clock: FakeClock) -> None:
    client, _ = api
    signed_in(client, clock)
    body = client.get("/api/sports/football/teams/fd:team:81/matches").json()
    assert body["team"]["shortName"] == "Barcelona" and [e["id"] for e in body["events"]] == ["fd:2", "fd:1", "fd:7", "fd:3"]
    assert client.get("/api/sports/football/teams/fd:team:1/matches").status_code == 404


def test_old_calendars_are_neither_searched_nor_counted_as_coverage(clock: FakeClock) -> None:
    idx = index(clock, seasons())
    assert idx.search("barca").complete
    clock.advance(search_module.MAX_AGE_S + 1)  # every refresh failed meanwhile
    result = idx.search("barca")
    assert result.indexed == [] and not result.complete and result.teams == [] and result.events == []


def test_an_unknown_team_on_a_partial_index_is_retry_not_not_found(tmp_path, clock: FakeClock) -> None:
    settings = base_settings(tmp_path, football_data_key=KEY, session_ttl_s=3600)
    service = build_service(settings, clock=clock, transport=FakeTransport(), warm_emblems=False)
    service.search_index.store("fd:PL", seasons()["fd:PL"], clock.now)  # only one of ten indexed
    service.search_index.ensure_warm = lambda: None  # no warm-up in this test
    client = TestClient(create_app(settings, clock=clock, sports=service), base_url="http://127.0.0.1:8000")
    signed_in(client, clock)
    r = client.get("/api/sports/football/teams/fd:team:81/matches")
    assert r.status_code == 503 and r.json()["reason"] == "indexing" and r.headers["retry-after"] == "15"


def test_index_keeps_the_stale_mark_of_a_calendar_served_from_cache(tmp_path, clock: FakeClock) -> None:
    from miniapp.sports import football_data
    from miniapp.tests.test_tournaments_crests import season_payload
    t = FakeTransport()
    settings = base_settings(tmp_path, football_data_key=KEY, session_ttl_s=3600)
    service = build_service(settings, clock=clock, transport=t, warm_emblems=False)
    service.football.client._sleep = no_sleep
    t.script = [t.ok(season_payload(match(1, home=BARCA, away=REAL)))]
    run(service.calendar("football", "fd:PL"))
    clock.advance(football_data.CALENDAR_TTL_S + 1)
    t.default = t.ok({"matches": "broken"})  # every refresh fails: the old calendar is served stale
    run(service.calendar("football", "fd:PL"))
    _, events = service.search_index.team("fd:team:81")
    assert events and all(e.provenance.stale for e in events)


def test_a_query_naming_both_sides_finds_the_fixture(clock: FakeClock) -> None:
    result = index(clock, seasons()).search("city united")  # no single team is called "city united"
    assert result.teams == [] and [e.id for e in result.events] == ["fd:5"]
    assert index(clock, seasons()).search("arsenal city").events[0].id == "fd:6"
    assert index(clock, seasons()).search("arsenal betis").events == []  # never played each other here


def test_warm_up_stops_when_its_background_budget_is_spent(clock: FakeClock) -> None:
    loads = []

    async def load(tid):
        loads.append(tid)
        raise UpstreamError("rate_limited", low_priority_only=True)

    async def sleep(seconds):
        pass

    idx = SearchIndex(load, lambda: ["fd:PL", "fd:PD", "fd:CL"], clock, sleep=sleep)

    async def scenario():
        await idx.ensure_warm()

    run(scenario())
    assert loads == ["fd:PL"]  # no spinning through the rest: the next search starts a new pass
