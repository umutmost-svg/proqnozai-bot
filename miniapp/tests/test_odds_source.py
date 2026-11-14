"""The Mini App's own Mostbet source: list -> catalogue match -> prices, within the budget, offline (fake API)."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from miniapp.odds import mostbet
from miniapp.odds.source import PAUSE_S, OddsSource
from miniapp.odds.store import OddsStore
from miniapp.sports.models import Competitor, FootballEvent, Provenance
from miniapp.tests.conftest import FakeClock

SPAIN = Competitor("fd:team:760", "Spain", None)
ITALY = Competitor("fd:team:784", "Italy", None)


def src_time(ts: float) -> str:
    """The list's own format: DD.MM.YYYY HH:MM:SS in UTC+3."""
    return datetime.fromtimestamp(ts + 3 * 3600, UTC).strftime("%d.%m.%Y %H:%M:%S")


def line(line_id: int, home: str, away: str, kickoff: float, *, live: bool = False, country="UEFA Nations League",
         league="League A", category="Football") -> dict:
    return {"id": line_id, "lineCategory": category, "lineSuperCategory": country, "lineSubCategory": league,
            "team1Title": home, "team2Title": away, "matchBeginAt": src_time(kickoff), "isLive": live}


OUTCOMES = [{"outcomeId": 1, "groupTitle": "Match result", "outcomeTitle": "1", "odd": "1.80"},
            {"outcomeId": 2, "groupTitle": "Match result", "outcomeTitle": "X", "odd": "3.80"},
            {"outcomeId": 3, "groupTitle": "Match result", "outcomeTitle": "2", "odd": "4.50"},
            {"outcomeId": 4, "groupTitle": "Total corners", "outcomeTitle": "Over (9.5)", "odd": "1.90"}]


class FakeApi:
    def __init__(self, lines: list[dict], outcomes: list[dict] | None = None) -> None:
        self.lines, self.outcomes = lines, OUTCOMES if outcomes is None else outcomes
        self.calls: list[str] = []
        self.rate_limit_odds = False

    async def __call__(self, url: str) -> tuple[int, bytes]:
        self.calls.append(url)
        if "/outcomes/" in url:
            if self.rate_limit_odds:
                return 429, b""
            return 200, json.dumps({"lineMatchOutcomes": self.outcomes}).encode()
        return 200, json.dumps({"lineMatches": self.lines}).encode()


async def no_sleep(_: float) -> None:
    return None


def event(home, away, kickoff: float, comp="fd:UNL") -> FootballEvent:
    return FootballEvent("fd:1", comp, datetime.fromtimestamp(kickoff, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "upcoming",
                         home, away, Provenance("football-data.org", "2027-01-14T08:00:00Z", None), "TIMED")


def test_the_lists_time_is_utc_plus_three() -> None:
    assert mostbet.parse_kickoff("15.01.2027 18:00:00") == datetime(2027, 1, 15, 15, tzinfo=UTC).timestamp()
    assert mostbet.parse_kickoff("2027-01-15T15:00:00Z") == datetime(2027, 1, 15, 15, tzinfo=UTC).timestamp()
    assert mostbet.parse_kickoff("soon") is None


def test_virtual_outright_and_other_sports_are_not_matches() -> None:
    assert mostbet.is_real_match(line(1, "Spain", "Italy", 0))
    assert not mostbet.is_real_match(line(1, "Spain (Esports)", "Italy", 0))
    assert not mostbet.is_real_match(line(1, "Spain", "?", 0))
    assert not mostbet.is_real_match(line(1, "Spain", "Italy", 0, category="Tennis"))


def test_only_main_markets_are_read() -> None:
    assert mostbet.parse_outcomes(OUTCOMES) == {"w1": 1.8, "x": 3.8, "w2": 4.5}


def test_a_list_page_that_fails_or_a_429_never_gives_a_partial_list() -> None:
    async def failing(url: str):
        return (200, json.dumps({"lineMatches": [{"id": i} for i in range(1, 101)]}).encode()) if "lastId=0" in url else (500, b"")

    with pytest.raises(mostbet.SourceError):
        asyncio.run(mostbet.fetch_list(failing, no_sleep))

    async def limited(url: str):
        return 429, b""

    with pytest.raises(mostbet.RateLimited):
        asyncio.run(mostbet.fetch_list(limited, no_sleep))


def test_a_cycle_lists_matches_prices_and_serves(clock: FakeClock) -> None:
    t = clock.now + 3 * 3600
    store = OddsStore(clock, catalog=True)
    api = FakeApi([line(10, "Spain", "Italy", t), line(11, "Faroe Islands", "Moldova", t)])
    source = OddsSource(store, lambda: [event(SPAIN, ITALY, t)], clock=clock, sleep=no_sleep, fetch=api)
    assert asyncio.run(source.cycle()) == 1
    assert sum("/outcomes/" in u for u in api.calls) == 1          # only the matched line is priced
    found = store.lookup(event(SPAIN, ITALY, t))
    assert found is not None and found.book["1"] == 1.8
    assert asyncio.run(source.cycle()) == 0                        # priced 0 s ago: not due yet


def test_the_budget_and_a_429_pause(clock: FakeClock) -> None:
    t = clock.now + 3 * 3600
    teams = [(Competitor(f"fd:team:{100 + i}", f"Home {i}", None), Competitor(f"fd:team:{200 + i}", f"Away {i}", None))
             for i in range(5)]
    store = OddsStore(clock, catalog=True)
    api = FakeApi([line(10 + i, h.name, a.name, t) for i, (h, a) in enumerate(teams)])
    events = [FootballEvent(f"fd:{i}", "fd:UNL", datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "upcoming", h, a, Provenance("football-data.org", "x", None), "TIMED")
              for i, (h, a) in enumerate(teams)]
    source = OddsSource(store, lambda: events, max_per_cycle=2, clock=clock, sleep=no_sleep, fetch=api)
    assert asyncio.run(source.cycle()) == 2                        # at most max_per_cycle per window


def test_a_429_pauses_every_request(clock: FakeClock) -> None:
    t = clock.now + 3 * 3600
    store = OddsStore(clock, catalog=True)
    api = FakeApi([line(10, "Spain", "Italy", t)])
    api.rate_limit_odds = True
    source = OddsSource(store, lambda: [event(SPAIN, ITALY, t)], clock=clock, sleep=no_sleep, fetch=api)
    assert asyncio.run(source.cycle()) == 0
    assert api.calls[-1].count("/outcomes/") == 1                  # the 429 came from a price request
    calls = len(api.calls)
    clock.advance(PAUSE_S - 1)
    assert asyncio.run(source.cycle()) == 0 and len(api.calls) == calls   # paused: no request at all
    clock.advance(2)
    api.rate_limit_odds = False
    assert asyncio.run(source.cycle()) == 1


def test_a_live_line_drops_its_pre_match_prices(clock: FakeClock) -> None:
    t = clock.now + 3 * 3600
    store = OddsStore(clock, catalog=True)
    api = FakeApi([line(10, "Spain", "Italy", t)])
    source = OddsSource(store, lambda: [event(SPAIN, ITALY, t)], clock=clock, sleep=no_sleep, fetch=api)
    asyncio.run(source.cycle())
    assert store.lookup(event(SPAIN, ITALY, t)) is not None
    clock.advance(16 * 60)
    api.lines = [line(10, "Spain", "Italy", t, live=True)]          # the list says it went live (early start)
    asyncio.run(source.cycle())
    assert store.lookup(event(SPAIN, ITALY, t)) is None


def test_the_budget_setting_is_bounded(tmp_path) -> None:
    from miniapp.settings import SettingsError
    from miniapp.tests.test_sports import base_settings
    assert base_settings(tmp_path).odds_source_max_per_cycle == 20
    with pytest.raises(SettingsError):
        base_settings(tmp_path, odds_source_max_per_cycle=41)


def test_the_budget_counts_requests_pages_included(clock: FakeClock) -> None:
    t = clock.now + 3 * 3600
    store = OddsStore(clock, catalog=True)
    many = [{**OUTCOMES[0], "outcomeId": i} for i in range(1, 101)]          # a full page: one more page follows
    api = FakeApi([line(10, "Spain", "Italy", t)], outcomes=many)
    source = OddsSource(store, lambda: [event(SPAIN, ITALY, t)], max_per_cycle=1, clock=clock, sleep=no_sleep, fetch=api)
    assert asyncio.run(source.cycle()) == 0                                 # the second page would pass the budget
    assert sum("/outcomes/" in u for u in api.calls) == 1


def test_the_first_price_request_waits_after_the_list(clock: FakeClock) -> None:
    t = clock.now + 3 * 3600
    store = OddsStore(clock, catalog=True)
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    source = OddsSource(store, lambda: [event(SPAIN, ITALY, t)], clock=clock, sleep=sleep,
                        fetch=FakeApi([line(10, "Spain", "Italy", t)]))
    asyncio.run(source.cycle())
    assert slept and slept[0] == 2.0



def test_the_budget_is_a_rolling_five_minute_window(clock: FakeClock) -> None:
    t = clock.now + 3 * 3600
    teams = [(Competitor(f"fd:team:{300 + i}", f"Home {i}", None), Competitor(f"fd:team:{400 + i}", f"Away {i}", None))
             for i in range(4)]
    store = OddsStore(clock, catalog=True)
    api = FakeApi([line(20 + i, h.name, a.name, t) for i, (h, a) in enumerate(teams)])
    events = [event(h, a, t) for h, a in teams]
    source = OddsSource(store, lambda: events, max_per_cycle=2, clock=clock, sleep=no_sleep, fetch=api)
    assert asyncio.run(source.cycle()) == 2
    clock.advance(60)                                    # a minute later: the window is still full
    assert asyncio.run(source.cycle()) == 0
    clock.advance(241)
    assert asyncio.run(source.cycle()) == 2



def test_unmatched_names_are_logged_for_the_alias_table(clock: FakeClock, caplog) -> None:
    import logging
    t = clock.now + 3 * 3600
    store = OddsStore(clock, catalog=True)
    source = OddsSource(store, lambda: [event(SPAIN, ITALY, t)], clock=clock, sleep=no_sleep,
                        fetch=FakeApi([line(10, "Spain", "Portugal", t)]))
    with caplog.at_level(logging.INFO, logger="miniapp.odds"):
        asyncio.run(source.cycle())
    assert any("odds.unmatched_examples reason=unknown_names" in r.message and "Spain - Portugal" in r.message
               for r in caplog.records)
