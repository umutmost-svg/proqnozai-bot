"""Standings warm-up and the rate-limit wait (Retry-After) — offline, provider faked."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from miniapp.app import create_app
from miniapp.sports import service as service_module
from miniapp.sports.cache import SportsCache
from miniapp.sports.client import LOW_PRIORITY_RESERVE, HttpResponse, UpstreamError
from miniapp.sports.football_data import FootballDataAdapter
from miniapp.sports.service import STANDINGS_REFRESH_AFTER_S, STANDINGS_WARM_SPACING_S, SportsService
from miniapp.tests.conftest import FakeClock, fresh, login
from miniapp.tests.test_sports import KEY, FakeTransport, base_settings, make_client, run
from miniapp.tests.test_standings import LEAGUE, block, row, table

PD_TABLE = table(block(row(1, 81, "FC Barcelona", 33), row(2, 86, "Real Madrid CF", 30)), code="PD")


def warm_service(t: FakeTransport, clock: FakeClock, *, warm: bool = True, **limits) -> tuple[SportsService, list]:
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    adapter = FootballDataAdapter(make_client(t, clock, **limits), ("PL", "PD"), clock)
    service = SportsService(adapter, cache=SportsCache(clock=clock), clock=clock, warm_emblems=False,
                            warm_standings=warm, sleep=sleep)
    return service, slept


def standings_calls(t: FakeTransport) -> list[str]:
    return [url.split("/competitions/")[1].split("/")[0] for url, _ in t.calls if url.endswith("/standings")]


async def warm_once(service: SportsService) -> None:
    task = service.ensure_standings_warm()
    if task is not None:
        await task


def test_a_pass_loads_every_missing_table_one_at_a_time(clock: FakeClock) -> None:
    t = FakeTransport()
    service, slept = warm_service(t, clock)
    t.script = [t.ok(LEAGUE), t.ok(PD_TABLE)]
    run(warm_once(service))
    assert standings_calls(t) == ["PL", "PD"] and slept == [STANDINGS_WARM_SPACING_S]
    # Both tables are now served from the cache: a user's table costs no request.
    result = run(service.standings_table("football", "fd:PD"))
    assert result is not None and not result.stale and len(t.calls) == 2
    # Nothing due: no new pass, no request.
    assert run(_ensure(service)) is None and len(t.calls) == 2


async def _ensure(service: SportsService):
    return service.ensure_standings_warm()


def test_tables_are_refreshed_before_they_expire_and_kept_if_the_refresh_fails(clock: FakeClock) -> None:
    t = FakeTransport()
    service, _ = warm_service(t, clock)
    t.script = [t.ok(LEAGUE), t.ok(PD_TABLE)]
    run(warm_once(service))
    clock.advance(STANDINGS_REFRESH_AFTER_S - 60)
    assert run(_ensure(service)) is None  # not due yet
    clock.advance(120)
    # The early refresh of PL fails at the provider: the table still in date is served as it was.
    t.script = [HttpResponse(500, {}, b"")] * 3 + [t.ok(PD_TABLE)]
    run(warm_once(service))
    pl = run(service.standings_table("football", "fd:PL"))
    assert pl is not None and pl.stale is False
    assert standings_calls(t).count("PD") == 2


def test_the_warm_up_leaves_the_minute_to_users(clock: FakeClock) -> None:
    t = FakeTransport()
    service, _ = warm_service(t, clock, per_minute=8)
    t.default = t.ok(LEAGUE)
    client = service.football.client
    for _ in range(5):  # 3 left in this minute: exactly the reserve for interactive requests
        client._reserve()
    run(warm_once(service))
    assert t.calls == []
    assert client.minute_headroom() == 3


def test_the_warm_up_stops_at_half_of_the_daily_budget(clock: FakeClock) -> None:
    t = FakeTransport()
    service, _ = warm_service(t, clock, per_minute=8, per_day=10)
    t.default = t.ok(LEAGUE)
    client = service.football.client
    for _ in range(5):
        client._reserve()
    clock.advance(61)  # the minute is free again, the day is half spent
    run(warm_once(service))
    assert t.calls == []


def test_its_requests_keep_the_low_priority_reserve(clock: FakeClock, monkeypatch) -> None:
    t = FakeTransport()
    service, _ = warm_service(t, clock)
    seen: list[int] = []
    original = service.football.standings

    async def spy(code: str):
        seen.append(LOW_PRIORITY_RESERVE.get())
        return await original(code)

    monkeypatch.setattr(service.football, "standings", spy)
    t.script = [t.ok(LEAGUE), t.ok(PD_TABLE)]
    run(warm_once(service))
    assert seen == [service_module.STANDINGS_WARM_RESERVE] * 2
    assert LOW_PRIORITY_RESERVE.get() == 0


def test_a_remembered_failure_is_not_retried_by_the_warm_up(clock: FakeClock) -> None:
    t = FakeTransport()
    service, _ = warm_service(t, clock)
    t.script = [HttpResponse(500, {}, b"")] * 3
    with pytest.raises(UpstreamError):
        run(service.standings_table("football", "fd:PL"))
    assert standings_calls(t) == ["PL"] * 3
    t.script = [t.ok(PD_TABLE)]
    run(warm_once(service))
    assert standings_calls(t) == ["PL"] * 3 + ["PD"]


def test_off_by_default_and_without_a_provider(clock: FakeClock) -> None:
    t = FakeTransport()
    service, _ = warm_service(t, clock, warm=False)
    assert run(_ensure(service)) is None and t.calls == []
    assert run(_ensure(SportsService(None, clock=clock, warm_standings=True))) is None


def test_a_rate_limited_refresh_after_expiry_serves_the_last_table_marked_stale(clock: FakeClock) -> None:
    t = FakeTransport()
    service, _ = warm_service(t, clock, warm=False)
    t.script = [t.ok(LEAGUE)]
    run(service.standings_table("football", "fd:PL"))
    clock.advance(31 * 60)
    t.script = [HttpResponse(429, {"retry-after": "30"}, b"")]
    result = run(service.standings_table("football", "fd:PL"))
    assert result is not None and result.stale is True


# --- the wait --------------------------------------------------------------------------------------

def test_the_limiter_says_how_long_until_it_admits_again(clock: FakeClock) -> None:
    client = make_client(FakeTransport(), clock, per_minute=3)
    client._reserve()
    clock.advance(10)
    client._reserve()
    client._reserve()
    with pytest.raises(UpstreamError) as e:
        client._reserve()
    assert e.value.code == "rate_limited" and e.value.retry_after == pytest.approx(50)
    token = LOW_PRIORITY_RESERVE.set(1)
    try:
        with pytest.raises(UpstreamError) as e:
            client._reserve()
        # One more must age out for a low-priority request: the second one, 60 s after it was made.
        assert e.value.retry_after == pytest.approx(60)
    finally:
        LOW_PRIORITY_RESERVE.reset(token)


def test_a_provider_429_waits_its_retry_after(clock: FakeClock) -> None:
    t = FakeTransport()
    client = make_client(t, clock)
    t.script = [HttpResponse(429, {"retry-after": "42"}, b"")]
    with pytest.raises(UpstreamError) as e:
        run(client.get_json("/competitions/PL/standings"))
    assert e.value.retry_after == pytest.approx(42)
    clock.advance(2)
    with pytest.raises(UpstreamError) as e:
        client._reserve()
    assert e.value.retry_after == pytest.approx(40)


def test_a_local_refusal_is_remembered_only_until_the_limiter_admits(clock: FakeClock) -> None:
    cache = SportsCache(clock=clock, failure_backoff_s=30)
    calls: list[int] = []

    async def refused():
        calls.append(1)
        raise UpstreamError("rate_limited", retry_after=5)

    async def ok():
        calls.append(2)
        return "table", 60

    with pytest.raises(UpstreamError) as e:
        run(cache.get("k", refused))
    clock.advance(2)
    with pytest.raises(UpstreamError) as e:
        run(cache.get("k", ok))
    assert e.value.retry_after == pytest.approx(3) and calls == [1]
    clock.advance(3)
    assert run(cache.get("k", ok)).value == "table"


def test_a_provider_refusal_keeps_the_full_backoff_and_reports_the_longer_wait(clock: FakeClock) -> None:
    cache = SportsCache(clock=clock, failure_backoff_s=30)

    async def refused():
        raise UpstreamError("rate_limited", status=429, retry_after=45)

    with pytest.raises(UpstreamError):
        run(cache.get("k", refused))
    clock.advance(10)
    with pytest.raises(UpstreamError) as e:
        run(cache.get("k", refused))
    assert e.value.retry_after == pytest.approx(35)


# --- HTTP ------------------------------------------------------------------------------------------

def test_signed_in_requests_start_the_warm_up(tmp_path, clock: FakeClock) -> None:
    t = FakeTransport()
    service, _ = warm_service(t, clock)
    t.default = t.ok(LEAGUE)
    started: list[int] = []
    real = service.ensure_standings_warm

    def spy():
        started.append(1)
        return real()

    service.ensure_standings_warm = spy  # type: ignore[method-assign]
    settings = base_settings(tmp_path, football_data_key=KEY, session_ttl_s=3600)
    client = TestClient(create_app(settings, clock=clock, sports=service), base_url="http://127.0.0.1:8000")
    assert client.get("/api/sports/football/tournaments").status_code == 401 and started == []
    login(client, fresh(clock))
    assert client.get("/api/sports/football/tournaments").status_code == 200 and started == [1]


def test_the_app_builds_its_own_service_with_the_warm_up(tmp_path, clock: FakeClock, monkeypatch) -> None:
    import miniapp.app as app_module
    seen: dict = {}
    real = app_module.build_service

    def spy(settings, **kw):
        seen.update(kw)
        return real(settings, **kw)

    monkeypatch.setattr(app_module, "build_service", spy)
    create_app(base_settings(tmp_path, football_data_key=KEY, session_ttl_s=3600), clock=clock)
    assert seen.get("warm_standings") is True


def test_the_retry_after_never_exceeds_the_cap() -> None:
    from miniapp.sports.routes import MAX_RETRY_AFTER_S, _upstream
    r = _upstream(UpstreamError("rate_limited", retry_after=10 ** 6))
    assert r.headers["retry-after"] == str(MAX_RETRY_AFTER_S)
    r = _upstream(UpstreamError("rate_limited", retry_after=0.2))
    assert r.headers["retry-after"] == "1"
    r = _upstream(UpstreamError("timeout"))
    assert "retry-after" not in r.headers


def test_a_failed_warm_refresh_is_not_asked_again_before_its_pause(clock: FakeClock) -> None:
    t = FakeTransport()
    service, _ = warm_service(t, clock)
    t.script = [t.ok(LEAGUE), t.ok(PD_TABLE)]
    run(warm_once(service))
    clock.advance(STANDINGS_REFRESH_AFTER_S + 1)
    t.script = [HttpResponse(500, {}, b"")] * 3 + [t.ok(PD_TABLE)]
    run(warm_once(service))  # PL kept as it was (fresh), PD refreshed
    pl_calls = standings_calls(t).count("PL")
    for _ in range(3):  # signed-in requests keep coming
        clock.advance(60)
        run(warm_once(service))
    assert standings_calls(t).count("PL") == pl_calls
    clock.advance(service_module.STANDINGS_WARM_RETRY_S)
    t.script = [t.ok(LEAGUE)]
    run(warm_once(service))
    assert standings_calls(t).count("PL") == pl_calls + 1


def test_a_table_the_source_does_not_have_is_left_alone_for_hours(clock: FakeClock) -> None:
    t = FakeTransport()
    service, _ = warm_service(t, clock)
    t.script = [HttpResponse(404, {}, b""), t.ok(PD_TABLE)]
    run(warm_once(service))
    clock.advance(31 * 60)
    t.script = [t.ok(PD_TABLE)]
    run(warm_once(service))
    assert standings_calls(t) == ["PL", "PD", "PD"]


def test_every_attempt_of_the_warm_up_stops_at_its_share_of_the_day(clock: FakeClock) -> None:
    t = FakeTransport()
    service, _ = warm_service(t, clock, per_minute=8, per_day=4)
    service.football.client._reserve()  # 1 of 4 used: the warm-up may reach 2, never more
    t.default = HttpResponse(500, {}, b"")
    run(warm_once(service))
    assert len(t.calls) == 1
    usage = service.football.client.usage()
    assert usage["usedToday"] == 2 and service.football.client.last_error != "quota_exhausted"
    # A user's request still has the rest of the day.
    t.default = t.ok(PD_TABLE)
    assert run(service.standings_table("football", "fd:PD")) is not None


@pytest.mark.parametrize(("per_day", "used", "allowed"), [(3, 0, 1), (3, 1, 0), (1, 0, 0), (5, 1, 1)])
def test_the_share_of_the_day_is_whole_requests_rounded_down(clock: FakeClock, per_day, used, allowed) -> None:
    client = make_client(FakeTransport(), clock, per_day=per_day)
    for _ in range(used):
        client._reserve()
    token = service_module.LOW_PRIORITY_DAY_SHARE.set(0.5)
    try:
        admitted = 0
        for _ in range(3):
            try:
                client._reserve()
                admitted += 1
            except UpstreamError as e:
                assert e.code == "rate_limited" and e.low_priority_only
        assert admitted == allowed
    finally:
        service_module.LOW_PRIORITY_DAY_SHARE.reset(token)
    assert client.last_error is None


def test_a_background_only_refusal_is_never_remembered_for_users(clock: FakeClock) -> None:
    t = FakeTransport()
    service, _ = warm_service(t, clock, per_minute=8, per_day=4)
    service.football.client._reserve()
    t.script = [HttpResponse(500, {}, b"")]  # the warm-up's one allowed attempt fails, its retry is refused
    run(warm_once(service))
    assert standings_calls(t) == ["PL"]
    t.script = [t.ok(LEAGUE)]
    result = run(service.standings_table("football", "fd:PL"))  # at once: nothing remembered
    assert result is not None and standings_calls(t) == ["PL", "PL"]


def test_the_minute_reserve_refusal_is_not_remembered_either(clock: FakeClock) -> None:
    t = FakeTransport()
    service, _ = warm_service(t, clock, warm=False, per_minute=4)
    client = service.football.client
    for _ in range(2):
        client._reserve()
    token = LOW_PRIORITY_RESERVE.set(3)
    try:
        with pytest.raises(UpstreamError) as e:
            run(service.standings_table("football", "fd:PL"))
        assert e.value.low_priority_only
    finally:
        LOW_PRIORITY_RESERVE.reset(token)
    t.script = [t.ok(LEAGUE)]
    assert run(service.standings_table("football", "fd:PL")) is not None


def test_a_remembered_local_refusal_ends_exactly_when_the_limiter_admits(clock: FakeClock) -> None:
    cache = SportsCache(clock=clock, failure_backoff_s=30)

    async def refused():
        raise UpstreamError("rate_limited", retry_after=0.2)

    async def ok():
        return "table", 60

    with pytest.raises(UpstreamError):
        run(cache.get("k", refused))
    clock.advance(0.3)
    assert run(cache.get("k", ok)).value == "table"


def test_a_user_joining_a_background_load_refused_by_its_own_limits_loads_it_again(clock: FakeClock) -> None:
    cache = SportsCache(clock=clock)
    started: list[str] = []
    gate = None

    async def load():
        mode = "background" if LOW_PRIORITY_RESERVE.get() else "user"
        started.append(mode)
        if mode == "background":
            await gate.wait()
            raise UpstreamError("rate_limited", low_priority_only=True)
        return "table", 60

    async def scenario():
        nonlocal gate
        gate = asyncio.Event()
        token = LOW_PRIORITY_RESERVE.set(3)
        try:
            background = cache.start("k", load)
        finally:
            LOW_PRIORITY_RESERVE.reset(token)
        user = asyncio.create_task(cache.get("k", load))  # joins the running background load
        await asyncio.sleep(0)
        gate.set()
        with pytest.raises(UpstreamError):
            await background
        return await user

    assert run(scenario()).value == "table"
    assert started == ["background", "user"]


def test_a_background_caller_joining_keeps_the_refusal(clock: FakeClock) -> None:
    cache = SportsCache(clock=clock)

    async def load():
        raise UpstreamError("rate_limited", low_priority_only=True)

    async def scenario():
        token = LOW_PRIORITY_RESERVE.set(3)
        try:
            with pytest.raises(UpstreamError):
                await cache.get("k", load)
        finally:
            LOW_PRIORITY_RESERVE.reset(token)

    run(scenario())


def test_an_expired_table_is_not_held_stale_after_a_background_only_refusal(clock: FakeClock) -> None:
    cache = SportsCache(clock=clock)

    async def first():
        return "old", 60

    async def refused():
        raise UpstreamError("rate_limited", low_priority_only=True)

    async def fresh_load():
        return "new", 60

    run(cache.get("k", first))
    clock.advance(61)
    with pytest.raises(UpstreamError):
        run(cache.get("k", refused))
    entry = run(cache.get("k", fresh_load))
    assert entry.value == "new" and entry.stale is False


@pytest.mark.parametrize("per_minute", [1, 3])
def test_a_minute_limit_at_or_below_the_reserve_refuses_cleanly(clock: FakeClock, per_minute) -> None:
    client = make_client(FakeTransport(), clock, per_minute=per_minute)
    token = LOW_PRIORITY_RESERVE.set(3)
    try:
        with pytest.raises(UpstreamError) as e:
            client._reserve()
    finally:
        LOW_PRIORITY_RESERVE.reset(token)
    assert e.value.code == "rate_limited" and e.value.low_priority_only
    client._reserve()  # a user is still admitted


def test_an_expired_value_is_held_stale_only_until_the_limiter_admits(clock: FakeClock) -> None:
    cache = SportsCache(clock=clock, failure_backoff_s=30)

    async def first():
        return "old", 60

    async def refused():
        raise UpstreamError("rate_limited", retry_after=1)

    async def fresh_load():
        return "new", 60

    run(cache.get("k", first))
    clock.advance(61)
    held = run(cache.get("k", refused))
    assert held.value == "old" and held.stale is True
    clock.advance(1)
    entry = run(cache.get("k", fresh_load))
    assert entry.value == "new" and entry.stale is False


def test_a_provider_failure_still_holds_the_stale_value_for_the_backoff(clock: FakeClock) -> None:
    cache = SportsCache(clock=clock, failure_backoff_s=30)
    calls: list[int] = []

    async def first():
        return "old", 60

    async def failing():
        calls.append(1)
        raise UpstreamError("rate_limited", status=429, retry_after=5)

    run(cache.get("k", first))
    clock.advance(61)
    assert run(cache.get("k", failing)).stale is True
    clock.advance(20)
    assert run(cache.get("k", failing)).stale is True and calls == [1]
