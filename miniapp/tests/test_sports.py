"""Sports data layer (M3): offline only — the provider is a fake transport.

The payloads below imitate the football-data.org v4 shape; they are test data,
never served by the app.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from miniapp.app import create_app
from miniapp.settings import DEFAULT_COMPETITIONS, Settings, SettingsError
from miniapp.sports import football_data
from miniapp.sports.cache import SportsCache
from miniapp.sports.client import ClientLimits, HttpResponse, ProviderClient, TransportError, UpstreamError
from miniapp.sports.football_data import FootballDataAdapter, normalize_match
from miniapp.sports.service import RequestError, SportsService, build_service
from miniapp.tests.conftest import NOW, ORIGIN, FakeClock, fresh, login
from miniapp.tests.signing import TEST_BOT_TOKEN

KEY = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
DAY = "2027-01-15"  # NOW is 2027-01-15T08:00:00Z
FETCHED = "2027-01-15T08:00:00Z"
ALLOWED = frozenset({"PL", "PD"})


def team(tid: int, name: str) -> dict:
    return {"id": tid, "name": name, "shortName": name.split()[0], "tla": name[:3].upper(), "crest": "https://x/c.png"}


def match(mid: int, status: str = "TIMED", *, code: str = "PL", utc: str = f"{DAY}T15:00:00Z",
          full: tuple | None = None, half: tuple | None = None, minute: int | None = None,
          home: tuple = (57, "Arsenal FC"), away: tuple = (61, "Chelsea FC")) -> dict:
    m = {
        "id": mid, "utcDate": utc, "status": status, "lastUpdated": "2027-01-15T07:59:00Z",
        "competition": {"id": 2021 if code == "PL" else 2014, "code": code, "name": "x"},
        "homeTeam": team(*home), "awayTeam": team(*away),
        "score": {"winner": None, "duration": "REGULAR",
                  "fullTime": {"home": full[0] if full else None, "away": full[1] if full else None},
                  "halfTime": {"home": half[0] if half else None, "away": half[1] if half else None}},
    }
    if minute is not None:
        m["minute"] = minute
    return m


class FakeTransport:
    """Scripted upstream: list of responses (or exceptions) per URL path prefix."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.script: list[object] = []
        self.default: object | None = None

    def ok(self, payload: dict, headers: dict | None = None) -> HttpResponse:
        return HttpResponse(200, headers or {}, json.dumps(payload).encode())

    async def __call__(self, url: str, headers, timeout_s: float) -> HttpResponse:
        self.calls.append((url, dict(headers)))
        item = self.script.pop(0) if self.script else self.default
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, HttpResponse), f"unscripted call {url}"
        return item


async def no_sleep(_: float) -> None:
    return None


def make_client(transport: FakeTransport, clock: FakeClock, **limits) -> ProviderClient:
    lim = ClientLimits(per_minute=limits.pop("per_minute", 8), per_day=limits.pop("per_day", 100), **limits)
    return ProviderClient(football_data.PROVIDER, football_data.BASE_URL, {"X-Auth-Token": KEY}, lim,
                          transport=transport, clock=clock, sleep=no_sleep)


def make_service(transport: FakeTransport, clock: FakeClock, **limits) -> SportsService:
    adapter = FootballDataAdapter(make_client(transport, clock, **limits), ("PL", "PD"), clock)
    return SportsService(adapter, cache=SportsCache(clock=clock), clock=clock, unknown_lookups_per_hour=2)


def run(coro):
    return asyncio.run(coro)


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


# --- normalization -------------------------------------------------------------

@pytest.mark.parametrize(("source", "status"), [
    ("SCHEDULED", "upcoming"), ("TIMED", "upcoming"),
    ("IN_PLAY", "live"), ("PAUSED", "live"), ("EXTRA_TIME", "live"), ("PENALTY_SHOOTOUT", "live"),
    ("FINISHED", "finished"), ("AWARDED", "finished"),
    ("POSTPONED", "postponed"), ("SUSPENDED", "suspended"), ("CANCELLED", "cancelled"),
    ("SOMETHING_NEW", "unknown"),
])
def test_every_provider_status_maps_explicitly(source: str, status: str) -> None:
    event = normalize_match(match(1, source, full=(1, 0)), FETCHED, ALLOWED)
    assert event is not None and event.status == status and event.source_status == source
    assert event.awarded is (source == "AWARDED")


def test_postponed_and_cancelled_are_never_upcoming_and_carry_no_score() -> None:
    for source in ("POSTPONED", "CANCELLED"):
        event = normalize_match(match(1, source, full=(2, 2)), FETCHED, ALLOWED)
        assert event.status != "upcoming" and event.score is None and event.minute is None


def test_live_match_score_minute_and_provenance() -> None:
    event = normalize_match(match(42, "IN_PLAY", full=(2, 1), half=(1, 1), minute=67), FETCHED, ALLOWED)
    assert event.id == "fd:42" and event.tournament_id == "fd:PL"
    assert event.score == (2, 1) and event.half_time == (1, 1) and event.minute == 67
    assert event.starts_at == f"{DAY}T15:00:00Z"
    assert event.provenance.provider == "football-data.org"
    assert event.provenance.fetched_at == FETCHED and event.provenance.source_updated_at == "2027-01-15T07:59:00Z"
    body = event.to_json()
    # Nothing is invented: no statistics, lineups, odds or probabilities.
    assert body["stats"] is None and body["statsAvailability"] == "unavailable"
    assert not {"odds", "probabilities", "lineups", "xg", "possession"} & set(body)


def test_upcoming_has_no_score_even_if_provider_sends_zeros() -> None:
    event = normalize_match(match(1, "TIMED", full=(0, 0), minute=0), FETCHED, ALLOWED)
    assert event.score is None and event.minute is None


def test_missing_minute_stays_none_not_zero() -> None:
    assert normalize_match(match(1, "IN_PLAY", full=(0, 0)), FETCHED, ALLOWED).minute is None


@pytest.mark.parametrize("broken", [
    {"id": "x"}, {"id": 1}, "nope",
    {**match(1), "utcDate": "2027-01-15T15:00:00"},  # no zone: never guess
    {**match(1), "homeTeam": {"id": 1}},
    {**match(1), "competition": {"code": "SA"}},  # not configured
])
def test_unusable_or_out_of_scope_matches_are_dropped(broken) -> None:
    assert normalize_match(broken, FETCHED, ALLOWED) is None


def test_partial_score_is_not_turned_into_a_score() -> None:
    m = match(1, "IN_PLAY")
    m["score"]["fullTime"] = {"home": 1, "away": None}
    assert normalize_match(m, FETCHED, ALLOWED).score is None


def test_same_names_in_different_competitions_stay_distinct() -> None:
    a = normalize_match(match(1, code="PL", home=(1, "United"), away=(2, "City")), FETCHED, ALLOWED)
    b = normalize_match(match(2, code="PD", home=(3, "United"), away=(4, "City")), FETCHED, ALLOWED)
    assert a.tournament_id != b.tournament_id and a.home.id != b.home.id
    ids = [t.id for t in football_data.tournaments(["PL", "PD"])]
    assert ids == ["fd:PL", "fd:PD"]


def test_event_id_parsing_is_strict() -> None:
    assert football_data.parse_event_id("fd:123") == 123
    for bad in ("fd:", "fd:-1", "fd:1e3", "xx:1", "123", "fd:1/../2", "fd:" + "9" * 13, "fd:²", "fd:١٢",
                "fd:0", "fd:0001"):
        assert football_data.parse_event_id(bad) is None


# --- client: limits, retries, errors -----------------------------------------------

def test_429_cools_down_without_retry_and_blocks_further_calls(clock: FakeClock) -> None:
    t = FakeTransport()
    t.script = [HttpResponse(429, {"retry-after": "30"}, b"")]
    c = make_client(t, clock)
    with pytest.raises(UpstreamError) as exc:
        run(c.get_json("/matches"))
    assert exc.value.code == "rate_limited" and len(t.calls) == 1
    with pytest.raises(UpstreamError):
        run(c.get_json("/matches"))  # cooling down: not even attempted
    assert len(t.calls) == 1 and c.usage()["coolingDownSeconds"] == 30
    clock.advance(31)
    t.script = [t.ok({"matches": []})]
    run(c.get_json("/matches"))
    assert len(t.calls) == 2


def test_5xx_and_timeouts_retry_with_backoff_then_succeed(clock: FakeClock) -> None:
    t = FakeTransport()
    sleeps: list[float] = []

    async def record(s: float) -> None:
        sleeps.append(s)

    t.script = [HttpResponse(502, {}, b""), TransportError("timeout"), t.ok({"ok": 1})]
    c = ProviderClient("p", "https://api.example.org", {}, ClientLimits(per_minute=8, per_day=10, backoff_s=0.5),
                       transport=t, clock=clock, sleep=record)
    data, _ = run(c.get_json("/x"))
    assert data == {"ok": 1} and len(t.calls) == 3 and sleeps == [0.5, 1.0]
    assert c.usage()["usedToday"] == 3  # every attempt counts against the quota


def test_persistent_5xx_gives_up_after_retries(clock: FakeClock) -> None:
    t = FakeTransport()
    t.default = HttpResponse(503, {}, b"")
    c = make_client(t, clock, retries=2)
    with pytest.raises(UpstreamError) as exc:
        run(c.get_json("/x"))
    assert exc.value.code == "upstream_error" and len(t.calls) == 3


@pytest.mark.parametrize("status", [400, 401, 403])
def test_client_errors_are_not_retried(clock: FakeClock, status: int) -> None:
    t = FakeTransport()
    t.default = HttpResponse(status, {}, b"")
    with pytest.raises(UpstreamError):
        run(make_client(t, clock).get_json("/x"))
    assert len(t.calls) == 1


def test_bad_json_is_an_error_not_data(clock: FakeClock) -> None:
    t = FakeTransport()
    t.script = [HttpResponse(200, {}, b"<html>")]
    with pytest.raises(UpstreamError) as exc:
        run(make_client(t, clock).get_json("/x"))
    assert exc.value.code == "bad_response"


def test_per_minute_and_daily_budgets(clock: FakeClock) -> None:
    t = FakeTransport()
    t.default = t.ok({})
    c = make_client(t, clock, per_minute=2, per_day=3)
    run(c.get_json("/a"))
    run(c.get_json("/b"))
    with pytest.raises(UpstreamError) as exc:
        run(c.get_json("/c"))
    assert exc.value.code == "rate_limited"
    clock.advance(61)
    run(c.get_json("/c"))
    clock.advance(61)
    with pytest.raises(UpstreamError) as exc:
        run(c.get_json("/d"))
    assert exc.value.code == "quota_exhausted" and len(t.calls) == 3
    clock.advance(24 * 3600)  # next UTC day: the daily budget resets
    run(c.get_json("/d"))


def test_key_only_in_header_never_in_url_or_logs(clock: FakeClock, caplog: pytest.LogCaptureFixture) -> None:
    t = FakeTransport()
    t.script = [HttpResponse(500, {}, b""), HttpResponse(500, {}, b""), HttpResponse(500, {}, b"")]
    caplog.set_level(logging.DEBUG)
    with pytest.raises(UpstreamError):
        run(make_client(t, clock).get_json("/matches", {"dateFrom": DAY}))
    url, headers = t.calls[0]
    assert url.startswith("https://api.football-data.org/v4/matches?") and KEY not in url
    assert headers["X-Auth-Token"] == KEY
    assert KEY not in caplog.text and "dateFrom" not in caplog.text


@pytest.mark.parametrize("path", ["matches", "//evil.example/x", "/../x", "/a//b"])
def test_only_plain_paths_on_the_fixed_host(clock: FakeClock, path: str) -> None:
    with pytest.raises(ValueError):
        run(make_client(FakeTransport(), clock).get_json(path))


def test_https_base_required() -> None:
    with pytest.raises(ValueError):
        ProviderClient("p", "http://api.example.org", {}, ClientLimits(per_minute=1, per_day=1))


# --- cache ---------------------------------------------------------------------

def day_payload(*matches: dict) -> dict:
    return {"matches": list(matches)}


def test_concurrent_requests_for_one_day_cost_one_upstream_call(clock: FakeClock) -> None:
    t = FakeTransport()
    gate = asyncio.Event()

    async def slow(url, headers, timeout_s):
        t.calls.append((url, dict(headers)))
        await gate.wait()
        return t.ok(day_payload(match(1)))

    service = make_service(t, clock)
    service.football.client._transport = slow
    start, end = utc(f"{DAY}T00:00:00Z"), utc("2027-01-16T00:00:00Z")

    async def burst():
        tasks = [asyncio.create_task(service.events("football", start, end, None)) for _ in range(50)]
        await asyncio.sleep(0)
        gate.set()
        return await asyncio.gather(*tasks)

    results = run(burst())
    assert len(t.calls) == 1 and all(len(r.events) == 1 for r in results)


def test_ttl_depends_on_match_state(clock: FakeClock) -> None:
    t = FakeTransport()
    t.default = t.ok(day_payload(match(1, "IN_PLAY", full=(0, 0))))
    service = make_service(t, clock)
    start, end = utc(f"{DAY}T00:00:00Z"), utc("2027-01-16T00:00:00Z")
    run(service.events("football", start, end, None))
    clock.advance(30)
    run(service.events("football", start, end, None))
    assert len(t.calls) == 1  # live data: reused for up to a minute
    clock.advance(31)
    run(service.events("football", start, end, None))
    assert len(t.calls) == 2

    t.default = t.ok(day_payload(match(1, "TIMED")))
    clock.advance(61)
    run(service.events("football", start, end, None))
    clock.advance(10 * 60)
    run(service.events("football", start, end, None))
    assert len(t.calls) == 3  # upcoming: 15 minutes


def test_failed_refresh_serves_stale_data_marked_with_its_time(clock: FakeClock) -> None:
    t = FakeTransport()
    t.script = [t.ok(day_payload(match(1, "IN_PLAY", full=(1, 0))))]
    service = make_service(t, clock, retries=0)
    start, end = utc(f"{DAY}T00:00:00Z"), utc("2027-01-16T00:00:00Z")
    first = run(service.events("football", start, end, None))
    assert first.stale is False
    clock.advance(120)
    t.script = [HttpResponse(503, {}, b"")]
    second = run(service.events("football", start, end, None))
    assert second.stale is True and second.fetched_at == first.fetched_at
    assert second.events[0].provenance.stale is True and second.events[0].score == (1, 0)


def test_without_cached_data_an_outage_is_an_error_not_demo_data(clock: FakeClock) -> None:
    t = FakeTransport()
    t.default = HttpResponse(503, {}, b"")
    service = make_service(t, clock, retries=0)
    with pytest.raises(UpstreamError):
        run(service.events("football", utc(f"{DAY}T00:00:00Z"), utc("2027-01-16T00:00:00Z"), None))


# --- service: ranges, days, tournaments, ids ---------------------------------------

def test_local_day_across_utc_midnight_reads_both_utc_days(clock: FakeClock) -> None:
    # Baku (UTC+4): 15 Jan 00:00..24:00 local = 14 Jan 20:00Z .. 15 Jan 20:00Z.
    t = FakeTransport()
    t.script = [
        t.ok(day_payload(match(1, utc="2027-01-14T19:30:00Z"), match(2, utc="2027-01-14T21:00:00Z"))),
        t.ok(day_payload(match(3, utc=f"{DAY}T19:59:00Z"), match(4, utc=f"{DAY}T20:00:00Z"))),
    ]
    service = make_service(t, clock)
    result = run(service.events("football", utc("2027-01-14T20:00:00Z"), utc(f"{DAY}T20:00:00Z"), None))
    assert [e.id for e in result.events] == ["fd:2", "fd:3"]
    assert "dateFrom=2027-01-14" in t.calls[0][0] and "dateFrom=2027-01-15" in t.calls[1][0]


def test_provider_overlap_produces_no_duplicates(clock: FakeClock) -> None:
    t = FakeTransport()
    t.script = [t.ok(day_payload(match(1), match(1), match(2, utc="2027-01-16T10:00:00Z")))]
    service = make_service(t, clock)
    result = run(service.events("football", utc(f"{DAY}T00:00:00Z"), utc("2027-01-16T00:00:00Z"), None))
    assert [e.id for e in result.events] == ["fd:1"]  # match 2 belongs to the next UTC day


def test_tournament_filter_and_unknown_tournament(clock: FakeClock) -> None:
    t = FakeTransport()
    t.default = t.ok(day_payload(match(1, code="PL"), match(2, code="PD")))
    service = make_service(t, clock)
    start, end = utc(f"{DAY}T00:00:00Z"), utc("2027-01-16T00:00:00Z")
    assert [e.id for e in run(service.events("football", start, end, "fd:PD")).events] == ["fd:2"]
    with pytest.raises(RequestError):
        run(service.events("football", start, end, "fd:SA"))


@pytest.mark.parametrize(("start", "end", "code"), [
    ("2027-01-15T00:00:00Z", "2027-01-15T00:00:00Z", "bad_range"),
    ("2027-01-15T00:00:00Z", "2027-01-17T00:00:01Z", "bad_range"),
    ("2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z", "range_out_of_window"),
    ("2027-05-01T00:00:00Z", "2027-05-02T00:00:00Z", "range_out_of_window"),
])
def test_range_validation_spends_no_quota(clock: FakeClock, start: str, end: str, code: str) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    with pytest.raises(RequestError) as exc:
        run(service.events("football", utc(start), utc(end), None))
    assert exc.value.code == code and t.calls == []


def test_sports_without_a_provider_are_not_connected_and_empty(clock: FakeClock) -> None:
    t = FakeTransport()
    service = make_service(t, clock)
    start, end = utc(f"{DAY}T00:00:00Z"), utc("2027-01-16T00:00:00Z")
    for sport in ("tennis", "basketball", "mma"):
        result = run(service.events(sport, start, end, None))
        assert result.events == [] and result.tournaments == []
        assert service.coverage()["sports"][sport] == {"status": "not_connected", "provider": None, "tournaments": []}
    assert t.calls == []


def test_event_detail_cached_and_not_found_cached(clock: FakeClock) -> None:
    t = FakeTransport()
    t.script = [t.ok(match(7, "FINISHED", full=(3, 1))), HttpResponse(404, {}, b"")]
    service = make_service(t, clock)
    event, tournament = run(service.event("fd:7"))
    assert event.score == (3, 1) and tournament.id == "fd:PL"
    run(service.event("fd:7"))
    assert len(t.calls) == 1
    assert run(service.event("fd:8")) == (None, None)
    assert run(service.event("fd:8")) == (None, None)
    assert len(t.calls) == 2  # the 404 is remembered


def test_unknown_ids_have_their_own_hourly_budget(clock: FakeClock) -> None:
    t = FakeTransport()
    t.default = HttpResponse(404, {}, b"")
    service = make_service(t, clock)  # 2 unknown lookups per hour
    run(service.event("fd:100"))
    run(service.event("fd:101"))
    with pytest.raises(UpstreamError):
        run(service.event("fd:102"))
    assert len(t.calls) == 2
    assert run(service.event("not-an-id")) == (None, None) and len(t.calls) == 2


def test_ids_seen_in_a_listing_do_not_spend_the_unknown_budget(clock: FakeClock) -> None:
    t = FakeTransport()
    t.script = [t.ok(day_payload(match(1), match(2), match(3)))]
    t.default = t.ok(match(1))
    service = make_service(t, clock)
    run(service.events("football", utc(f"{DAY}T00:00:00Z"), utc("2027-01-16T00:00:00Z"), None))
    for mid in (1, 2, 3):
        clock.advance(1)
        service.cache._entries.pop(f"football:match:{mid}", None)
        t.default = t.ok(match(mid))
        run(service.event(f"fd:{mid}"))  # budget is 2, three known ids pass


# --- settings ------------------------------------------------------------------------

def base_settings(tmp_path, **kw) -> Settings:
    return Settings(bot_token=TEST_BOT_TOKEN, db_path=tmp_path / "m.db", trusted_origins=frozenset({ORIGIN}),
                    environment="development", **kw)


def test_key_is_not_in_settings_repr(tmp_path) -> None:
    assert KEY not in repr(base_settings(tmp_path, football_data_key=KEY))


@pytest.mark.parametrize("kw", [
    {"football_data_key": "short"}, {"football_data_key": "x" * 20 + "!"},
    {"football_data_competitions": ("XX",)}, {"football_data_competitions": ()},
    {"football_data_per_minute": 11}, {"football_data_per_day": 0},
])
def test_invalid_sports_settings_are_rejected(tmp_path, kw) -> None:
    with pytest.raises(SettingsError):
        base_settings(tmp_path, **kw)


def test_from_env_reads_only_miniapp_variables(tmp_path) -> None:
    env = {"MINIAPP_BOT_TOKEN": TEST_BOT_TOKEN, "MINIAPP_DB_PATH": str(tmp_path / "m.db"),
           "MINIAPP_TRUSTED_ORIGINS": ORIGIN, "MINIAPP_ENV": "development",
           "FOOTBALL_KEY": KEY, "APIFOOTBALL_KEY": KEY,  # the bot's variables: ignored
           "MINIAPP_FOOTBALL_DATA_COMPETITIONS": "pl, pd ,CL"}
    s = Settings.from_env(env)
    assert s.football_data_key is None and s.football_data_competitions == ("PL", "PD", "CL")
    assert build_service(s).coverage()["sports"]["football"]["status"] == "not_connected"


# --- HTTP API ------------------------------------------------------------------------

@pytest.fixture()
def api(tmp_path, clock: FakeClock):
    t = FakeTransport()
    settings = base_settings(tmp_path, football_data_key=KEY, session_ttl_s=3600)
    # Emblem warm-up (one /competitions call a day) is covered in test_tournaments_crests.py;
    # off here so these tests keep exact upstream call scripts.
    service = build_service(settings, clock=clock, transport=t, warm_emblems=False)
    service.football.client._sleep = no_sleep
    app = create_app(settings, clock=clock, sports=service)
    return TestClient(app, base_url="http://127.0.0.1:8000"), t


def signed_in(client: TestClient, clock: FakeClock) -> None:
    assert login(client, fresh(clock)).status_code == 200


def test_sports_api_requires_a_session_and_spends_no_quota_otherwise(api, clock: FakeClock) -> None:
    client, t = api
    for path in ("/api/sports/coverage", "/api/sports/football/tournaments",
                 f"/api/sports/football/events?from={DAY}T00:00:00Z&to=2027-01-16T00:00:00Z",
                 "/api/sports/event/fd:1", "/api/sports/cricket/events", "/api/sports/cricket/tournaments"):
        r = client.get(path)
        assert r.status_code == 401 and r.json() == {"error": "unauthenticated"}, path
    assert t.calls == []


def test_events_api_returns_real_provider_events(api, clock: FakeClock) -> None:
    client, t = api
    signed_in(client, clock)
    t.script = [t.ok(day_payload(match(11, "IN_PLAY", full=(1, 1), minute=55), match(12, "POSTPONED")))]
    r = client.get(f"/api/sports/football/events?from={DAY}T00:00:00Z&to=2027-01-16T00:00:00Z")
    assert r.status_code == 200
    body = r.json()
    assert body["coverage"] == {"status": "connected", "provider": "football-data.org",
                                "attribution": "Football data provided by the Football-Data.org API",
                                "liveScores": "delayed"}  # free plan: the provider delays scores
    assert [(e["id"], e["status"]) for e in body["events"]] == [("fd:11", "live"), ("fd:12", "postponed")]
    assert body["events"][0]["score"] == [1, 1] and body["events"][0]["minute"] == 55
    assert body["fetchedAt"] == "2027-01-15T08:00:00Z" and body["stale"] is False
    assert [x["id"] for x in body["tournaments"]] == [f"fd:{c}" for c in DEFAULT_COMPETITIONS.split(",")]
    # No key and no third-party image URL: a crest from an unknown host is simply not offered.
    assert KEY not in r.text and "https://" not in r.text
    assert all(e[side]["crest"] is None for e in body["events"] for side in ("home", "away"))


def test_events_api_validates_parameters(api, clock: FakeClock) -> None:
    client, t = api
    signed_in(client, clock)
    for query in ("", f"?from={DAY}T00:00:00Z", "?from=x&to=y",
                  f"?from={DAY}T00:00:00Z&to=2027-01-16T00:00:00Z&limit=999",
                  f"?from={DAY}T00:00:00&to=2027-01-16T00:00:00"):
        assert client.get(f"/api/sports/football/events{query}").status_code == 400, query
    assert client.get("/api/sports/cricket/events").status_code == 404
    assert t.calls == []


def test_other_sports_report_not_connected_without_demo_events(api, clock: FakeClock) -> None:
    client, t = api
    signed_in(client, clock)
    r = client.get(f"/api/sports/tennis/events?from={DAY}T00:00:00Z&to=2027-01-16T00:00:00Z")
    assert r.status_code == 200
    assert r.json()["coverage"] == {"status": "not_connected", "provider": None, "attribution": None,
                                    "liveScores": None} and r.json()["events"] == []
    assert t.calls == []


def test_upstream_outage_is_a_503_with_a_reason(api, clock: FakeClock) -> None:
    client, t = api
    signed_in(client, clock)
    t.default = HttpResponse(429, {"retry-after": "60"}, b"")
    r = client.get(f"/api/sports/football/events?from={DAY}T00:00:00Z&to=2027-01-16T00:00:00Z")
    assert r.status_code == 503 and r.json() == {"error": "source_unavailable", "reason": "rate_limited"}
    assert r.headers["retry-after"] == "60"


def test_event_api_and_coverage(api, clock: FakeClock) -> None:
    client, t = api
    signed_in(client, clock)
    t.script = [t.ok(match(21, "FINISHED", full=(2, 0), half=(1, 0)))]
    r = client.get("/api/sports/event/fd:21")
    assert r.status_code == 200 and r.json()["event"]["score"] == [2, 0]
    assert r.json()["tournament"]["id"] == "fd:PL"
    assert client.get("/api/sports/event/" + "9" * 50).status_code == 404
    cov = client.get("/api/sports/coverage").json()["sports"]
    assert cov["football"]["status"] == "connected" and cov["football"]["features"]["statistics"] is False
    assert cov["football"]["quota"]["usedToday"] == 1
    assert {cov[s]["status"] for s in ("tennis", "basketball", "mma")} == {"not_connected"}
    assert KEY not in json.dumps(cov)


def test_no_key_means_not_connected_and_no_upstream_at_all(client: TestClient, clock: FakeClock) -> None:
    signed_in(client, clock)
    r = client.get(f"/api/sports/football/events?from={DAY}T00:00:00Z&to=2027-01-16T00:00:00Z")
    assert r.status_code == 200 and r.json()["coverage"]["status"] == "not_connected" and r.json()["events"] == []


def test_today_is_the_clock_day() -> None:
    assert datetime.fromtimestamp(NOW, UTC).date() == date(2027, 1, 15)


def test_provider_reporting_an_empty_minute_budget_pauses_us_before_a_429(clock: FakeClock) -> None:
    t = FakeTransport()
    t.script = [t.ok({}, {"x-requests-available-minute": "0", "x-requestcounter-reset": "20"})]
    c = make_client(t, clock)
    run(c.get_json("/a"))
    with pytest.raises(UpstreamError) as exc:
        run(c.get_json("/b"))
    assert exc.value.code == "rate_limited" and len(t.calls) == 1
    clock.advance(21)
    t.script = [t.ok({})]
    run(c.get_json("/b"))


def test_live_plan_switch_changes_only_the_declared_freshness(tmp_path, clock: FakeClock) -> None:
    paid = build_service(base_settings(tmp_path, football_data_key=KEY, football_data_live_plan=True), clock=clock)
    free = build_service(base_settings(tmp_path, football_data_key=KEY), clock=clock)
    assert paid.coverage()["sports"]["football"]["features"]["liveScores"] == "realtime"
    assert free.coverage()["sports"]["football"]["features"]["liveScores"] == "delayed"


def test_real_transport_never_follows_a_redirect_with_the_key() -> None:
    # Loopback only: the "provider" redirects to a second server that must receive nothing.
    import http.server
    import threading

    from miniapp.sports.client import urllib_transport

    stolen: list[dict] = []

    class Thief(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            stolen.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args) -> None:
            pass

    thief = http.server.HTTPServer(("127.0.0.1", 0), Thief)

    class Provider(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{thief.server_port}/steal")
            self.end_headers()

        def log_message(self, *args) -> None:
            pass

    provider = http.server.HTTPServer(("127.0.0.1", 0), Provider)
    for server in (thief, provider):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        response = run(urllib_transport(f"http://127.0.0.1:{provider.server_port}/v4/matches", {"X-Auth-Token": KEY}, 5))
    finally:
        provider.shutdown()
        thief.shutdown()
    assert response.status == 302 and stolen == []


def test_during_an_outage_stale_data_holds_back_refreshes_for_everyone(clock: FakeClock) -> None:
    t = FakeTransport()
    t.script = [t.ok(day_payload(match(1, "IN_PLAY", full=(1, 0))))]
    service = make_service(t, clock, retries=0)
    start, end = utc(f"{DAY}T00:00:00Z"), utc("2027-01-16T00:00:00Z")
    first = run(service.events("football", start, end, None))
    clock.advance(120)  # live TTL passed
    t.default = HttpResponse(503, {}, b"")
    for _ in range(5):  # five page views during the outage
        result = run(service.events("football", start, end, None))
        assert result.stale is True and result.fetched_at == first.fetched_at
        clock.advance(1)
    assert len(t.calls) == 2  # one load, one failed refresh: the rest waited out the backoff
    clock.advance(30)
    run(service.events("football", start, end, None))
    assert len(t.calls) == 3  # retried once the backoff ended


def test_outage_without_cached_data_is_remembered_not_retried_per_view(clock: FakeClock) -> None:
    t = FakeTransport()
    t.default = HttpResponse(503, {}, b"")
    service = make_service(t, clock, retries=0)
    start, end = utc(f"{DAY}T00:00:00Z"), utc("2027-01-16T00:00:00Z")
    for _ in range(5):
        with pytest.raises(UpstreamError) as exc:
            run(service.events("football", start, end, None))
        assert exc.value.code == "upstream_error"
        clock.advance(1)
    assert len(t.calls) == 1  # one failed request; four views got the remembered error
    clock.advance(30)
    t.default = t.ok(day_payload(match(1)))
    assert [e.id for e in run(service.events("football", start, end, None)).events] == ["fd:1"]
    assert len(t.calls) == 2


def test_data_past_the_stale_limit_is_never_served_and_the_outage_is_remembered(clock: FakeClock) -> None:
    t = FakeTransport()
    t.script = [t.ok(day_payload(match(1, "IN_PLAY", full=(1, 0))))]
    service = make_service(t, clock, retries=0)
    start, end = utc(f"{DAY}T00:00:00Z"), utc("2027-01-16T00:00:00Z")
    run(service.events("football", start, end, None))
    clock.advance(6 * 3600 + 1)  # older than max_stale_s
    t.default = HttpResponse(503, {}, b"")
    for _ in range(3):
        with pytest.raises(UpstreamError):
            run(service.events("football", start, end, None))
    assert len(t.calls) == 2  # the initial load + one failed refresh


def test_a_late_failed_refresh_cannot_extend_stale_data_past_its_limit(clock: FakeClock) -> None:
    cache = SportsCache(max_stale_s=100, failure_backoff_s=30, clock=clock)
    calls: list[int] = []

    async def ok():
        calls.append(1)
        return "value", 10.0

    async def down():
        calls.append(1)
        raise UpstreamError("upstream_error")

    run(cache.get("k", ok))
    clock.advance(95)  # 5 s before the stale limit
    assert run(cache.get("k", down)).stale is True
    clock.advance(4)  # still inside the limit: held, no new call
    assert run(cache.get("k", down)).stale is True and len(calls) == 2
    clock.advance(2)  # past fetched_at + max_stale_s: never served again
    with pytest.raises(UpstreamError):
        run(cache.get("k", down))


def test_an_answer_with_only_broken_records_is_an_error_not_an_empty_day(clock: FakeClock) -> None:
    t = FakeTransport()
    t.script = [t.ok({"matches": [{"id": 1}, {"id": 2, "competition": {"code": "PL"}}]})]
    service = make_service(t, clock, retries=0)
    with pytest.raises(UpstreamError) as exc:
        run(service.events("football", utc(f"{DAY}T00:00:00Z"), utc("2027-01-16T00:00:00Z"), None))
    assert exc.value.code == "bad_response"


def test_records_filtered_out_of_scope_or_day_still_make_a_valid_empty_day(clock: FakeClock) -> None:
    t = FakeTransport()
    t.script = [t.ok(day_payload(match(1, code="SA"), match(2, utc="2027-01-16T10:00:00Z")))]
    service = make_service(t, clock)
    result = run(service.events("football", utc(f"{DAY}T00:00:00Z"), utc("2027-01-16T00:00:00Z"), None))
    assert result.events == []  # SA is not served, match 2 is tomorrow: a valid empty day


def test_an_expired_not_found_entry_cannot_bypass_the_unknown_id_budget(clock: FakeClock) -> None:
    t = FakeTransport()
    t.default = HttpResponse(404, {}, b"")
    adapter = FootballDataAdapter(make_client(t, clock), ("PL", "PD"), clock)
    service = SportsService(adapter, cache=SportsCache(clock=clock), clock=clock, unknown_lookups_per_hour=1)
    assert run(service.event("fd:500")) == (None, None)
    clock.advance(15 * 60 + 1)  # the cached 404 expired
    # The budget refuses the refresh: the earlier answer stands, the provider is not asked again.
    assert run(service.event("fd:500")) == (None, None)
    assert len(t.calls) == 1
    with pytest.raises(UpstreamError) as exc:
        run(service.event("fd:501"))  # a new unknown id is blocked as well
    assert exc.value.code == "rate_limited" and len(t.calls) == 1


def test_a_cancelled_caller_does_not_cancel_the_shared_load(clock: FakeClock) -> None:
    cache = SportsCache(clock=clock)
    gate = asyncio.Event()
    loads: list[int] = []

    async def load():
        loads.append(1)
        await gate.wait()
        return "value", 60.0

    async def scenario():
        first = asyncio.create_task(cache.get("k", load))
        await asyncio.sleep(0)
        second = asyncio.create_task(cache.get("k", load))
        await asyncio.sleep(0)
        first.cancel()  # e.g. the first user closed the screen
        await asyncio.sleep(0)
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        return await second

    assert run(scenario()).value == "value" and len(loads) == 1
    assert run(cache.get("k", load)).value == "value" and len(loads) == 1  # stored for later callers too


def test_malformed_match_detail_is_an_error_but_out_of_scope_is_not_found(clock: FakeClock) -> None:
    t = FakeTransport()
    t.script = [t.ok({"id": 1}), t.ok(match(2, code="SA"))]
    service = make_service(t, clock, retries=0)
    service._known_ids.update({"fd:1", "fd:2"})
    with pytest.raises(UpstreamError) as exc:
        run(service.event("fd:1"))
    assert exc.value.code == "bad_response"
    assert run(service.event("fd:2")) == (None, None)


@pytest.mark.parametrize("bad", [["PL"], {"x": 1}, 7])
def test_unexpected_value_types_are_a_bad_response_held_by_the_backoff(clock: FakeClock, bad) -> None:
    t = FakeTransport()
    broken = match(1)
    broken["competition"]["code"] = bad
    t.default = t.ok({"matches": [broken]})
    service = make_service(t, clock, retries=0)
    start, end = utc(f"{DAY}T00:00:00Z"), utc("2027-01-16T00:00:00Z")
    for _ in range(3):
        with pytest.raises(UpstreamError) as exc:
            run(service.events("football", start, end, None))
        assert exc.value.code == "bad_response"
    assert len(t.calls) == 1  # remembered: repeated views do not spend quota
    t.script = [t.ok(broken)]
    service._known_ids.add("fd:9")
    with pytest.raises(UpstreamError) as exc:
        run(service.event("fd:9"))
    assert exc.value.code == "bad_response"


def test_a_trickling_response_ends_by_the_absolute_deadline() -> None:
    # Loopback server that sends one byte every 0.2 s for a long time: each socket
    # wait is short, so only an overall deadline can stop it.
    import http.server
    import threading
    import time as _time

    from miniapp.sports.client import urllib_transport

    stop = threading.Event()

    class Drip(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "100000")
            self.end_headers()
            while not stop.is_set():
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except OSError:
                    return
                _time.sleep(0.2)

        def log_message(self, *args) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    started = _time.monotonic()
    try:
        with pytest.raises(TransportError):
            run(urllib_transport(f"http://127.0.0.1:{server.server_port}/v4/matches", {}, 1.0))
    finally:
        stop.set()
        server.shutdown()
    assert _time.monotonic() - started < 3.0


def test_one_broken_in_scope_record_makes_the_whole_day_a_bad_response(clock: FakeClock) -> None:
    t = FakeTransport()
    t.script = [t.ok(day_payload(match(1), {"id": 2, "competition": {"code": "PL"}}))]
    service = make_service(t, clock, retries=0)
    with pytest.raises(UpstreamError) as exc:
        run(service.events("football", utc(f"{DAY}T00:00:00Z"), utc("2027-01-16T00:00:00Z"), None))
    assert exc.value.code == "bad_response"


def test_superscript_digit_id_is_not_found_not_a_500(api, clock: FakeClock) -> None:
    client, t = api
    signed_in(client, clock)
    assert client.get("/api/sports/event/fd:%C2%B2").status_code == 404 and t.calls == []


def test_a_range_mixing_a_stale_day_and_a_fresh_day_marks_only_the_stale_events(clock: FakeClock) -> None:
    # Baku day = 14 Jan 20:00Z .. 15 Jan 20:00Z: two UTC days, refreshed separately.
    t = FakeTransport()
    t.script = [
        t.ok(day_payload(match(1, "IN_PLAY", full=(0, 0), utc="2027-01-14T21:00:00Z"))),
        t.ok(day_payload(match(2, "TIMED", utc=f"{DAY}T15:00:00Z"))),
    ]
    service = make_service(t, clock, retries=0)
    start, end = utc("2027-01-14T20:00:00Z"), utc(f"{DAY}T20:00:00Z")
    run(service.events("football", start, end, None))
    clock.advance(120)  # the live day (14th) expired, the upcoming day (15th) did not
    t.script = [HttpResponse(503, {}, b"")]
    result = run(service.events("football", start, end, None))
    assert result.stale is True
    assert {e.id: e.provenance.stale for e in result.events} == {"fd:1": True, "fd:2": False}


def test_a_429_with_a_slow_body_keeps_its_headers_and_never_reads_the_body() -> None:
    # Loopback "provider": 429 + Retry-After, then a body it never finishes sending.
    import http.server
    import threading
    import time as _time

    from miniapp.sports.client import urllib_transport

    stop = threading.Event()

    class Busy(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(429)
            self.send_header("Retry-After", "42")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            while not stop.is_set():
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except OSError:
                    return
                _time.sleep(0.2)

        def log_message(self, *args) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Busy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        response = run(urllib_transport(f"http://127.0.0.1:{server.server_port}/v4/matches", {}, 1.0))
    finally:
        stop.set()
        server.shutdown()
    assert response.status == 429 and response.headers["retry-after"] == "42" and response.body == b""


def test_a_detail_answer_for_another_match_is_rejected_not_cached(clock: FakeClock) -> None:
    t = FakeTransport()
    t.script = [t.ok(match(2, "FINISHED", full=(1, 0))), t.ok(match(1, "FINISHED", full=(3, 3)))]
    service = make_service(t, clock, retries=0)
    service._known_ids.add("fd:1")
    with pytest.raises(UpstreamError) as exc:
        run(service.event("fd:1"))
    assert exc.value.code == "bad_response"
    clock.advance(31)  # after the failure backoff, a correct answer is accepted
    event, _ = run(service.event("fd:1"))
    assert event.id == "fd:1" and event.score == (3, 3)


def test_a_detail_answer_for_another_match_of_an_unserved_competition_is_not_a_cached_404(clock: FakeClock) -> None:
    t = FakeTransport()
    t.script = [t.ok(match(2, code="SA")), t.ok(match(1, "FINISHED", full=(1, 1)))]
    service = make_service(t, clock, retries=0)
    service._known_ids.add("fd:1")
    with pytest.raises(UpstreamError) as exc:
        run(service.event("fd:1"))
    assert exc.value.code == "bad_response"
    clock.advance(31)
    event, _ = run(service.event("fd:1"))
    assert event.id == "fd:1"  # nothing wrong was cached in between


def test_at_most_two_real_provider_connections_even_when_callers_time_out(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each upstream call blocks longer than the caller waits: callers give up, but the
    # number of calls really in progress never exceeds the worker pool (2).
    import threading
    import time as _time

    from miniapp.sports import client as client_module

    active = 0
    peak = 0
    lock = threading.Lock()

    class SlowOpener:
        def open(self, request, timeout):  # noqa: ANN001, ANN201
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                _time.sleep(0.6)
                raise TimeoutError("slow provider")
            finally:
                with lock:
                    active -= 1

    monkeypatch.setattr(client_module, "_OPENER", SlowOpener())

    async def burst():
        async def one():
            with pytest.raises(TransportError):
                await client_module.urllib_transport("https://api.example.org/v4/matches", {}, 0.1)
        await asyncio.gather(*(one() for _ in range(6)))

    run(burst())  # every caller is released by its own deadline...
    _time.sleep(2.0)  # ...while the queued calls drain through the pool
    assert peak == 2


@pytest.mark.parametrize("bad_id", [True, False, -5, 0])
def test_boolean_or_non_positive_ids_are_rejected(bad_id) -> None:
    m = match(1)
    m["id"] = bad_id
    assert normalize_match(m, FETCHED, ALLOWED) is None
    m = match(1)
    m["homeTeam"]["id"] = bad_id
    assert normalize_match(m, FETCHED, ALLOWED) is None


def test_local_daily_budget_exhaustion_shows_in_coverage(clock: FakeClock) -> None:
    t = FakeTransport()
    t.default = t.ok(day_payload())
    service = make_service(t, clock, per_day=1)
    start, end = utc(f"{DAY}T00:00:00Z"), utc("2027-01-16T00:00:00Z")
    run(service.events("football", start, end, None))
    assert service.coverage()["sports"]["football"]["status"] == "connected"
    clock.advance(16 * 60)  # the cached day expired; the budget refuses the refresh
    assert run(service.events("football", start, end, None)).stale is True  # last good data, marked
    assert service.coverage()["sports"]["football"]["status"] == "unavailable"
    assert len(t.calls) == 1
