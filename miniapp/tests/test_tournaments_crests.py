"""M3.3: short team names, crests served from our origin, tournament calendars.

Offline only: the provider and the image host are fakes.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from miniapp.app import create_app
from miniapp.sports import football_data
from miniapp.sports.client import UpstreamError
from miniapp.sports.crests import PNG_MAGIC, CrestStore, crest_source
from miniapp.sports.football_data import normalize_match, short_name
from miniapp.sports.service import build_service
from miniapp.tests.conftest import FakeClock
from miniapp.sports.client import TransportError
from miniapp.tests.test_sports import (
    ALLOWED, DAY, FETCHED, KEY, FakeTransport, base_settings, day_payload, make_service, match, no_sleep, run,
    signed_in,
)

PNG = PNG_MAGIC + b"\x00" * 64
CREST = "https://crests.football-data.org/57.png"


def with_crests(m: dict, home: str | None = CREST, away: str | None = "https://crests.football-data.org/61.png",
                emblem: str | None = "https://crests.football-data.org/PL.png") -> dict:
    m["homeTeam"]["crest"], m["awayTeam"]["crest"] = home, away
    m["competition"]["emblem"] = emblem
    return m


SEASON = {"id": 1, "startDate": "2026-08-21", "endDate": "2027-05-30", "currentMatchday": 21}


def season_payload(*matches: dict, code: str = "PL", season: dict | None = None) -> dict:
    for m in matches:
        if isinstance(m, dict):
            m["season"] = dict(season or SEASON)
    return {"filters": {"season": "2026"}, "resultSet": {"count": len(matches)},
            "competition": {"id": 2021, "code": code, "name": "Premier League",
                            "emblem": "https://crests.football-data.org/PL.png"},
            "matches": list(matches)}


class FakeImages:
    def __init__(self, body: bytes | None = PNG) -> None:
        self.body = body
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, url: str, timeout_s: float) -> bytes | None:
        self.calls.append(url)
        if self.gate is not None:
            await self.gate.wait()
        return self.body


# --- short names --------------------------------------------------------------------

def test_display_names_are_keyed_by_team_id_not_by_name() -> None:
    assert short_name(81, "FC Barcelona", "Barça") == "Barcelona"
    assert short_name(78, "Club Atlético de Madrid", "Atleti") == "Atlético Madrid"
    assert short_name(65, "Manchester City FC", "Man City") == "Manchester City"
    assert short_name(66, "Manchester United FC", "Man United") == "Manchester United"
    assert short_name(90, "Real Betis Balompié", "Real Betis") == "Real Betis"   # the provider's own form
    # Another club whose name happens to match is not renamed: only the id decides.
    assert short_name(999999, "FC Barcelona", "Barcelona SC") == "Barcelona SC"
    assert short_name(1, "Some Club", None) == "1. FC Köln"                       # id 1 is Köln
    assert short_name(424242, "Some Club", None) is None
    assert short_name(424242, "Some Club", "x" * 29) is None  # not a short name: the full name is shown


def test_display_names_are_readable() -> None:
    from miniapp.sports.football_data import DISPLAY_NAMES
    assert all(2 < len(v) <= 28 and "." not in v.replace("1. FC", "") for v in DISPLAY_NAMES.values())


def test_normalized_teams_carry_the_short_name_and_keep_the_full_one() -> None:
    event = normalize_match(match(1, home=(397, "Brighton & Hove Albion FC")), FETCHED, ALLOWED)
    assert event is not None
    assert event.home.to_json()["name"] == "Brighton & Hove Albion FC"
    assert event.home.to_json()["shortName"] == "Brighton"
    assert event.away.to_json()["shortName"] == "Chelsea"


# --- crest URLs ---------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://crests.football-data.org/57.png",          # not HTTPS
    "https://crests.football-data.org.evil.com/57.png",
    "https://evil.com/crests.football-data.org/57.png",
    "https://crests.football-data.org:8443/57.png",
    "https://user@crests.football-data.org/57.png",
    "https://crests.football-data.org/57.svg",         # SVG could carry script on our origin
    "https://crests.football-data.org/57.png?x=1",
    "https://crests.football-data.org/57.png#a",
    "https://crests.football-data.org/a/../57.png",
    "https://crests.football-data.org/%2e%2e.png",
    "https://crests.football-data.org:bad/PL.png",     # malformed port: refused, never an exception
    "", None, 57, "https://crests.football-data.org/" + "a" * 300 + ".png",
])
def test_only_https_png_from_the_provider_crest_host_is_accepted(url: object) -> None:
    assert crest_source(url) is None


def test_a_provider_crest_becomes_our_own_path_never_the_third_party_url() -> None:
    event = normalize_match(with_crests(match(1)), FETCHED, ALLOWED)
    assert event is not None
    assert crest_source(CREST) == CREST
    assert event.home.to_json()["crest"] == "/api/crests/team/57"
    assert "crests.football-data.org" not in str(event.to_json())
    foreign = normalize_match(with_crests(match(2), home="https://evil.com/57.png"), FETCHED, ALLOWED)
    assert foreign is not None and foreign.home.to_json()["crest"] is None


# --- crest store --------------------------------------------------------------------

def test_crest_image_is_fetched_once_and_then_served_from_memory(clock: FakeClock) -> None:
    images = FakeImages()
    store = CrestStore(images, clock=clock)
    assert store.learn("team", "57", CREST)

    async def scenario():
        return [await store.image("team", "57") for _ in range(3)]

    assert run(scenario()) == [PNG, PNG, PNG]
    assert images.calls == [CREST]


def test_unknown_crest_costs_no_fetch(clock: FakeClock) -> None:
    images = FakeImages()
    store = CrestStore(images, clock=clock)
    assert not store.learn("team", "1", "https://evil.com/1.png")
    assert run(store.image("team", "1")) is None and run(store.image("team", "2")) is None
    assert images.calls == [] and store.path("team", "1") is None


def test_a_non_png_answer_is_refused_and_not_refetched_for_an_hour(clock: FakeClock) -> None:
    images = FakeImages(b"<svg onload=alert(1)>")
    store = CrestStore(images, clock=clock)
    store.learn("team", "57", CREST)
    assert run(store.image("team", "57")) is None
    assert run(store.image("team", "57")) is None
    assert len(images.calls) == 1
    clock.advance(3601)
    images.body = PNG
    assert run(store.image("team", "57")) == PNG


def test_oversized_image_is_refused(clock: FakeClock) -> None:
    store = CrestStore(FakeImages(PNG_MAGIC + b"\x00" * 400_000), clock=clock)
    store.learn("team", "57", CREST)
    assert run(store.image("team", "57")) is None


def test_concurrent_requests_for_one_crest_share_one_fetch(clock: FakeClock) -> None:
    images = FakeImages()
    store = CrestStore(images, clock=clock)
    store.learn("team", "57", CREST)

    async def scenario():
        images.gate = asyncio.Event()
        tasks = [asyncio.create_task(store.image("team", "57")) for _ in range(5)]
        await asyncio.sleep(0)
        images.gate.set()
        return await asyncio.gather(*tasks)

    assert run(scenario()) == [PNG] * 5
    assert images.calls == [CREST]


def test_crest_fetches_are_capped_per_minute_without_remembering_a_failure(clock: FakeClock) -> None:
    images = FakeImages()
    store = CrestStore(images, clock=clock, per_minute=2)
    for n in range(1, 4):
        store.learn("team", str(n), f"https://crests.football-data.org/{n}.png")
    assert [run(store.image("team", str(n))) for n in range(1, 4)] == [PNG, PNG, None]
    clock.advance(61)
    assert run(store.image("team", "3")) == PNG  # the cap was not a remembered failure


def test_image_cache_is_bounded_by_bytes(clock: FakeClock) -> None:
    store = CrestStore(FakeImages(), clock=clock, max_bytes=len(PNG) * 2)
    for n in range(1, 5):
        store.learn("team", str(n), f"https://crests.football-data.org/{n}.png")
        run(store.image("team", str(n)))
    assert len(store._images) == 2 and store._image_bytes <= len(PNG) * 2


# --- calendar -----------------------------------------------------------------------

def test_calendar_is_the_whole_season_in_one_request(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    late = with_crests(match(2, utc="2027-02-01T15:00:00Z"))
    early = with_crests(match(1, "FINISHED", utc="2026-08-22T15:00:00Z", full=(2, 1)))
    early["matchday"], late["matchday"] = 1, 24
    t.script = [t.ok(season_payload(late, early))]
    result = run(service.calendar("football", "fd:PL"))
    assert result is not None
    assert t.calls[0][0].endswith("/competitions/PL/matches")
    assert [e.id for e in result.events] == ["fd:1", "fd:2"]
    assert [e.to_json()["matchday"] for e in result.events] == [1, 24]
    assert result.season.to_json() == {"startDate": "2026-08-21", "endDate": "2027-05-30", "currentMatchday": 21}
    assert service.tournament_json(result.tournament)["emblem"] == "/api/crests/competition/PL"
    assert service.crests.path("team", "57") == "/api/crests/team/57"
    # Cached and shared.
    assert run(service.calendar("football", "fd:PL")) is not None and len(t.calls) == 1


def test_a_match_opened_from_a_calendar_comes_from_that_calendar_not_per_match_requests(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)  # unknown-id budget: 2 per hour
    matches = [match(n, utc=f"2027-02-0{n}T15:00:00Z") for n in range(1, 6)]
    t.script = [t.ok(season_payload(*matches))]
    run(service.calendar("football", "fd:PL"))
    for n in range(1, 6):  # more than the budget, yet no per-match request at all
        found, tournament = run(service.event(f"fd:{n}"))
        assert found is not None and found.id == f"fd:{n}" and tournament is not None and tournament.id == "fd:PL"
    assert len(t.calls) == 1 and not service._unknown_lookups
    # After the calendar expires, one shared calendar request refreshes it — still no per-match calls.
    clock.advance(football_data.CALENDAR_TTL_S + 1)
    t.script = [t.ok(season_payload(*[match(n, utc=f"2027-02-0{n}T15:00:00Z") for n in range(1, 6)]))]
    assert run(service.event("fd:3"))[0] is not None
    assert len(t.calls) == 2 and t.calls[1][0].endswith("/competitions/PL/matches")


def test_ids_from_a_calendar_do_not_bypass_the_unknown_id_budget_elsewhere(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    t.script = [t.ok(season_payload(match(1)))]
    run(service.calendar("football", "fd:PL"))
    assert "fd:1" not in service._known_ids
    # A match the calendar does not list is an ordinary unknown id: budgeted.
    t.script = [t.ok(match(77)), t.ok(match(78))]
    run(service.event("fd:77")), run(service.event("fd:78"))
    with pytest.raises(UpstreamError) as exc:
        run(service.event("fd:79"))
    assert exc.value.code == "rate_limited"


def test_calendar_skips_fixtures_whose_teams_are_not_known_yet(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    tbd = match(3)
    tbd["homeTeam"] = {"id": None, "name": None, "shortName": None, "tla": None, "crest": None}
    t.script = [t.ok(season_payload(match(1), tbd))]
    result = run(service.calendar("football", "fd:PL"))
    assert result is not None and [e.id for e in result.events] == ["fd:1"]


def tbd(m: dict, **over) -> dict:
    m["homeTeam"] = {"id": None, "name": None}
    return {**m, **over}


@pytest.mark.parametrize("broken", [
    lambda: match(1, code="PD"),                      # another competition in this answer
    lambda: {**match(1), "utcDate": "soon"},           # unreadable match
    lambda: "not a match",
    lambda: {"homeTeam": {"id": None, "name": None}},  # truncated record, not a TBD fixture
    lambda: tbd(match(1, "FINISHED", full=(1, 0))),     # a played match always has its teams
    lambda: tbd(match(1), utcDate=None),
    lambda: tbd(match(1), id=None),
    lambda: {**tbd(match(1)), "awayTeam": {"id": 5}},  # the known side itself is broken
    lambda: tbd(match(1, code="PD")),                   # a TBD fixture of another competition
    lambda: {**match(1), "homeTeam": {}},               # an empty side is broken, not "open"
    lambda: {**match(1), "homeTeam": {"id": None, "name": []}},
])
def test_a_calendar_with_a_broken_match_is_an_error_not_a_silent_gap(clock: FakeClock, broken) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    t.script = [t.ok(season_payload(match(2), broken()))]
    with pytest.raises(UpstreamError) as exc:
        run(service.calendar("football", "fd:PL"))
    assert exc.value.code == "bad_response"


def test_calendar_refreshes_faster_while_a_match_is_live(clock: FakeClock) -> None:
    t = FakeTransport()
    adapter = make_service(t, clock).football
    t.script = [t.ok(season_payload(match(1, "IN_PLAY", full=(0, 0)))), t.ok(season_payload(match(1)))]
    assert run(adapter.calendar("PL"))[1] == football_data.CALENDAR_LIVE_TTL_S
    assert run(adapter.calendar("PL"))[1] == football_data.CALENDAR_TTL_S


def test_calendar_of_an_unknown_tournament_costs_nothing(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    assert run(service.calendar("football", "fd:SA")) is None  # not configured in this service
    assert run(service.calendar("tennis", "fd:PL")) is None
    with pytest.raises(UpstreamError):
        run(service.football.calendar("SA"))
    assert t.calls == []


def test_day_listing_skips_undetermined_fixtures_instead_of_failing(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    tbd = match(3)
    tbd["awayTeam"] = {"id": None, "name": None}
    t.script = [t.ok(day_payload(match(1), tbd))]
    events, _ = run(service.football.day(football_data.date.fromisoformat(DAY)))
    assert [e.id for e in events] == ["fd:1"]


# --- HTTP API -----------------------------------------------------------------------

@pytest.fixture()
def api(tmp_path, clock: FakeClock):
    t = FakeTransport()
    images = FakeImages()
    settings = base_settings(tmp_path, football_data_key=KEY, session_ttl_s=3600)
    service = build_service(settings, clock=clock, transport=t, crest_fetch=images, warm_emblems=False)
    service.football.client._sleep = no_sleep
    app = create_app(settings, clock=clock, sports=service)
    return TestClient(app, base_url="http://127.0.0.1:8000"), t, images


def test_calendar_requires_a_session_and_spends_nothing_otherwise(api, clock: FakeClock) -> None:
    client, t, images = api
    r = client.get("/api/sports/football/tournaments/fd:PL/calendar")
    assert r.status_code == 401 and r.headers["cache-control"] == "no-store"
    assert t.calls == [] and images.calls == []


def test_crests_need_no_session_but_only_ever_serve_what_the_provider_listed(api, clock: FakeClock) -> None:
    client, t, images = api
    # Without a session: an unknown crest is a cheap 404, no fetch, no session lookup.
    for path in ("/api/crests/team/57", "/api/crests/competition/PL"):
        r = client.get(path)
        assert r.status_code == 404 and r.headers["cache-control"] == "no-store", path
    assert images.calls == []
    signed_in(client, clock)
    t.script = [t.ok(season_payload(with_crests(match(1))))]
    client.get("/api/sports/football/tournaments/fd:PL/calendar")
    client.cookies.clear()
    r = client.get("/api/crests/team/57")
    assert r.status_code == 200 and r.content == PNG and "set-cookie" not in r.headers


def test_calendar_api(api, clock: FakeClock) -> None:
    client, t, _ = api
    signed_in(client, clock)
    t.script = [t.ok(season_payload(with_crests(match(1))))]
    r = client.get("/api/sports/football/tournaments/fd:PL/calendar")
    assert r.status_code == 200
    body = r.json()
    assert body["tournament"] == {"id": "fd:PL", "sport": "football", "name": "Premier League",
                                  "area": "England", "provider": "football-data.org",
                                  "emblem": "/api/crests/competition/PL", "featured": True}
    assert body["season"]["currentMatchday"] == 21 and body["stale"] is False
    assert body["events"][0]["home"] == {"id": "fd:team:57", "name": "Arsenal FC", "shortName": "Arsenal",
                                         "crest": "/api/crests/team/57"}
    assert body["coverage"]["attribution"] == football_data.ATTRIBUTION
    assert KEY not in r.text and "crests.football-data.org" not in r.text
    # The tournament list now offers the learned emblem too.
    listed = client.get("/api/sports/football/tournaments").json()["tournaments"]
    assert next(x for x in listed if x["id"] == "fd:PL")["emblem"] == "/api/crests/competition/PL"
    assert next(x for x in listed if x["id"] == "fd:SA")["emblem"] is None


def test_calendar_api_rejects_bad_requests_without_upstream_calls(api, clock: FakeClock) -> None:
    client, t, _ = api
    signed_in(client, clock)
    assert client.get("/api/sports/football/tournaments/fd:XX/calendar").status_code == 404
    assert client.get("/api/sports/cricket/tournaments/fd:PL/calendar").status_code == 404
    assert client.get("/api/sports/football/tournaments/fd:PL/calendar?from=x").status_code == 400
    assert client.get(f"/api/sports/football/tournaments/{'x' * 41}/calendar").status_code == 404
    assert t.calls == []


def test_calendar_api_reports_an_upstream_failure(api, clock: FakeClock) -> None:
    client, t, _ = api
    signed_in(client, clock)
    t.script = [t.ok({"matches": "broken"})]
    r = client.get("/api/sports/football/tournaments/fd:PL/calendar")
    assert r.status_code == 503 and r.json() == {"error": "source_unavailable", "reason": "bad_response"}


def test_crest_route_serves_a_verified_png_with_a_private_cache(api, clock: FakeClock) -> None:
    client, t, images = api
    signed_in(client, clock)
    assert client.get("/api/crests/team/57").status_code == 404  # not learned yet: no fetch
    assert images.calls == []
    t.script = [t.ok(season_payload(with_crests(match(1))))]
    client.get("/api/sports/football/tournaments/fd:PL/calendar")
    r = client.get("/api/crests/team/57")
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "public, max-age=86400"  # no user data: edge-cacheable
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/crests/competition/PL").content == PNG
    assert images.calls == [CREST, "https://crests.football-data.org/PL.png"]


@pytest.mark.parametrize("path", [
    "/api/crests/team/057", "/api/crests/team/abc", "/api/crests/team/1234567890123",
    "/api/crests/player/57", "/api/crests/competition/P-L",
])
def test_crest_route_rejects_malformed_ids(api, clock: FakeClock, path: str) -> None:
    client, _, images = api
    signed_in(client, clock)
    r = client.get(path)
    assert r.status_code == 404 and r.headers["cache-control"] == "no-store"
    assert images.calls == []


def test_crest_route_has_a_global_per_second_cap_that_junk_cannot_exhaust(tmp_path, clock: FakeClock) -> None:
    settings = base_settings(tmp_path, football_data_key=KEY, session_ttl_s=3600)
    service = build_service(settings, clock=clock, transport=FakeTransport(), crest_fetch=FakeImages(),
                            warm_emblems=False)
    service.crests._per_second = 3
    service.crests.learn("team", "57", CREST)
    client = TestClient(create_app(settings, clock=clock, sports=service), base_url="http://127.0.0.1:8000")
    # Unknown ids, malformed ids and cache-busting query strings: cheap 404s, not counted.
    for path in ["/api/crests/team/1", "/api/crests/team/x", "/api/crests/team/57?v=1"] * 5:
        assert client.get(path).status_code == 404
    codes = [client.get("/api/crests/team/57").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    clock.advance(1.01)
    assert client.get("/api/crests/team/57").status_code == 200


def test_a_crest_fetch_waits_for_a_slot_only_briefly(clock: FakeClock, monkeypatch) -> None:
    from miniapp.sports import crests as crests_module
    monkeypatch.setattr(crests_module, "QUEUE_WAIT_S", 0.05)
    images = FakeImages()
    store = CrestStore(images, clock=clock)
    for n in (1, 2):
        store.learn("team", str(n), f"https://crests.football-data.org/{n}.png")

    async def scenario():
        for _ in range(crests_module.CREST_CONCURRENCY):
            await store._gate.acquire()  # every slot busy
        busy = await store.image("team", "1")
        for _ in range(crests_module.CREST_CONCURRENCY):
            store._gate.release()
        return busy, await store.image("team", "1")

    assert run(scenario()) == (None, PNG)  # the busy refusal was not remembered as a failure


def test_a_fixture_with_both_sides_still_open_is_skipped_not_broken(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    final = tbd(match(9))
    final["awayTeam"] = {"id": None, "name": None}
    t.script = [t.ok(season_payload(match(1), final))]
    assert [e.id for e in run(service.calendar("football", "fd:PL")).events] == ["fd:1"]


@pytest.mark.parametrize("season", [
    {"startDate": "garbage", "endDate": None, "currentMatchday": 1},
    {"startDate": None, "endDate": "2027-13-40", "currentMatchday": 1},
    {"startDate": None, "endDate": None, "currentMatchday": "10"},
    {"startDate": None, "endDate": None, "currentMatchday": 0},
])
def test_unreadable_season_facts_make_the_calendar_broken(clock: FakeClock, season: dict) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    t.script = [t.ok(season_payload(match(1), season=season))]
    with pytest.raises(UpstreamError) as exc:
        run(service.calendar("football", "fd:PL"))
    assert exc.value.code == "bad_response"


def test_a_season_that_is_not_an_object_or_broken_on_a_later_match_is_broken(clock: FakeClock) -> None:
    for second in ("bad", {"startDate": "x"}):
        t = FakeTransport()
        service = make_service(t, clock)
        payload = season_payload(match(1), match(2))
        payload["matches"][1]["season"] = second
        t.script = [t.ok(payload)]
        with pytest.raises(UpstreamError):
            run(service.calendar("football", "fd:PL"))


def test_a_calendar_answer_about_another_competition_is_broken_even_when_empty(clock: FakeClock) -> None:
    for payload in (season_payload(code="PD"), {"matches": [], "resultSet": {"count": 0}}):
        t = FakeTransport()
        service = make_service(t, clock)
        t.script = [t.ok(payload)]
        with pytest.raises(UpstreamError) as exc:
            run(service.calendar("football", "fd:PL"))
        assert exc.value.code == "bad_response"


def test_two_records_under_one_match_id_are_broken(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    t.script = [t.ok(season_payload(match(1), match(1, utc="2027-02-01T15:00:00Z")))]
    with pytest.raises(UpstreamError):
        run(service.calendar("football", "fd:PL"))


@pytest.mark.parametrize("order", ["open_first", "match_first", "two_open"])
def test_an_open_fixture_sharing_an_id_is_broken(clock: FakeClock, order: str) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    normal, open_ = match(1), tbd(match(1))
    pair = {"open_first": (open_, normal), "match_first": (normal, open_), "two_open": (open_, tbd(match(1)))}[order]
    t.script = [t.ok(season_payload(*pair))]
    with pytest.raises(UpstreamError):
        run(service.calendar("football", "fd:PL"))


def test_a_failed_download_of_a_moved_crest_does_not_block_the_new_url(clock: FakeClock) -> None:
    images = FakeImages(None)
    store = CrestStore(images, clock=clock)
    store.learn("team", "57", CREST)

    async def scenario():
        images.gate = asyncio.Event()
        task = asyncio.create_task(store.image("team", "57"))
        await asyncio.sleep(0)
        store.learn("team", "57", "https://crests.football-data.org/57-new.png")
        images.gate.set()
        return await task

    assert run(scenario()) is None
    images.gate, images.body = None, PNG
    assert run(store.image("team", "57")) == PNG  # not blocked by the old URL's failure


def test_a_crest_moved_during_its_download_is_not_served(clock: FakeClock) -> None:
    images = FakeImages()
    store = CrestStore(images, clock=clock)
    store.learn("team", "57", CREST)

    async def scenario():
        images.gate = asyncio.Event()
        task = asyncio.create_task(store.image("team", "57"))
        await asyncio.sleep(0)
        store.learn("team", "57", "https://crests.football-data.org/57-new.png")
        images.gate.set()
        return await task

    assert run(scenario()) is None
    images.gate = None
    assert run(store.image("team", "57")) == PNG and images.calls[-1].endswith("57-new.png")


def test_a_null_season_on_a_match_is_broken(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    payload = season_payload(match(1), match(2))
    payload["matches"][1]["season"] = None
    t.script = [t.ok(payload)]
    with pytest.raises(UpstreamError):
        run(service.calendar("football", "fd:PL"))


def test_a_truncated_competition_list_is_broken_and_retried_soon(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    for bad in ([42], [{"emblem": "https://crests.football-data.org/PL.png"}]):
        t.script = [t.ok({"competitions": bad})]
        with pytest.raises(UpstreamError):
            run(service.football.competition_emblems())
    t.script = [t.ok({"competitions": [42]})]
    run(service.ensure_emblems())
    assert service._emblems_retry_at == clock.now + football_data.EMBLEMS_RETRY_S  # not the 24-hour gate


def test_a_slow_emblem_list_never_holds_a_page_for_long(clock: FakeClock) -> None:
    service = make_service(FakeTransport(), clock)
    calls = []

    async def slow():
        calls.append(1)
        await asyncio.sleep(5)
        return {}, 60.0

    service.football.competition_emblems = slow

    async def scenario():
        loop = asyncio.get_running_loop()
        started = loop.time()
        await service.ensure_emblems(wait_s=0.05)
        await service.ensure_emblems(wait_s=0.05)  # joins the same background request
        waited = loop.time() - started
        service._emblems_task.cancel()
        return waited

    assert run(scenario()) < 1.0 and calls == [1]


def test_missing_season_facts_stay_unknown(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    t.script = [t.ok(season_payload(match(1), season={"id": 1}))]
    assert run(service.calendar("football", "fd:PL")).season.to_json() == {
        "startDate": None, "endDate": None, "currentMatchday": None}


def competitions_payload() -> dict:
    return {"competitions": [
        {"code": "PL", "emblem": "https://crests.football-data.org/PL.png"},
        {"code": "SA", "emblem": "https://crests.football-data.org/c111.png"},
        {"code": "CLI", "emblem": "https://crests.football-data.org/CLI.png"},   # not served
        {"code": "BL1", "emblem": "https://evil.com/BL1.png"},                   # foreign host
    ]}


@pytest.fixture()
def warm_api(tmp_path, clock: FakeClock):
    t = FakeTransport()
    settings = base_settings(tmp_path, football_data_key=KEY, session_ttl_s=3600)
    service = build_service(settings, clock=clock, transport=t, crest_fetch=FakeImages())
    service.football.client._sleep = no_sleep
    return TestClient(create_app(settings, clock=clock, sports=service), base_url="http://127.0.0.1:8000"), t


def test_a_cold_tournament_list_has_emblems_from_one_daily_request(warm_api, clock: FakeClock) -> None:
    client, t = warm_api
    signed_in(client, clock)
    t.script = [t.ok(competitions_payload())]
    listed = {x["id"]: x["emblem"] for x in client.get("/api/sports/football/tournaments").json()["tournaments"]}
    assert listed["fd:PL"] == "/api/crests/competition/PL" and listed["fd:SA"] == "/api/crests/competition/SA"
    assert listed["fd:BL1"] is None  # a URL from another host is never offered
    assert "fd:CLI" not in listed
    assert [c[0].rsplit("/", 1)[-1] for c in t.calls] == ["competitions"]
    client.get("/api/sports/football/tournaments")
    clock.advance(3600)
    client.get("/api/sports/football/tournaments")
    assert len(t.calls) == 1  # not asked again the same day, although some codes have no emblem


def test_emblems_are_refreshed_daily_even_when_all_are_known(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)  # serves PL and PD
    both = {"competitions": [{"code": "PL", "emblem": "https://crests.football-data.org/PL.png"},
                             {"code": "PD", "emblem": "https://crests.football-data.org/PD.png"}]}
    t.script = [t.ok(both)]
    run(service.ensure_emblems())
    clock.advance(football_data.EMBLEMS_TTL_S + 1)
    moved = {"competitions": [{"code": "PL", "emblem": "https://crests.football-data.org/PL-2027.png"},
                              {"code": "PD", "emblem": "https://crests.football-data.org/PD.png"}]}
    t.script = [t.ok(moved)]
    run(service.ensure_emblems())
    assert len(t.calls) == 2 and service.football.emblems["PL"].endswith("PL-2027.png")
    service.tournament_json(service.tournaments("football")[0])
    assert service.crests._urls["competition:PL"].endswith("PL-2027.png")


def test_an_emblem_failure_never_breaks_the_list_and_is_retried_later(warm_api, clock: FakeClock) -> None:
    client, t = warm_api
    signed_in(client, clock)
    t.script = [t.ok({"competitions": "broken"})]
    r = client.get("/api/sports/football/tournaments")
    assert r.status_code == 200 and all(x["emblem"] is None for x in r.json()["tournaments"])
    client.get("/api/sports/football/tournaments")
    assert len(t.calls) == 1  # no retry storm
    clock.advance(football_data.EMBLEMS_RETRY_S + 1)
    t.script = [t.ok(competitions_payload())]
    listed = client.get("/api/sports/football/tournaments").json()["tournaments"]
    assert next(x for x in listed if x["id"] == "fd:PL")["emblem"] == "/api/crests/competition/PL"


def test_events_listing_is_served_before_emblems_are_asked(warm_api, clock: FakeClock) -> None:
    client, t = warm_api
    signed_in(client, clock)
    t.script = [t.ok(day_payload(match(1)))]
    t.default = TransportError("timeout")  # the emblem list times out (with its retries)
    r = client.get(f"/api/sports/football/events?from={DAY}T00:00:00Z&to=2027-01-16T00:00:00Z")
    assert r.status_code == 200 and [e["id"] for e in r.json()["events"]] == ["fd:1"]
    assert "/matches" in t.calls[0][0] and t.calls[1][0].endswith("/competitions")


@pytest.mark.parametrize("count", [380, "1", True, -1, None, "missing", "no_result_set"])
def test_a_season_shorter_than_the_provider_says_is_broken(clock: FakeClock, count) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    payload = season_payload(match(1))
    if count == "no_result_set":
        del payload["resultSet"]
    elif count == "missing":
        del payload["resultSet"]["count"]
    else:
        payload["resultSet"]["count"] = count
    t.script = [t.ok(payload)]
    with pytest.raises(UpstreamError):
        run(service.calendar("football", "fd:PL"))


def test_a_fresh_calendar_answers_even_ids_known_from_a_day_listing(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    t.script = [t.ok(day_payload(match(1)))]
    run(service._football_day(football_data.date.fromisoformat(DAY)))
    t.script = [t.ok(season_payload(match(1)))]
    run(service.calendar("football", "fd:PL"))
    found, _ = run(service.event("fd:1"))
    assert found is not None and len(t.calls) == 2  # no /matches/1 request
    # Once the calendar expires, a known id goes back to its own (fresher) match request.
    clock.advance(football_data.CALENDAR_TTL_S + 1)
    t.script = [t.ok(match(1))]
    run(service.event("fd:1"))
    assert t.calls[-1][0].endswith("/matches/1")


def test_an_emblem_the_provider_withdraws_is_no_longer_offered(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    t.script = [t.ok({"competitions": [{"code": "PL", "emblem": "https://crests.football-data.org/PL.png"}]})]
    run(service.ensure_emblems())
    pl = next(x for x in service.tournaments("football") if x.id == "fd:PL")
    assert service.tournament_json(pl)["emblem"] == "/api/crests/competition/PL"
    clock.advance(football_data.EMBLEMS_TTL_S + 1)
    t.script = [t.ok({"competitions": [{"code": "PL", "emblem": None}]})]
    run(service.ensure_emblems())
    assert service.tournament_json(pl)["emblem"] is None
    assert service.crests.path("competition", "PL") is None


def test_rejected_crest_requests_have_their_own_cap(tmp_path, clock: FakeClock) -> None:
    settings = base_settings(tmp_path, football_data_key=KEY, session_ttl_s=3600)
    service = build_service(settings, clock=clock, transport=FakeTransport(), crest_fetch=FakeImages(),
                            warm_emblems=False)
    service.crests._rejected_per_second = 2
    service.crests.learn("team", "57", CREST)
    client = TestClient(create_app(settings, clock=clock, sports=service), base_url="http://127.0.0.1:8000")
    codes = [client.get(f"/api/crests/team/{n}").status_code for n in (1, 2, 3)]
    assert codes == [404, 404, 429]
    assert client.get("/api/crests/team/57").status_code == 200  # real crests keep their own budget


def test_a_competition_omitted_from_the_daily_list_loses_its_emblem(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    t.script = [t.ok({"competitions": [{"code": "PL", "emblem": "https://crests.football-data.org/PL.png"}]})]
    run(service.ensure_emblems())
    clock.advance(football_data.EMBLEMS_TTL_S + 1)
    t.script = [t.ok({"competitions": []})]
    run(service.ensure_emblems())
    pl = next(x for x in service.tournaments("football") if x.id == "fd:PL")
    assert service.tournament_json(pl)["emblem"] is None


@pytest.mark.parametrize("path", ["/api/crests/team/57/extra", "/api/crests/x/y/z?nonce=1", "/api/crests/"])
def test_other_paths_under_the_crest_prefix_share_the_rejected_cap(tmp_path, clock: FakeClock, path: str) -> None:
    settings = base_settings(tmp_path, football_data_key=KEY, session_ttl_s=3600)
    service = build_service(settings, clock=clock, transport=FakeTransport(), crest_fetch=FakeImages(),
                            warm_emblems=False)
    service.crests._rejected_per_second = 1
    client = TestClient(create_app(settings, clock=clock, sports=service), base_url="http://127.0.0.1:8000")
    assert [client.get(path).status_code for _ in range(2)] == [404, 429]


def test_head_requests_share_the_crest_caps(tmp_path, clock: FakeClock) -> None:
    settings = base_settings(tmp_path, football_data_key=KEY, session_ttl_s=3600)
    service = build_service(settings, clock=clock, transport=FakeTransport(), crest_fetch=FakeImages(),
                            warm_emblems=False)
    service.crests._rejected_per_second = 1
    service.crests.learn("team", "57", CREST)
    client = TestClient(create_app(settings, clock=clock, sports=service), base_url="http://127.0.0.1:8000")
    assert client.head("/api/crests/team/57").status_code == 200
    assert [client.head("/api/crests/team/57?nonce=1").status_code for _ in range(2)] == [404, 429]


def test_a_withdrawn_emblem_does_not_come_back_from_match_payloads(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    t.script = [t.ok({"competitions": [{"code": "PL", "emblem": None}]})]
    run(service.ensure_emblems())
    t.script = [t.ok(season_payload(with_crests(match(1))))]
    run(service.calendar("football", "fd:PL"))
    assert "PL" not in service.football.emblems


def test_a_broken_competition_list_changes_nothing(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    t.script = [t.ok({"competitions": [{"code": "PL", "emblem": "https://crests.football-data.org/PL.png"}]})]
    run(service.football.competition_emblems())
    t.script = [t.ok({"competitions": [{"code": "PL", "emblem": None}, 42]})]
    with pytest.raises(UpstreamError):
        run(service.football.competition_emblems())
    assert service.football.emblems["PL"].endswith("PL.png")


@pytest.mark.parametrize("other", [
    {"id": 2, "startDate": "2026-08-21", "endDate": "2027-05-30", "currentMatchday": 21},   # another season id
    {"id": 1, "startDate": "2025-08-15", "endDate": "2026-05-24", "currentMatchday": 38},   # other facts
])
def test_a_calendar_mixing_seasons_is_broken(clock: FakeClock, other: dict) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    payload = season_payload(match(1), match(2))
    payload["matches"][1]["season"] = other
    t.script = [t.ok(payload)]
    with pytest.raises(UpstreamError):
        run(service.calendar("football", "fd:PL"))


def test_a_match_without_a_season_among_matches_with_one_is_broken(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    payload = season_payload(match(1), match(2))
    del payload["matches"][1]["season"]
    t.script = [t.ok(payload)]
    with pytest.raises(UpstreamError):
        run(service.calendar("football", "fd:PL"))


def test_failure_records_are_bounded_with_the_learned_urls(clock: FakeClock) -> None:
    store = CrestStore(FakeImages(None), clock=clock, max_urls=2)
    for n in range(1, 6):
        store.learn("team", str(n), f"https://crests.football-data.org/{n}.png")
        run(store.image("team", str(n)))
    assert len(store._urls) == 2 and set(store._failed) <= set(store._urls)


def test_all_competitions_mode_and_featured_order():
    from miniapp.settings import _competitions
    from miniapp.sports.football_data import FEATURED, KNOWN_COMPETITIONS, all_codes, tournaments

    codes = _competitions("all")
    assert len(codes) == len(KNOWN_COMPETITIONS) == len(set(codes)) >= 100
    assert codes[:len(FEATURED)] == FEATURED
    assert _competitions(" pl , pd ") == ("PL", "PD")
    listed = tournaments(all_codes())
    assert [t.featured for t in listed[:len(FEATURED)]] == [True] * len(FEATURED)
    assert not any(t.featured for t in listed[len(FEATURED):])
    assert all(t.area for t in listed)


def test_search_pacing_adapts_to_competitions_and_budget():
    from miniapp.sports.search import REFRESH_AFTER_S, WARM_SPACING_S, search_pacing

    assert search_pacing(10, 8, 3000) == (WARM_SPACING_S, REFRESH_AFTER_S)  # free tier: unchanged
    spacing, refresh = search_pacing(117, 100, 10000)
    assert spacing < WARM_SPACING_S and refresh > REFRESH_AFTER_S
    # the warm-up stays within a quarter of the daily budget
    assert 117 * 86400 / refresh <= 10000 * 0.25 + 1


def test_large_catalogue_day_listing_asks_the_whole_day_and_checks_completeness():
    import asyncio
    from datetime import date

    from miniapp.sports.client import UpstreamError
    from miniapp.sports.football_data import FootballDataAdapter, all_codes

    calls = []

    class Client:
        def __init__(self, payload):
            self.payload = payload

        async def get_json(self, path, params=None):
            calls.append(dict(params or {}))
            return self.payload, None

    match = {"id": 1, "utcDate": "2026-11-08T15:00:00Z", "status": "FINISHED", "stage": "REGULAR_SEASON",
             "competition": {"code": "TSL", "name": "Süper Lig"},
             "homeTeam": {"id": 10, "name": "A"}, "awayTeam": {"id": 11, "name": "B"},
             "score": {"fullTime": {"home": 1, "away": 0}, "regularTime": None}}
    foreign = {**match, "id": 2, "competition": {"code": "ZZZ", "name": "x"}}
    adapter = FootballDataAdapter(Client({"matches": [match, foreign], "resultSet": {"count": 2}}), all_codes(), lambda: 1.8e9)
    events, _ = asyncio.run(adapter.day(date(2026, 11, 8)))
    assert "competitions" not in calls[-1] and [e.id for e in events] == ["fd:1"]
    truncated = FootballDataAdapter(Client({"matches": [match], "resultSet": {"count": 2}}), all_codes(), lambda: 1.8e9)
    try:
        asyncio.run(truncated.day(date(2026, 11, 8)))
        raise AssertionError("a truncated day must not pass")
    except UpstreamError as exc:
        assert exc.code == "bad_response"
    small = FootballDataAdapter(Client({"matches": []}), ("PL", "PD"), lambda: 1.8e9)
    asyncio.run(small.day(date(2026, 11, 8)))
    assert calls[-1]["competitions"] == "PL,PD"


def test_minor_cup_final_cannot_outrank_the_main_leagues():
    from miniapp.sports.top import WEIGHTS

    assert WEIGHTS.other_competition + min(WEIGHTS.stage["FINAL"], WEIGHTS.other_stage_cap) < WEIGHTS.competition["PL"]


def test_whole_day_listing_requires_a_count():
    import asyncio
    from datetime import date

    from miniapp.sports.client import UpstreamError
    from miniapp.sports.football_data import FootballDataAdapter, all_codes

    class Client:
        async def get_json(self, path, params=None):
            return {"matches": []}, None  # no resultSet: completeness cannot be proven

    try:
        asyncio.run(FootballDataAdapter(Client(), all_codes(), lambda: 1.8e9).day(date(2026, 11, 8)))
        raise AssertionError("an unprovable whole day must not pass")
    except UpstreamError as exc:
        assert exc.code == "bad_response"


def test_minor_competition_bonuses_are_capped_with_a_table():
    from types import SimpleNamespace

    from miniapp.sports.top import WEIGHTS, Table, score

    event = SimpleNamespace(stage="FINAL", status="finished", starts_at="2026-11-08T15:00:00Z",
                            home=SimpleNamespace(id="a"), away=SimpleNamespace(id="b"))
    table = Table(teams=20, positions={"a": 1, "b": 2})
    minor = score(event, "ZZZ", table)
    major = score(SimpleNamespace(**{**event.__dict__, "stage": None}), "PL", None)
    assert minor.score <= WEIGHTS.other_competition + WEIGHTS.other_stage_cap < major.score


def test_cup_without_a_table_is_a_cached_empty_table_and_huge_days_are_rejected():
    import asyncio
    from datetime import date

    from miniapp.sports.client import UpstreamError
    from miniapp.sports.football_data import NO_TABLE_TTL_S, WHOLE_DAY_MAX, FootballDataAdapter, all_codes

    class NotFound:
        async def get_json(self, path, params=None):
            raise UpstreamError("not_found", status=404)

    standings, ttl = asyncio.run(FootballDataAdapter(NotFound(), all_codes(), lambda: 1.8e9).standings("FAC"))
    assert standings.blocks == () and ttl == NO_TABLE_TTL_S

    class Huge:
        async def get_json(self, path, params=None):
            return {"matches": [{}] * WHOLE_DAY_MAX, "resultSet": {"count": WHOLE_DAY_MAX}}, None

    try:
        asyncio.run(FootballDataAdapter(Huge(), all_codes(), lambda: 1.8e9).day(date(2026, 11, 8)))
        raise AssertionError("a possibly truncated whole day must not pass")
    except UpstreamError as exc:
        assert exc.code == "bad_response"
