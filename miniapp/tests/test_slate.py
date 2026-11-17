"""The daily slate: every validated match's model forecast on its match day, independent of what users open."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from miniapp.forecast.history import ForecastHistory
from miniapp.forecast.slate import LOAD_SPACING_S, PASS_EVERY_S, RETRY_S, SlateRecorder
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

    def __init__(self, events, cached=(), loadable=(), served=("fd:PL", "fd:PD", "fd:BL1"), failing=()) -> None:
        self.events, self.cached, self.loadable = events, set(cached), set(loadable)
        self.served, self.failing = served, set(failing)
        self.background_calls: list[str] = []

    def tournaments(self, sport):
        return [SimpleNamespace(id=t) for t in self.served]

    async def background_model_history(self, tournament, max_age_s=None):
        self.max_age = max_age_s
        if tournament in self.failing:
            raise RuntimeError("provider error")
        if tournament not in self.cached:
            self.background_calls.append(tournament)
            if tournament not in self.loadable:
                return None, False
            self.cached.add(tournament)
            loaded = True
        else:
            loaded = False
        complete = tournament not in getattr(self, "partial", set())
        return ModelHistory([e for e in self.events if e.tournament_id == tournament], f"{DAY[:4]}-08-01", complete,
                            None if complete else "transient", None), loaded


class Forecaster:
    def __init__(self, by_id) -> None:
        self.by_id = by_id

    def compute(self, event, history):
        return self.by_id[event.id]


def make(tmp_path, sports, by_id, clock=None):
    history = ForecastHistory(tmp_path / "h.db")
    history.init_schema()
    clock = clock or Clock()
    sleeps: list[float] = []

    async def sleep(s):
        sleeps.append(s)

    recorder = SlateRecorder(history, sports, Forecaster(by_id), ["PL", "PD", "BL1", "SA"], clock, sleep=sleep)
    recorder.sleeps = sleeps  # type: ignore[attr-defined]
    return recorder, history, clock


def run_pass(recorder) -> None:
    asyncio.run(recorder.run_pass())


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
    assert recorder.sleeps == [LOAD_SPACING_S, LOAD_SPACING_S]  # spaced loads, never a burst
    assert history.list_forecasts() == (0, []) and history.status_counts()["pending"] == 0  # admin: AI only
    clock.now += 60
    run_pass(recorder)                                      # same numbers: nothing new
    assert history.list_forecasts(include_slate=True)[0] == 3


def test_a_refused_or_failing_competition_never_stops_the_others_and_is_retried_soon(tmp_path, caplog) -> None:
    events = [ev(1), ev(5, code="PD", home=(7, "G FC"), away=(8, "H FC"))]
    sports = Sports(events, cached={"fd:PL"}, loadable=set(), failing={"fd:BL1"})
    recorder, history, clock = make(tmp_path, sports, {e.id: forecast(("1", 0.64, 0.45)) for e in events})
    run_pass(recorder)
    assert "slate.competition_failed tournament=fd:BL1 type=RuntimeError" in caplog.text
    assert history.list_forecasts(include_slate=True)[0] == 1        # PL from the cache all the same
    assert recorder.wait_s() == RETRY_S
    sports.loadable, sports.failing = {"fd:PD", "fd:BL1"}, set()
    run_pass(recorder)
    assert history.list_forecasts(include_slate=True)[0] == 2 and recorder.wait_s() == PASS_EVERY_S


def test_the_next_pass_comes_right_after_utc_midnight(tmp_path) -> None:
    from datetime import datetime

    late = datetime.fromisoformat(f"{DAY}T23:50:00+00:00").timestamp()
    recorder, _, clock = make(tmp_path, Sports([]), {}, Clock(late))
    assert recorder.wait_s() == 10 * 60 + 5
    started = datetime.fromisoformat(f"{DAY}T23:59:00+00:00").timestamp()
    clock.now = started + 120                               # a pass that ran across midnight
    assert recorder.wait_s(started) == 1.0                  # the post-midnight pass is due now, not in 30 min


def test_a_transient_previous_season_is_retried_soon_and_the_calendar_must_be_fresh(tmp_path) -> None:
    from miniapp.forecast.slate import CALENDAR_MAX_AGE_S

    sports = Sports([ev(1)], cached={"fd:PL"})
    sports.partial = {"fd:PL"}
    recorder, history, _ = make(tmp_path, sports, {"fd:1": forecast(("1", 0.64, 0.45))})
    run_pass(recorder)
    assert history.list_forecasts(include_slate=True)[0] == 1 and recorder.wait_s() == RETRY_S
    assert sports.max_age == CALENDAR_MAX_AGE_S


def test_a_match_that_kicked_off_while_computing_is_not_recorded(tmp_path) -> None:
    from datetime import datetime

    events = [ev(1, utc=f"{DAY}T08:00:30Z")]
    clock = Clock()
    sports = Sports(events, cached={"fd:PL"})

    class Slow:
        def compute(self, event, history):
            clock.now += 60                                 # the computation outlasts the kickoff
            return forecast(("1", 0.64, 0.45))

    recorder, history, _ = make(tmp_path, sports, {}, clock)
    recorder.forecaster = Slow()
    run_pass(recorder)
    assert history.list_forecasts(include_slate=True)[0] == 0


def test_a_failed_history_write_is_logged_and_written_by_a_later_pass(tmp_path, caplog) -> None:
    events = [ev(1)]
    sports = Sports(events, cached={"fd:PL"})
    recorder, history, clock = make(tmp_path, sports, {"fd:1": forecast(("1", 0.64, 0.45))})
    real = history.record_many
    history.record_many = lambda records: (_ for _ in ()).throw(OSError("disk full"))  # type: ignore[method-assign]
    run_pass(recorder)
    assert "slate.history_write_failed type=OSError" in caplog.text
    history.record_many = real  # type: ignore[method-assign]
    assert recorder.wait_s() == RETRY_S
    run_pass(recorder)
    assert history.list_forecasts(include_slate=True)[0] == 1
