"""Lists show only matches the goal model can forecast (owner 16.11): no "analysis without the statistical model"
in the day's list or the top matches."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from miniapp.app import create_app
from miniapp.forecast.coverage import ModelCoverage
from miniapp.forecast.data import MatchResult
from miniapp.forecast.engine import model_possible
from miniapp.forecast.selection import MIN_SAMPLE
from miniapp.sports.football_data import normalize_match
from miniapp.sports.service import build_service
from miniapp.tests.conftest import FakeClock, fresh, login
from miniapp.tests.test_sports import ALLOWED, DAY, FETCHED, KEY, FakeTransport, base_settings, match, no_sleep

NOW = datetime.fromisoformat(f"{DAY}T08:00:00+00:00").timestamp()
DAY_S = 86400


def results(n: int, teams: tuple[int, ...] = (57, 61, 1, 2), until: float = NOW - 3 * DAY_S) -> list[MatchResult]:
    """`n` finished matches cycling through `teams`, all well before today."""
    pairs = [(h, a) for h in teams for a in teams if h != a]
    return [MatchResult(i + 1, until - i * 3600, *pairs[i % len(pairs)], 1, 0) for i in range(n)]


def test_model_needs_enough_results_and_both_teams_with_history() -> None:
    assert model_possible(results(MIN_SAMPLE), 57, 61, NOW)
    assert not model_possible(results(MIN_SAMPLE - 1), 57, 61, NOW)  # small_sample
    assert not model_possible(results(MIN_SAMPLE), 57, 999, NOW)  # team_history: a team never seen
    today = results(MIN_SAMPLE, until=NOW)  # the latest results are not known before 00:00 UTC of today
    assert not model_possible(today, 57, 61, NOW)


@dataclass
class History:
    events: list
    previous_missing: str | None = None


def finished(mid: int, home: int, away: int, utc: str) -> object:
    raw = match(mid, "FINISHED", home=(home, f"T{home}"), away=(away, f"T{away}"), utc=utc, full=(1, 0), half=(0, 0))
    e = normalize_match(raw, FETCHED, ALLOWED)
    assert e is not None
    return e


def season(n: int) -> History:
    teams = (57, 61, 1, 2)
    pairs = [(h, a) for h in teams for a in teams if h != a]
    start = datetime(2026, 8, 1, 15, tzinfo=UTC).timestamp()
    return History([finished(1000 + i, *pairs[i % len(pairs)],
                             datetime.fromtimestamp(start + i * DAY_S, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
                    for i in range(n)])


def upcoming(mid: int, code: str, home: int = 57, away: int = 61):
    e = normalize_match(match(mid, code=code, home=(home, f"T{home}"), away=(away, f"T{away}")), FETCHED, ALLOWED)
    assert e is not None
    return e


def test_keep_uses_cached_calendars_and_trusts_only_validated_competitions_when_cold() -> None:
    histories = {"fd:PL": season(MIN_SAMPLE)}
    keep = ModelCoverage(histories.get, lambda: NOW, trusted=frozenset({"PD"})).keep
    pl, pl_new_team, pd = upcoming(1, "PL"), upcoming(2, "PL", away=999), upcoming(3, "PD")
    assert [e.id for e in keep([pl, pl_new_team, pd])] == ["fd:1", "fd:3"]
    # Cold and not validated: hidden until the warm-up loads its calendar.
    assert keep([upcoming(4, "PD")]) and not ModelCoverage(histories.get, lambda: NOW, frozenset()).keep([pd])
    # A competition with too few results: hidden even though it is cached.
    assert ModelCoverage({"fd:PL": season(10)}.get, lambda: NOW, frozenset()).keep([pl]) == []
    # The previous season is not loaded yet (not absent): a validated competition stays listed meanwhile;
    # once the source says it has none, the current season alone decides.
    loading = History(season(10).events, "transient")
    assert ModelCoverage({"fd:PL": loading}.get, lambda: NOW, frozenset({"PL"})).keep([pl]) == [pl]
    assert ModelCoverage({"fd:PL": loading}.get, lambda: NOW, frozenset()).keep([pl]) == []
    # ...but a team with too few matches is hidden whatever the previous season brings later.
    new_team = upcoming(5, "PL", away=999)
    assert ModelCoverage({"fd:PL": History(season(MIN_SAMPLE).events, "transient")}.get, lambda: NOW,
                         frozenset({"PL"})).keep([new_team]) == []
    # Even when the competition is also short of results (the team check comes first).
    assert ModelCoverage({"fd:PL": loading}.get, lambda: NOW, frozenset({"PL"})).keep([new_team]) == []
    absent = History(season(10).events, "absent")
    assert ModelCoverage({"fd:PL": absent}.get, lambda: NOW, frozenset({"PL"})).keep([pl]) == []


def test_the_day_list_and_the_top_show_only_covered_matches(tmp_path, clock: FakeClock) -> None:
    t = FakeTransport()
    settings = base_settings(tmp_path, football_data_key=KEY, session_ttl_s=3600)
    service = build_service(settings, clock=clock, transport=t, warm_emblems=False)
    service.football.client._sleep = no_sleep
    # PL is cached with too few results (no model); PD is not cached but validated (kept).
    cached = {"fd:PL": season(10)}
    service.cached_model_history = lambda tid, any_age=False: cached.get(tid)  # type: ignore[method-assign]
    client = TestClient(create_app(settings, clock=clock, sports=service), base_url="http://127.0.0.1:8000")
    login(client, fresh(clock))
    day = {"matches": [match(1), match(2, home=(1, "A FC"), away=(2, "B FC"), code="PD")]}
    t.script = [t.ok(day)]
    q = f"from={DAY}T00:00:00Z&to=2027-01-16T00:00:00Z"
    assert [e["id"] for e in client.get(f"/api/sports/football/events?{q}").json()["events"]] == ["fd:2"]
    assert [x["event"]["id"] for x in client.get(f"/api/sports/football/top?{q}").json()["top"]] == ["fd:2"]
