"""The daily slate: every validated match's model forecast on its match day, independent of what users open."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from miniapp.forecast.history import ForecastHistory
from miniapp.forecast.slate import PASS_EVERY_S, RETRY_S, SlateRecorder
from miniapp.sports.service import ModelHistory
from miniapp.tests.test_quick import NOW, ev, forecast
from miniapp.tests.test_sports import DAY


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Sports:
    """Cached histories per competition; `loadable`: competitions a background load can still fetch."""

    def __init__(self, events, cached=(), loadable=(), served=("fd:PL", "fd:PD", "fd:BL1")) -> None:
        self.events, self.cached, self.loadable = events, set(cached), set(loadable)
        self.served = served
        self.background_calls: list[str] = []

    def tournaments(self, sport):
        return [SimpleNamespace(id=t) for t in self.served]

    def cached_model_history(self, tournament):
        if tournament not in self.cached:
            return None
        return ModelHistory([e for e in self.events if e.tournament_id == tournament], f"{DAY[:4]}-08-01", True,
                            None, None)

    async def background_calendar(self, tournament):
        self.background_calls.append(tournament)
        if tournament not in self.loadable:
            return None
        self.cached.add(tournament)
        return object()


class Forecaster:
    def __init__(self, by_id) -> None:
        self.by_id = by_id

    def compute(self, event, history):
        return self.by_id[event.id]


def make(tmp_path, sports, by_id, clock=None):
    history = ForecastHistory(tmp_path / "h.db")
    history.init_schema()
    clock = clock or Clock()
    return SlateRecorder(history, sports, Forecaster(by_id), ["PL", "PD", "BL1", "SA"], clock), history, clock


def run_pass(recorder) -> None:
    async def go():
        task = recorder.ensure()
        if task is not None:
            await task

    asyncio.run(go())


def test_every_remaining_match_of_today_is_recorded_once_whatever_users_open(tmp_path) -> None:
    events = [ev(1), ev(2, home=(1, "A FC"), away=(2, "B FC")),
              ev(3, utc=f"{DAY}T07:00:00Z", home=(3, "C FC"), away=(4, "D FC")),   # kicked off before now
              ev(4, utc="2027-01-17T15:00:00Z", home=(5, "E FC"), away=(6, "F FC")),  # tomorrow: its own day
              ev(5, code="PD", home=(7, "G FC"), away=(8, "H FC"))]
    by_id = {e.id: forecast(("1", 0.64, 0.45)) for e in events}
    by_id["fd:2"] = forecast(None, reason="no_qualifying_market")
    sports = Sports(events, cached={"fd:PL"}, loadable={"fd:PD", "fd:BL1"})
    recorder, history, clock = make(tmp_path, sports, by_id)
    run_pass(recorder)
    total, rows = history.list_forecasts(include_slate=True)
    assert {r["eventId"] for r in rows} == {"fd:1", "fd:2", "fd:5"} and all(r["key"].startswith("slate:") for r in rows)
    assert sports.background_calls == ["fd:BL1", "fd:PD"]   # only what the cache lacked; SA is not served
    assert history.list_forecasts() == (0, []) and history.status_counts()["pending"] == 0  # admin: AI only
    clock.now += PASS_EVERY_S
    run_pass(recorder)                                      # same numbers: nothing new
    assert history.list_forecasts(include_slate=True)[0] == 3


def test_a_refused_background_load_keeps_the_cached_ones_and_retries_soon(tmp_path) -> None:
    events = [ev(1), ev(5, code="PD", home=(7, "G FC"), away=(8, "H FC"))]
    sports = Sports(events, cached={"fd:PL"}, loadable=set())
    recorder, history, clock = make(tmp_path, sports, {e.id: forecast(("1", 0.64, 0.45)) for e in events})
    run_pass(recorder)
    assert sports.background_calls == ["fd:BL1", "fd:PD"]
    assert history.list_forecasts(include_slate=True)[0] == 1        # PL from the cache
    clock.now += RETRY_S - 1
    assert recorder.ensure() is None                        # not yet
    sports.loadable = {"fd:PL", "fd:PD", "fd:BL1"}
    clock.now += 1
    run_pass(recorder)
    assert history.list_forecasts(include_slate=True)[0] == 2


def test_a_failed_history_write_is_logged_and_written_by_a_later_pass(tmp_path, caplog) -> None:
    events = [ev(1)]
    sports = Sports(events, cached={"fd:PL"})
    recorder, history, clock = make(tmp_path, sports, {"fd:1": forecast(("1", 0.64, 0.45))})
    real = history.record_many
    history.record_many = lambda records: (_ for _ in ()).throw(OSError("disk full"))  # type: ignore[method-assign]
    run_pass(recorder)
    assert "slate.history_write_failed type=OSError" in caplog.text
    history.record_many = real  # type: ignore[method-assign]
    clock.now += PASS_EVERY_S
    run_pass(recorder)
    assert history.list_forecasts(include_slate=True)[0] == 1
