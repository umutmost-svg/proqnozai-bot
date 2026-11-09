"""Automatic settlement of the forecast history (internal tracking) — offline, provider faked."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from miniapp import admin
from miniapp.app import create_app
from miniapp.forecast.history import ForecastHistory, history_path
from miniapp.forecast.settlement import CHECK_EVERY_S, RETRY_S, SPACING_S, Settlement
from miniapp.sports import service as service_module
from miniapp.sports.client import LOW_PRIORITY_RESERVE, UpstreamError
from miniapp.tests.conftest import NOW, ORIGIN, FakeClock, fresh, login
from miniapp.tests.signing import TEST_BOT_TOKEN
from miniapp.tests.test_forecast_history import event, record
from miniapp.tests.test_sports import KEY, FakeTransport, base_settings, match
from miniapp.tests.test_standings_warm import warm_service
from miniapp.tests.test_tournaments_crests import season_payload

# record(): fd:7 of fd:PL, kickoff 2027-01-14T15:00:00Z = NOW - 17 h (inside the pass window).


@pytest.fixture()
def history(tmp_path, clock: FakeClock) -> ForecastHistory:
    h = ForecastHistory(tmp_path / "m.forecasts.db", clock)
    h.init_schema()
    return h


def status(history: ForecastHistory) -> dict[str, int]:
    return history.status_counts()


class FakeSports:
    def __init__(self, results: list[object], served: tuple[str, ...] = ("fd:PL",)) -> None:
        self.results = results
        self.served = served
        self.calls: list[str] = []

    def tournaments(self, sport: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(id=t) for t in self.served]

    async def background_calendar(self, tournament_id: str):
        self.calls.append(tournament_id)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def calendar(*events) -> SimpleNamespace:
    return SimpleNamespace(events=list(events))


def make(history: ForecastHistory, sports: FakeSports, clock: FakeClock) -> tuple[Settlement, list[float]]:
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    return Settlement(history, sports, clock, sleep=sleep), slept  # type: ignore[arg-type]


async def pass_once(s: Settlement) -> bool:
    task = s.ensure()
    if task is None:
        return False
    await task
    return True


def test_a_loaded_calendar_settles_its_forecasts_without_a_request(history, clock: FakeClock) -> None:
    history.record(record())
    sports = FakeSports([])
    s, _ = make(history, sports, clock)

    async def go() -> None:
        s.on_calendar("fd:PL", [event()])
        await asyncio.gather(*s._jobs)

    asyncio.run(go())
    assert status(history) == {"pending": 0, "settled": 1, "void": 0} and sports.calls == []


def test_the_pass_asks_once_for_a_competition_with_overdue_forecasts(history, clock: FakeClock) -> None:
    history.record(record())
    sports = FakeSports([calendar(event())])
    s, _ = make(history, sports, clock)
    assert asyncio.run(pass_once(s)) is True
    assert sports.calls == ["fd:PL"] and status(history)["settled"] == 1
    # Checked at most every CHECK_EVERY_S; then nothing is pending: no further request.
    assert asyncio.run(pass_once(s)) is False
    clock.advance(CHECK_EVERY_S)
    asyncio.run(pass_once(s))
    assert sports.calls == ["fd:PL"]


@pytest.mark.parametrize("hours_since_kickoff", [2.5, 15 * 24])
def test_matches_too_recent_or_too_old_cost_no_request(tmp_path, hours_since_kickoff: float) -> None:
    kickoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - hours_since_kickoff * 3600))
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - hours_since_kickoff * 3600 - 7200))
    clock = FakeClock()
    h = ForecastHistory(tmp_path / "m.forecasts.db", clock)
    h.init_schema()
    h.record(replace(record(created=created), kickoff_at=kickoff))
    sports = FakeSports([])
    s, _ = make(h, sports, clock)
    asyncio.run(pass_once(s))
    assert sports.calls == []


def test_a_postponed_match_is_asked_again_only_after_the_retry_interval(history, clock: FakeClock) -> None:
    history.record(record())
    sports = FakeSports([calendar(event(status="postponed", score=None))] * 3)
    s, _ = make(history, sports, clock)
    asyncio.run(pass_once(s))
    clock.advance(CHECK_EVERY_S)
    asyncio.run(pass_once(s))
    assert sports.calls == ["fd:PL"] and status(history)["pending"] == 1  # never a loss
    clock.advance(RETRY_S)
    asyncio.run(pass_once(s))
    assert sports.calls == ["fd:PL", "fd:PL"]


def test_a_calendar_a_user_loaded_spares_the_pass_its_request(history, clock: FakeClock) -> None:
    history.record(record())
    sports = FakeSports([])
    s, _ = make(history, sports, clock)

    async def go() -> None:
        s.on_calendar("fd:PL", [event(status="postponed", score=None)])
        await asyncio.gather(*s._jobs)
        task = s.ensure()
        assert task is not None
        await task

    asyncio.run(go())
    assert sports.calls == []


def test_an_exhausted_budget_stops_the_pass_and_the_next_check_retries(history, clock: FakeClock) -> None:
    history.record(record())
    history.record(record(key="k2", event="fd:9"))
    sports = FakeSports([None, calendar(event(), event(eid="fd:9"))], served=("fd:PL",))
    s, _ = make(history, sports, clock)
    asyncio.run(pass_once(s))
    assert sports.calls == ["fd:PL"] and status(history)["pending"] == 2
    clock.advance(CHECK_EVERY_S)
    asyncio.run(pass_once(s))
    assert sports.calls == ["fd:PL", "fd:PL"] and status(history)["settled"] == 2


def test_competitions_are_asked_one_at_a_time_and_failures_wait(tmp_path, clock: FakeClock) -> None:
    h = ForecastHistory(tmp_path / "m.forecasts.db", clock)
    h.init_schema()
    h.record(record())
    h.record(replace(record(key="k-pd", event="fd:8"), tournament_id="fd:PD"))
    h.record(replace(record(key="k-xx", event="fd:9"), tournament_id="fd:XX"))  # no longer served
    sports = FakeSports([UpstreamError("rate_limited"), calendar(event())],
                        served=("fd:PL", "fd:PD"))
    s, slept = make(h, sports, clock)
    asyncio.run(pass_once(s))
    assert sports.calls == ["fd:PD", "fd:PL"] and slept == [SPACING_S]
    assert h.status_counts()["settled"] == 1
    clock.advance(CHECK_EVERY_S)
    asyncio.run(pass_once(s))
    assert sports.calls == ["fd:PD", "fd:PL"]  # the failed one waits RETRY_S, the unserved one is never asked


def test_a_failing_history_never_breaks_the_pass(history, clock: FakeClock, monkeypatch) -> None:
    history.record(record())
    sports = FakeSports([calendar(event())])
    s, _ = make(history, sports, clock)

    def broken(*_):
        raise OSError("disk")

    monkeypatch.setattr(history, "settle_events", broken)
    asyncio.run(pass_once(s))
    assert s._task is None and status(history)["pending"] == 1


# --- with the real sports service --------------------------------------------------------

def finished_payload() -> dict:
    return season_payload(match(7, "FINISHED", utc="2027-01-14T15:00:00Z", full=(2, 1)))


def test_the_background_calendar_keeps_the_low_priority_reserve(history, clock: FakeClock, monkeypatch) -> None:
    history.record(record())
    t = FakeTransport()
    service, _ = warm_service(t, clock)
    seen: list[int] = []
    original = service.football.calendar

    async def spy(code: str):
        seen.append(LOW_PRIORITY_RESERVE.get())
        return await original(code)

    monkeypatch.setattr(service.football, "calendar", spy)
    t.script = [t.ok(finished_payload())]
    s = Settlement(history, service, clock)
    asyncio.run(pass_once(s))
    assert [url for url, _ in t.calls][0].endswith("/competitions/PL/matches")
    assert seen == [service_module.STANDINGS_WARM_RESERVE] and LOW_PRIORITY_RESERVE.get() == 0
    assert history.status_counts()["settled"] == 1


def test_the_background_calendar_leaves_the_minute_to_users(history, clock: FakeClock) -> None:
    history.record(record())
    t = FakeTransport()
    service, _ = warm_service(t, clock, per_minute=8)
    t.default = t.ok(finished_payload())
    for _ in range(5):
        service.football.client._reserve()
    s = Settlement(history, service, clock)
    asyncio.run(pass_once(s))
    assert t.calls == [] and history.status_counts()["pending"] == 1


def test_the_app_settles_from_a_calendar_a_user_opens(tmp_path, clock: FakeClock, monkeypatch) -> None:
    settings = base_settings(tmp_path, football_data_key=KEY, session_ttl_s=3600)
    h = ForecastHistory(history_path(settings.db_path), clock)
    h.init_schema()
    h.record(record())
    t = FakeTransport()
    service, _ = warm_service(t, clock, warm=False)
    t.default = t.ok(finished_payload())
    # The pass is switched off here: only the calendar listener can settle it.
    monkeypatch.setattr(Settlement, "ensure", lambda self: None)
    client = TestClient(create_app(settings, clock=clock, sports=service), base_url="http://127.0.0.1:8000")
    login(client, fresh(clock))
    with client:
        assert client.get("/api/sports/football/tournaments/fd:PL/calendar").status_code == 200
        # The listener's job runs on the app's loop thread: waited for, with a generous bound.
        deadline = time.monotonic() + 15
        while not h.status_counts()["settled"] and time.monotonic() < deadline:
            time.sleep(0.02)
    assert h.status_counts() == {"pending": 0, "settled": 1, "void": 0}
    assert sum(1 for url, _ in t.calls if url.endswith("/matches")) == 1


# --- admin --------------------------------------------------------------------------------

def snapshot(directory) -> dict[str, tuple[int, int]]:
    return {f.name: (f.stat().st_size, f.stat().st_mtime_ns) for f in directory.iterdir()}


def test_admin_reports_counts_and_quality_without_writing_anything(tmp_path, capsys) -> None:
    env = {"MINIAPP_BOT_TOKEN": TEST_BOT_TOKEN, "MINIAPP_DB_PATH": str(tmp_path / "m.db"),
           "MINIAPP_TRUSTED_ORIGINS": ORIGIN, "MINIAPP_ENV": "development"}
    assert admin.main(["forecasts"], env=env) == 2
    assert "not found" in capsys.readouterr().err and list(tmp_path.iterdir()) == []
    h = ForecastHistory(history_path(tmp_path / "m.db"))
    h.init_schema()
    h.record(record())
    h.record(record(key="k-nobet", event="fd:8", pick="NO_BET"))
    h.settle_events([event()])
    with sqlite3.connect(h.path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    # SQLite may add its own -wal/-shm sidecars for a read; the data files must stay byte-identical.
    before = snapshot(tmp_path)
    # Not even the main database is needed (or created): the command reads the history only.
    assert admin.main(["forecasts"], env=env) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["forecasts"] == {"pending": 1, "settled": 1, "void": 0}
    quality = out["quality"]["goals-1"]
    assert quality["forecasts"] == 1 and quality["picks"] == {"count": 1, "hitRate": 1.0}
    assert not (tmp_path / "m.db").exists()
    assert {k: v for k, v in snapshot(tmp_path).items() if k in before} == before


def test_admin_refuses_a_file_that_is_not_a_forecast_history(tmp_path, capsys) -> None:
    env = {"MINIAPP_BOT_TOKEN": TEST_BOT_TOKEN, "MINIAPP_DB_PATH": str(tmp_path / "m.db"),
           "MINIAPP_TRUSTED_ORIGINS": ORIGIN, "MINIAPP_ENV": "development"}
    with sqlite3.connect(history_path(tmp_path / "m.db")) as conn:
        conn.execute("CREATE TABLE users (id INTEGER)")
    assert admin.main(["forecasts"], env=env) == 2
    assert "not a forecast history" in capsys.readouterr().err


def test_ai_and_quick_activity_start_the_pass_too(tmp_path, clock: FakeClock, monkeypatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(Settlement, "ensure", lambda self: started.append("pass"))
    settings = base_settings(tmp_path, football_data_key=KEY, session_ttl_s=3600)
    t = FakeTransport()
    service, _ = warm_service(t, clock, warm=False)
    client = TestClient(create_app(settings, clock=clock, sports=service), base_url="http://127.0.0.1:8000")
    client.get("/api/ai/analysis?event_id=fd:1")
    assert started == []  # signed out: nothing starts
    login(client, fresh(clock))
    client.get("/api/ai/analysis?event_id=fd:1")
    assert started == ["pass"]
    client.get("/api/quick/pick?mode=single")
    assert started == ["pass", "pass"]


def test_the_background_calendar_waits_while_the_standings_warm_up_loads(history, clock: FakeClock) -> None:
    history.record(record())
    t = FakeTransport()
    service, _ = warm_service(t, clock)
    t.default = t.ok(finished_payload())

    async def go():
        blocker = asyncio.get_running_loop().create_future()
        service._standings_task = asyncio.ensure_future(blocker)
        try:
            return await service.background_calendar("fd:PL")
        finally:
            blocker.cancel()

    assert asyncio.run(go()) is None and t.calls == []


def test_a_settlement_load_in_flight_defers_the_standings_warm_up(history, clock: FakeClock) -> None:
    history.record(record())
    t = FakeTransport()
    service, _ = warm_service(t, clock)
    release = asyncio.Event()
    original = service.football.calendar

    async def slow(code: str):
        await release.wait()
        return await original(code)

    service.football.calendar = slow  # type: ignore[method-assign]
    t.default = t.ok(finished_payload())

    async def go():
        load = asyncio.create_task(service.background_calendar("fd:PL"))
        await asyncio.sleep(0)
        assert service.background_busy()
        assert service.ensure_standings_warm() is None  # settlement holds the background slot
        assert await service.background_calendar("fd:PD") is None  # and only one load at a time
        release.set()
        result = await load
        assert result is not None and not service.background_busy()
        task = service.ensure_standings_warm()  # the next activity starts it
        assert task is not None
        task.cancel()

    asyncio.run(go())


def test_the_search_warm_up_also_holds_the_background_slot(history, clock: FakeClock) -> None:
    t = FakeTransport()
    service, _ = warm_service(t, clock, warm=False)

    async def go():
        blocker = asyncio.get_running_loop().create_future()
        service.search_index._warm = asyncio.ensure_future(blocker)
        try:
            return await service.background_calendar("fd:PL")
        finally:
            blocker.cancel()

    assert asyncio.run(go()) is None and t.calls == []
