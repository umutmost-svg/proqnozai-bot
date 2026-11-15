"""F2: each user's share of the shared football-data budget (offline; fake provider transport)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from miniapp.app import create_app
from miniapp.sports.client import HttpResponse
from miniapp.sports.service import build_service
from miniapp.sports.user_limits import IN_FLIGHT_MAX_AGE_S as IN_FLIGHT, MAX_USERS, UserShare, UserUpstreamLimiter
from miniapp.tests.conftest import ORIGIN, FakeClock, fresh, login
from miniapp.tests.test_sports import DAY, KEY, FakeTransport, base_settings, day_payload, match, no_sleep


def test_share_admits_while_there_is_room_and_records_the_real_cost() -> None:
    clock = FakeClock()
    lim = UserUpstreamLimiter(3, 2, clock)
    assert lim.admit(1, ticket="a") is None
    lim.charge(1, 2)
    lim.release(1, "a")
    assert lim.admit(1, ticket="b") is None        # 2 of 3 used: room for one more request
    lim.charge(1, 5)                               # ... which may overshoot by its own cost
    lim.release(1, "b")
    wait = lim.admit(1)
    assert wait is not None and 1 <= wait <= 60
    assert lim.admit(2) is None                    # another user has a share of their own
    clock.advance(60)
    assert lim.admit(1) is None


def test_unknown_ids_per_hour_and_refusal_charges_nothing() -> None:
    clock = FakeClock()
    lim = UserUpstreamLimiter(10, 2, clock)
    assert lim.admit(1, True, "a") is None and lim.admit(1, True, "b") is None
    lim.release(1, "a")
    lim.release(1, "b")
    wait = lim.admit(1, True)
    assert wait is not None and wait > 60
    assert len(lim._unknown[1]) == 2
    assert lim.admit(1, False) is None             # a known id still passes
    clock.advance(3600)
    assert lim.admit(1, True) is None
    assert UserUpstreamLimiter(10, 0, clock).admit(1, True) is not None


def test_memory_stays_bounded() -> None:
    clock = FakeClock()
    lim = UserUpstreamLimiter(3, 2, clock)
    for uid in range(MAX_USERS + 50):
        lim.admit(uid, True)
        lim.charge(uid, 10_000)
    assert len(lim._minute) <= MAX_USERS + 1 and len(lim._unknown) <= MAX_USERS + 1
    assert sum(len(v) for v in lim._in_flight.values()) <= MAX_USERS + 50
    assert max(len(v) for v in lim._minute.values()) <= 6


def test_tally_counts_real_provider_requests_but_not_background_work(clock: FakeClock) -> None:
    import asyncio

    from miniapp.sports.client import LOW_PRIORITY_DAY_SHARE
    from miniapp.tests.test_sports import make_service, utc

    t = FakeTransport()
    t.default = t.ok(day_payload(match(1)))
    service = make_service(t, clock)
    lim = UserUpstreamLimiter(3, 2, clock)

    async def go() -> None:
        async def day(d: int) -> None:
            await service.events("football", utc(f"2027-01-{d}T00:00:00Z"), utc(f"2027-01-{d + 1}T00:00:00Z"), None)
        share = UserShare(lim, 7)
        assert share.gate() is None
        with share.tally() as tally:
            for d in (15, 16, 17):
                await day(d)
            assert tally.count == 3                # three cold days
            await day(15)
            assert tally.count == 3                # cached: free
            token = LOW_PRIORITY_DAY_SHARE.set(0.5)
            try:
                await service.events("football", utc("2027-01-18T00:00:00Z"), utc("2027-01-19T00:00:00Z"), None)
            finally:
                LOW_PRIORITY_DAY_SHARE.reset(token)
            assert tally.count == 3 and len(t.calls) == 4   # background load made, not charged
    asyncio.run(go())
    assert lim.admit(7) is not None                # 3 recorded: the share is used


def test_parallel_requests_cannot_run_past_the_share() -> None:
    clock = FakeClock()
    lim = UserUpstreamLimiter(3, 2, clock)
    shares = [UserShare(lim, 1) for _ in range(20)]
    admitted = [s for s in shares if s.gate() is None]
    assert len(admitted) == 3                      # nothing recorded yet, but three are in progress
    with admitted[0].tally():
        pass                                       # finished without any provider request
    assert UserShare(lim, 1).gate() is None        # its place is free again
    clock.advance(IN_FLIGHT)                       # an admission never released stops counting
    assert UserShare(lim, 1).gate() is None


def test_a_load_outliving_its_request_is_still_charged() -> None:
    from miniapp.sports.client import USER_TALLY

    lim = UserUpstreamLimiter(3, 2, FakeClock())
    share = UserShare(lim, 9)
    assert share.gate() is None
    with share.tally():
        late = USER_TALLY.get()                    # the context a shielded cache load inherited
    for _ in range(3):
        late.count += 1
        late.charge()                              # its requests after the response was sent
    assert lim.admit(9) is not None


@pytest.fixture()
def two_users(tmp_path, clock: FakeClock):
    t = FakeTransport()
    settings = base_settings(tmp_path, football_data_key=KEY, session_ttl_s=3600, sports_user_per_minute=3,
                             sports_user_unknown_per_hour=2, sports_unknown_lookups_per_hour=20)
    service = build_service(settings, clock=clock, transport=t, warm_emblems=False)
    service.football.client._sleep = no_sleep
    app = create_app(settings, clock=clock, sports=service)
    a = TestClient(app, base_url="http://127.0.0.1:8000")
    b = TestClient(app, base_url="http://127.0.0.1:8000")
    assert login(a, fresh(clock, {"id": 1001, "first_name": "A"})).status_code == 200
    assert login(b, fresh(clock, {"id": 1002, "first_name": "B"})).status_code == 200
    return a, b, t


def test_invented_match_ids_exhaust_only_the_inventors_share(two_users) -> None:
    a, b, t = two_users
    t.default = HttpResponse(404, {}, b"")
    assert a.get("/api/sports/event/fd:900").status_code == 404
    assert a.get("/api/sports/event/fd:901").status_code == 404
    r = a.get("/api/sports/event/fd:902")
    assert r.status_code == 503
    assert r.json()["error"] == "source_unavailable" and r.json()["reason"] == "rate_limited"
    assert r.json()["scope"] == "user" and int(r.headers["Retry-After"]) == r.json()["retryAfter"] > 60
    assert len(t.calls) == 2                       # the refused lookup never reached the provider
    assert a.get("/api/sports/event/fd:900").status_code == 404   # remembered: costs nothing, not refused
    assert b.get("/api/sports/event/fd:903").status_code == 404   # the global hourly budget is untouched


def test_walking_the_calendar_is_capped_per_minute_and_cached_days_are_free(two_users, clock) -> None:
    a, b, t = two_users
    t.default = t.ok(day_payload(match(1)))
    days = [f"2027-01-{d:02d}" for d in range(15, 21)]
    codes = [a.get(f"/api/sports/football/events?from={d}T00:00:00Z&to={d}T23:59:59Z").status_code for d in days]
    assert codes == [200, 200, 200, 503, 503, 503]
    assert len(t.calls) == 3
    assert a.get(f"/api/sports/football/events?from={DAY}T00:00:00Z&to={DAY}T23:59:59Z").status_code == 200
    assert b.get("/api/sports/football/events?from=2027-01-19T00:00:00Z&to=2027-01-19T23:59:59Z").status_code == 200
    clock.advance(61)
    assert a.get("/api/sports/football/events?from=2027-01-20T00:00:00Z&to=2027-01-20T23:59:59Z").status_code == 200


def test_ai_reports_a_busy_data_source_not_a_busy_ai(two_users) -> None:
    a, b, t = two_users
    t.default = HttpResponse(404, {}, b"")
    a.get("/api/sports/event/fd:900")
    a.get("/api/sports/event/fd:901")
    r = a.get("/api/ai/analysis?event_id=fd:902")
    assert r.status_code == 503 and r.json()["error"] == "source_unavailable" and r.json()["scope"] == "user"


# --- F5: repeated logins of one account cannot grow the sessions table ---------------------------

def test_ended_sessions_per_user_are_bounded(tmp_path) -> None:
    import sqlite3

    from miniapp.store import ENDED_SESSIONS_KEPT_PER_USER, BurnWindow, Store
    from miniapp.telegram_auth import TelegramUser

    store = Store(tmp_path / "m.db")
    store.init_schema()
    for uid in (5, 6):
        store.upsert_user(TelegramUser(uid, "x", None, None, None), now=1)
    store.create_session("other-user-token", 6, "b" * 64, now=1, ttl_s=10_000)
    burn = BurnWindow(fresh_since=0, until=100_000)
    last = ""
    for i in range(200):                               # every login evicts the previous one (cap 1)
        last = f"token-{i:04d}-for-a-test"
        store.create_session(last, 5, f"{i:064x}", now=10 + i, ttl_s=10_000, max_active=1, burn=burn)
    with sqlite3.connect(tmp_path / "m.db") as con:
        rows = con.execute("SELECT count(*) FROM sessions WHERE user_id = 5").fetchone()[0]
    assert rows == ENDED_SESSIONS_KEPT_PER_USER + 1     # the active one plus the newest ended ones
    assert store.get_session(last).revoked_at is None
    assert store.get_session("token-0000-for-a-test") is None     # oldest row gone: reads as no session
    assert store.get_session("other-user-token").revoked_at is None
    assert store.is_init_data_consumed(f"{0:064x}", now=300)       # its launch stays burned


def test_login_limit_defaults_are_per_hour(tmp_path) -> None:
    s = base_settings(tmp_path)
    assert (s.login_rate_limit, s.login_rate_window_s) == (20, 3600)


def test_ai_source_rate_limit_is_its_own_reason(tmp_path, clock: FakeClock) -> None:
    from miniapp.sports.client import UpstreamError
    from miniapp.tests.conftest import ORIGIN

    class SourceBusy:
        connected = True

        async def analyse(self, event_id, user_id, *, create=True):
            raise UpstreamError("rate_limited", retry_after=30)

    app = create_app(base_settings(tmp_path, session_ttl_s=3600), clock=clock, ai=SourceBusy())
    c = TestClient(app, base_url="http://127.0.0.1:8000")
    r = login(c, fresh(clock))
    csrf = r.json()["session"]["csrf_token"]
    for resp in (c.get("/api/ai/analysis?event_id=fd:1"),
                 c.post("/api/ai/analysis", json={"event_id": "fd:1"}, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})):
        assert resp.status_code == 503
        assert resp.json() == {"error": "source_unavailable", "reason": "rate_limited", "retryAfter": 30}
        assert resp.headers["Retry-After"] == "30"


def test_quick_and_ai_are_gated_by_the_same_share(two_users, clock: FakeClock) -> None:
    a, b, t = two_users
    t.default = t.ok(day_payload(match(1)))
    for d in (15, 16, 17):
        assert a.get(f"/api/sports/football/events?from=2027-01-{d}T00:00:00Z&to=2027-01-{d}T23:59:59Z").status_code == 200
    calls = len(t.calls)
    span = f"from={DAY}T08:00:00Z&to=2027-01-18T08:00:00Z"   # 15-17 cached, 18 cold
    q = a.get(f"/api/quick/pick?mode=single&{span}")
    assert q.status_code == 200                    # cached first day: admitted; cold steps skipped, not paid
    assert len(t.calls) == calls                   # the share was full: no provider request for day 18
    q = a.get("/api/quick/pick?mode=single&from=2027-01-18T08:00:00Z&to=2027-01-19T08:00:00Z")
    assert q.status_code == 503 and q.json()["scope"] == "user"   # its first day is cold: refused
    r = a.get("/api/ai/analysis?event_id=fd:1")
    assert r.status_code == 503 and r.json()["scope"] == "user"
    assert b.get(f"/api/quick/pick?mode=single&{span}").status_code != 503


def test_an_expired_admission_never_frees_a_newer_one() -> None:
    clock = FakeClock()
    lim = UserUpstreamLimiter(2, 2, clock)
    old = UserShare(lim, 1)
    assert old.gate() is None
    clock.advance(IN_FLIGHT + 1)                   # it stopped counting
    newer = [UserShare(lim, 1) for _ in range(2)]
    assert all(s.gate() is None for s in newer)
    with old.tally():
        pass                                       # the old request finally ends
    assert UserShare(lim, 1).gate() is not None    # both newer ones still hold their places


def test_joining_a_running_load_needs_room_in_the_share(clock: FakeClock) -> None:
    import asyncio

    from miniapp.tests.test_sports import make_service

    service = make_service(FakeTransport(), clock)
    key = f"football:day:{DAY}"
    loop = asyncio.new_event_loop()
    try:
        service.cache._inflight[key] = loop.create_future()     # a (background) load is running
        assert service._user_cost(key) == 1
        assert service.day_cost(__import__("datetime").date(2027, 1, 15)) == 0   # quick's own view unchanged
    finally:
        service.cache._inflight.pop(key).cancel()
        loop.close()


def test_default_share_fits_an_ai_screen(tmp_path) -> None:
    """14.11 in production: with a share of 6 a cold AI analysis after any browsing was refused (empty cache
    after a deploy). The default is a quarter of the app's cap, at least one cold analysis, never above the cap."""
    from miniapp.settings import USER_SHARE_MIN, SettingsError
    from miniapp.sports.service import AI_EVIDENCE_REQUESTS

    assert USER_SHARE_MIN == AI_EVIDENCE_REQUESTS
    prod = base_settings(tmp_path, football_data_per_minute=60, football_data_live_plan=True)
    assert prod.user_share_per_minute == 15
    assert base_settings(tmp_path).user_share_per_minute == 6          # free tier (8/min): one cold analysis
    assert base_settings(tmp_path, football_data_per_minute=3).user_share_per_minute == 3   # never above the cap
    assert base_settings(tmp_path, sports_user_per_minute=4).user_share_per_minute == 4
    with pytest.raises(SettingsError):
        base_settings(tmp_path, sports_user_per_minute=9)               # above the app's 8/min
    # Production after a restart: a match page with its calendar and table, then a cold AI analysis.
    lim = UserUpstreamLimiter(prod.user_share_per_minute, 5, FakeClock())
    browse = UserShare(lim, 1)
    assert browse.gate(3) is None
    with browse.tally() as tally:
        for _ in range(3):
            tally.count += 1
            tally.charge()
    assert UserShare(lim, 1).gate(AI_EVIDENCE_REQUESTS) is None


def test_the_share_comes_from_the_environment(tmp_path) -> None:
    from miniapp.settings import Settings
    from miniapp.tests.signing import TEST_BOT_TOKEN

    env = {"MINIAPP_BOT_TOKEN": TEST_BOT_TOKEN, "MINIAPP_DB_PATH": str(tmp_path / "m.db"),
           "MINIAPP_TRUSTED_ORIGINS": ORIGIN, "MINIAPP_ENV": "development",
           "MINIAPP_FOOTBALL_DATA_PER_MINUTE": "60", "MINIAPP_FOOTBALL_DATA_LIVE_PLAN": "true"}
    assert Settings.from_env(env).user_share_per_minute == 15
    assert Settings.from_env({**env, "MINIAPP_SPORTS_USER_PER_MINUTE": ""}).user_share_per_minute == 15
    assert Settings.from_env({**env, "MINIAPP_SPORTS_USER_PER_MINUTE": "10"}).user_share_per_minute == 10


# --- round 2: admissions reserve their expected cost ---------------------------------------------

def test_an_admission_reserves_its_cost_until_its_requests_use_it_up() -> None:
    clock = FakeClock()
    lim = UserUpstreamLimiter(6, 2, clock)
    cold = UserShare(lim, 1)
    assert cold.gate(6) is None                    # a cold analysis reserves the whole share
    assert UserShare(lim, 1).gate(1) is not None   # nothing else fits while it runs
    with cold.tally() as tally:
        for _ in range(2):                         # two real requests come out of its reservation
            tally.count += 1
            tally.charge()
        assert UserShare(lim, 1).gate(1) is not None   # 2 in the window + 4 still reserved
    assert UserShare(lim, 1).gate(4) is None       # ended: only its 2 real requests remain
    assert UserShare(lim, 1).gate(1) is not None   # 2 + 4 reserved = the share


def test_parallel_cold_analyses_cannot_take_the_shared_budget() -> None:
    lim = UserUpstreamLimiter(6, 2, FakeClock())
    admitted = [s for s in (UserShare(lim, 1) for _ in range(6)) if s.gate(6) is None]
    assert len(admitted) == 1                      # was 6 when each admission held a single place


def test_a_cost_above_the_share_runs_only_when_the_user_is_idle() -> None:
    clock = FakeClock()
    lim = UserUpstreamLimiter(3, 2, clock)
    assert UserShare(lim, 1).gate(6) is None       # idle: allowed to overshoot by its own cost
    lim.charge(1)
    big = UserShare(lim, 2)
    lim.charge(2)
    wait = big.gate(6)
    assert wait is not None and wait == 60         # waits until its window is empty
    clock.advance(61)
    assert big.gate(6) is None


def test_a_cold_never_cached_match_counts_every_evidence_piece(clock: FakeClock) -> None:
    from miniapp.sports.service import AI_EVIDENCE_REQUESTS
    from miniapp.tests.test_sports import make_service

    service = make_service(FakeTransport(), clock)
    assert service.analysis_cost("fd:424242") == (AI_EVIDENCE_REQUESTS, True)
    assert service.analysis_cost("not-an-id") == (0, False)


def test_create_after_the_screen_loaded_the_evidence_is_not_refused(tmp_path, clock: FakeClock) -> None:
    """Codex round 1: the event lookup and the read made the share full; Create must still run, because
    everything it reads is cached now (cost 0)."""
    from miniapp.ai.analysis import build_ai_service
    from miniapp.tests.test_ai_analysis import FakeForecaster, FakeModel, arsenal_form, chelsea_form, standings

    t = FakeTransport()
    settings = base_settings(tmp_path, football_data_key=KEY, sports_user_per_minute=4)
    sports = build_service(settings, clock=clock, transport=t, warm_emblems=False)
    sports.football.client._sleep = no_sleep
    sports.history_cost = lambda tournament_id, **_: 0  # the fake forecaster reads no seasons
    ai = build_ai_service(settings, sports, clock=clock, client=FakeModel(), forecaster=FakeForecaster())
    client = TestClient(create_app(settings, clock=clock, sports=sports, ai=ai), base_url="http://127.0.0.1:8000")
    csrf = login(client, fresh(clock)).json()["session"]["csrf_token"]
    t.script = [t.ok(match(7)), t.ok(arsenal_form()), t.ok(chelsea_form()), t.ok(standings())]
    assert client.get("/api/sports/event/fd:7").status_code == 200       # 1 request
    assert client.get("/api/ai/analysis?event_id=fd:7").status_code == 404  # forms + table: the share is full
    assert len(t.calls) == 4
    r = client.post("/api/ai/analysis", json={"event_id": "fd:7"},
                    headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    assert len(t.calls) == 4                       # served from the cache



# --- round 3: a load outliving its request; step-by-step reservations; retry-after -----------------

def test_a_load_outliving_its_request_keeps_the_reservation(clock: FakeClock) -> None:
    """Codex round 2: the request ended, its cache load still runs — a second cold analysis must not
    reserve the share again until that load is done."""
    import asyncio

    from miniapp.sports.cache import SportsCache

    lim = UserUpstreamLimiter(6, 2, clock)
    cache = SportsCache(clock=clock)
    gate = asyncio.Event()

    async def scenario() -> None:
        async def slow():
            await gate.wait()
            return "v", 60.0
        first = UserShare(lim, 1)
        assert first.gate(6) is None
        with first.tally():
            cache.start("k", slow)                 # the request leaves; the load goes on
        assert UserShare(lim, 1).gate(6) is not None   # still reserved by the running load
        gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert UserShare(lim, 1).gate(6) is None   # load done: the reservation is free

    asyncio.run(scenario())


def test_take_reserves_steps_without_overshoot_and_uses_the_admission_first() -> None:
    lim = UserUpstreamLimiter(6, 2, FakeClock())
    share = UserShare(lim, 1)
    assert share.gate(1) is None                   # admitted for its first step
    assert share.take(1) is True                   # that step: the admission's own reservation
    assert share.take(4) is True                   # 1 + 4 reserved
    assert share.take(2) is False                  # would pass the share: the step is skipped
    assert share.take(1) is True                   # exactly the share
    assert UserShare(lim, 1).gate(1) is not None


def test_retry_after_comes_from_the_window_when_nothing_is_reserved(clock: FakeClock) -> None:
    lim = UserUpstreamLimiter(2, 2, clock)
    running = UserShare(lim, 1)
    assert running.gate(2) is None
    with running.tally() as tally:
        for _ in range(2):
            tally.count += 1
            tally.charge()                         # its whole reservation used up
        clock.advance(10)
        wait = UserShare(lim, 1).gate(1)
        assert wait is not None and wait > 40      # the window decides, not "5 s"


def test_a_stale_calendar_and_a_removed_match_count_both_requests(clock: FakeClock) -> None:
    from miniapp.tests.test_sports import make_service

    service = make_service(FakeTransport(), clock)
    service._calendar_index["fd:77"] = "fd:PL"
    cost, unknown = service.event_cost("fd:77")
    assert (cost, unknown) == (2, False)           # cold calendar + the match's own lookup



# --- round 4: retries, joined loads, the first day, credit ----------------------------------------

def test_a_retry_needs_room_in_the_share(clock: FakeClock) -> None:
    """Codex round 3: transient failures retried past the user's share."""
    import asyncio

    from miniapp.tests.test_sports import make_client

    t = FakeTransport()
    t.script = [HttpResponse(500, {"x-requests-available-minute": "90"}, b"{}")] * 3 + [t.ok({})]
    client = make_client(t, clock)
    lim = UserUpstreamLimiter(2, 2, clock)
    share = UserShare(lim, 1)
    assert share.gate(1) is None

    async def go() -> None:
        with share.tally():
            await client.get_json("/v4/x")
    with pytest.raises(Exception):
        asyncio.run(go())
    assert len(t.calls) == 2                       # one retry fitted the share of 2, the next did not


def test_a_failed_step_keeps_the_admission_credit() -> None:
    lim = UserUpstreamLimiter(3, 2, FakeClock())
    share = UserShare(lim, 1)
    assert share.gate(2) is None                   # 2 reserved at admission
    assert share.take(1) is True                   # 1 of the credit
    assert share.take(3) is False                  # does not fit: the remaining credit stays
    assert share.take(1) is True                   # served from that credit


def test_the_quick_first_cost_is_only_the_first_utc_day(clock: FakeClock) -> None:
    import asyncio
    from datetime import UTC, datetime

    from miniapp.quick.service import QuickService
    from miniapp.tests.test_sports import make_service

    t = FakeTransport()
    t.default = t.ok(day_payload(match(1)))
    sports = make_service(t, clock)
    today = datetime(2027, 1, 15, tzinfo=UTC)
    asyncio.run(sports.events("football", today, datetime(2027, 1, 16, tzinfo=UTC), None))  # today cached
    quick = QuickService(sports, clock=clock)
    start, end = datetime(2027, 1, 15, 8, tzinfo=UTC), datetime(2027, 1, 16, 8, tzinfo=UTC)
    assert quick.first_cost(start, end) == 0       # tomorrow's cold day is not part of the first step
