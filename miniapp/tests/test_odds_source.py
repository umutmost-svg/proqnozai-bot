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



REAL = [  # the groups and titles of a live line (15.11), in the API's order: second-half markets come later
    {"groupTitle": "1x2", "outcomeTitle": "W1", "odd": "1.870"}, {"groupTitle": "1x2", "outcomeTitle": "X", "odd": "4.150"},
    {"groupTitle": "1x2", "outcomeTitle": "W2", "odd": "3.650"},
    {"groupTitle": "Double Chance", "outcomeTitle": "2X", "odd": "1.940"},
    {"groupTitle": "Double Chance", "outcomeTitle": "1X", "odd": "1.280"},
    {"groupTitle": "Double Chance", "outcomeTitle": "12", "odd": "1.230"},
    {"groupTitle": "Total", "outcomeTitle": "Total Over (2.5)", "odd": "1.400"},
    {"groupTitle": "Total", "outcomeTitle": "Total Under (2.5)", "odd": "2.700"},
    {"groupTitle": "Home Team Total", "outcomeTitle": "Total Over (2.5)", "odd": "2.550"},
    {"groupTitle": "Both Teams To Score", "outcomeTitle": "Yes", "odd": "1.400"},
    {"groupTitle": "Both Teams To Score", "outcomeTitle": "No", "odd": "2.700"},
    {"groupTitle": "Handicap", "outcomeTitle": "Handicap 1 (-1.0)", "odd": "2.350"},
    {"groupTitle": "Handicap", "outcomeTitle": "Handicap 2 (+1.0)", "odd": "1.600"},
    {"groupTitle": "Handicap", "outcomeTitle": "Handicap 1 (+1.0)", "odd": "1.110"},
    {"groupTitle": "Handicap", "outcomeTitle": "Handicap 2 (-1.0)", "odd": "5.300"},
    {"groupTitle": "2nd Half - 1x2", "outcomeTitle": "W1", "odd": "2.400"},
    {"groupTitle": "2nd Half - Total", "outcomeTitle": "Total Over (2.5)", "odd": "2.850"},
    {"groupTitle": "1st Half - Total", "outcomeTitle": "Total Over (2.5)", "odd": "4.050"},
]


def test_only_the_main_full_time_groups_are_read() -> None:
    odds = mostbet.parse_outcomes(REAL)
    assert (odds["w1"], odds["x"], odds["w2"]) == (1.87, 4.15, 3.65)
    assert odds["over25"] == 1.4 and odds["under25"] == 2.7              # never the second half's 2.85
    assert (odds["dc_1x"], odds["dc_12"], odds["dc_x2"]) == (1.28, 1.23, 1.94)
    assert (odds["btts_yes"], odds["btts_no"]) == (1.4, 2.7)
    assert (odds["hcp_val"], odds["hcp_w1"], odds["hcp_w2"]) == (-1.0, 2.35, 1.6)   # one line, never mixed


def test_while_the_own_source_governs_the_bots_prices_are_not_stored(clock: FakeClock) -> None:
    from miniapp.odds.feed import CatalogLine, FeedLine
    t = clock.now + 3 * 3600
    store = OddsStore(clock, catalog=True)
    store.ingest_catalog([CatalogLine(10, "UEFA Nations League", "League A", "Spain", "Italy", t)])
    report = store.ingest([FeedLine(10, "UEFA Nations League", "League A", "Spain", "Italy", t, False, clock.now,
                                    {"1": 2.0, "X": 3.0, "2": 4.0})])
    assert report.accepted == 0 and len(store) == 0
    marker = store.ingest([FeedLine(10, "UEFA Nations League", "League A", "Spain", "Italy", t, True, clock.now, {})])
    assert marker.accepted == 1                                          # live markers still count



def test_a_live_marker_is_never_replaced_by_a_pre_match_price(clock: FakeClock) -> None:
    from miniapp.odds.feed import CatalogLine, FeedLine
    t = clock.now + 3 * 3600
    store = OddsStore(clock, catalog=True)
    store.ingest_catalog([CatalogLine(10, "UEFA Nations League", "League A", "Spain", "Italy", t)])
    store.match_catalog([event(SPAIN, ITALY, t)])
    store.ingest([FeedLine(10, "UEFA Nations League", "League A", "Spain", "Italy", t, True, clock.now, {})])  # bot: live
    clock.advance(60)
    store.ingest([FeedLine(10, "UEFA Nations League", "League A", "Spain", "Italy", t, False, clock.now,
                           {"1": 2.0, "X": 3.0, "2": 4.0})], own=True)   # the own source, between list refreshes
    assert store.lookup(event(SPAIN, ITALY, t)) is None


def test_prices_survive_a_restart_through_the_snapshot(clock: FakeClock, tmp_path) -> None:
    t = clock.now + 3 * 3600
    snap = tmp_path / "odds-snapshot.json"
    api = FakeApi([line(10, "Spain", "Italy", t)])
    first = OddsSource(OddsStore(clock, catalog=True), lambda: [event(SPAIN, ITALY, t)], clock=clock, sleep=no_sleep,
                       fetch=api, snapshot=snap)
    assert asyncio.run(first.cycle()) == 1 and snap.exists()
    clock.advance(60)
    store = OddsStore(clock, catalog=True)                          # the restart: an empty store
    second = OddsSource(store, lambda: [event(SPAIN, ITALY, t)], clock=clock, sleep=no_sleep, fetch=api, snapshot=snap)
    calls = sum("/outcomes/" in u for u in api.calls)
    assert asyncio.run(second.cycle()) == 0                         # restored after the first list: no new request
    assert sum("/outcomes/" in u for u in api.calls) == calls
    found = store.lookup(event(SPAIN, ITALY, t))
    assert found is not None and found.book["1"] == 1.8


def test_a_restored_line_gone_from_the_list_is_not_served(clock: FakeClock, tmp_path) -> None:
    t = clock.now + 3 * 3600
    snap = tmp_path / "odds-snapshot.json"
    first = OddsSource(OddsStore(clock, catalog=True), lambda: [event(SPAIN, ITALY, t)], clock=clock, sleep=no_sleep,
                       fetch=FakeApi([line(10, "Spain", "Italy", t)]), snapshot=snap)
    asyncio.run(first.cycle())
    store = OddsStore(clock, catalog=True)
    second = OddsSource(store, lambda: [event(SPAIN, ITALY, t)], clock=clock, sleep=no_sleep, fetch=FakeApi([]),
                        snapshot=snap)
    asyncio.run(second.cycle())
    assert len(store) == 0 and store.lookup(event(SPAIN, ITALY, t)) is None


def test_nothing_is_restored_before_a_complete_list(clock: FakeClock, tmp_path) -> None:
    t = clock.now + 3 * 3600
    snap = tmp_path / "odds-snapshot.json"
    first = OddsSource(OddsStore(clock, catalog=True), lambda: [event(SPAIN, ITALY, t)], clock=clock, sleep=no_sleep,
                       fetch=FakeApi([line(10, "Spain", "Italy", t)]), snapshot=snap)
    asyncio.run(first.cycle())

    async def failing(url: str) -> tuple[int, bytes]:
        return 500, b""
    store = OddsStore(clock, catalog=True)
    second = OddsSource(store, lambda: [event(SPAIN, ITALY, t)], clock=clock, sleep=no_sleep, fetch=failing, snapshot=snap)
    asyncio.run(second.cycle())
    assert len(store) == 0


@pytest.mark.parametrize("content", ["not json", '{"version": 2, "lines": []}', '{"version": 1, "lines": [{"id": "x"}]}',
                                     '{"version": 1, "lines": [{"id": 10, "country": "c", "league": "l", "home": "h", '
                                     '"away": "a", "kickoff": 1e18, "fetchedAt": 1e18, "book": {"1": 1.8}}]}',
                                     '{"version": 1, "lines": [{"id": 10, "country": "c", "league": "l", "home": "h", '
                                     '"away": "a", "kickoff": ' + "9" * 400 + ', "fetchedAt": 1, "book": {}}]}'])
def test_a_bad_or_stale_snapshot_is_ignored(clock: FakeClock, tmp_path, content) -> None:
    snap = tmp_path / "odds-snapshot.json"
    snap.write_text(content)
    source = OddsSource(OddsStore(clock, catalog=True), lambda: [], clock=clock, sleep=no_sleep, fetch=FakeApi([]),
                        snapshot=snap)
    assert source.restore() == 0


def test_an_old_snapshot_price_is_not_restored(clock: FakeClock, tmp_path) -> None:
    t = clock.now + 30 * 3600
    snap = tmp_path / "odds-snapshot.json"
    first = OddsSource(OddsStore(clock, catalog=True), lambda: [event(SPAIN, ITALY, t)], clock=clock, sleep=no_sleep,
                       fetch=FakeApi([line(10, "Spain", "Italy", t)]), snapshot=snap)
    asyncio.run(first.cycle())
    clock.advance(5 * 3600)                                                 # older than any serving limit (4 h)
    source = OddsSource(OddsStore(clock, catalog=True), lambda: [], clock=clock, sleep=no_sleep, fetch=FakeApi([]),
                        snapshot=snap)
    source._list = {10: {"country": "UEFA Nations League", "league": "League A", "home": "Spain", "away": "Italy",
                         "kickoff": float(t)}}                      # still listed: only its age keeps it out
    assert source.restore() == 0
    clock.advance(-5 * 3600 + 60)
    assert source.restore() == 1                                    # the same snapshot, fresh: restored


def test_a_restored_id_must_still_be_the_same_line(clock: FakeClock, tmp_path) -> None:
    t = clock.now + 3 * 3600
    snap = tmp_path / "odds-snapshot.json"
    first = OddsSource(OddsStore(clock, catalog=True), lambda: [event(SPAIN, ITALY, t)], clock=clock, sleep=no_sleep,
                       fetch=FakeApi([line(10, "Spain", "Italy", t)]), snapshot=snap)
    asyncio.run(first.cycle())
    api = FakeApi([line(10, "Spain", "Portugal", t)])                # the same id, now another match
    api.rate_limit_odds = True
    store = OddsStore(clock, catalog=True)
    second = OddsSource(store, lambda: [event(SPAIN, ITALY, t)], clock=clock, sleep=no_sleep, fetch=api, snapshot=snap)
    asyncio.run(second.cycle())
    assert len(store) == 0


def test_a_match_five_days_ahead_is_priced(clock: FakeClock) -> None:
    t = clock.now + 5 * 86400
    store = OddsStore(clock, catalog=True)
    api = FakeApi([line(10, "Spain", "Italy", t)])
    source = OddsSource(store, lambda: [event(SPAIN, ITALY, t)], clock=clock, sleep=no_sleep, fetch=api)
    assert asyncio.run(source.cycle()) == 1
    assert store.lookup(event(SPAIN, ITALY, t)) is not None


def test_the_source_starts_with_the_app(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from miniapp.app import create_app
    from miniapp.odds import source as source_module
    from miniapp.settings import Settings
    from miniapp.tests.conftest import ORIGIN
    from miniapp.tests.signing import TEST_BOT_TOKEN

    started = []
    monkeypatch.setattr(source_module.OddsSource, "ensure_running", lambda self: started.append(self))
    settings = Settings(bot_token=TEST_BOT_TOKEN, db_path=tmp_path / "miniapp.db", trusted_origins=frozenset({ORIGIN}),
                        environment="development", odds_feed_enabled=True, odds_catalog=True)
    with TestClient(create_app(settings)):
        assert len(started) == 1                 # no request yet: started at boot


def test_the_source_keeps_the_calendars_loaded(clock: FakeClock) -> None:
    warmed = []
    source = OddsSource(OddsStore(clock, catalog=True), lambda: [], clock=clock, sleep=no_sleep, fetch=FakeApi([]),
                        warm=lambda: warmed.append(1))
    asyncio.run(source.cycle())
    assert warmed == [1]


def test_a_week_tier_price_is_served_hours_later_but_a_client_keeps_it_an_hour(clock: FakeClock) -> None:
    t = clock.now + 4 * 86400
    store = OddsStore(clock, catalog=True)
    source = OddsSource(store, lambda: [event(SPAIN, ITALY, t)], clock=clock, sleep=no_sleep,
                        fetch=FakeApi([line(10, "Spain", "Italy", t)]))
    asyncio.run(source.cycle())
    first = store.lookup(event(SPAIN, ITALY, t))
    assert first is not None and 1 <= first.valid_for <= 3600              # a client keeps it at most an hour
    clock.advance(2 * 3600)                                                # not re-priced (refresh every 3 h)
    later = store.lookup(event(SPAIN, ITALY, t))
    assert later is not None and 1 <= later.valid_for <= 3600              # still served: 4 h limit that far out
    clock.advance(3 * 3600)
    assert store.lookup(event(SPAIN, ITALY, t)) is None                    # past 4 h: never shown


def test_the_price_about_to_go_stale_is_refreshed_first(clock: FakeClock) -> None:
    near_t, week_t = clock.now + 3 * 3600, clock.now + 4 * 86400
    near = (Competitor("fd:team:301", "Northton", None), Competitor("fd:team:302", "Eastton", None))
    week = (Competitor("fd:team:303", "Westton", None), Competitor("fd:team:304", "Southton", None))
    events = [FootballEvent("fd:1", "fd:UNL", datetime.fromtimestamp(near_t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "upcoming", *near, Provenance("football-data.org", "x", None), "TIMED"),
              FootballEvent("fd:2", "fd:UNL", datetime.fromtimestamp(week_t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "upcoming", *week, Provenance("football-data.org", "x", None), "TIMED")]
    api = FakeApi([line(10, "Northton", "Eastton", near_t), line(11, "Westton", "Southton", week_t)])
    source = OddsSource(OddsStore(clock, catalog=True), lambda: events, max_per_cycle=1, clock=clock, sleep=no_sleep,
                        fetch=api)
    source._priced = {10: clock.now - 11 * 60, 11: clock.now - (4 * 3600 - 3 * 60)}  # stale in 4 min vs in 3 min
    asyncio.run(source.cycle())
    assert [u for u in api.calls if "/outcomes/" in u][0].count("/line/11/") == 1      # the week match goes first


def test_a_share_of_requests_goes_beyond_the_near_matches(clock: FakeClock) -> None:
    near_t, far_t = clock.now + 3 * 3600, clock.now + 3 * 86400
    names = ["Aldton", "Brixton", "Corton", "Dalton", "Elton", "Fenton", "Gorton", "Hilton", "Ilton", "Jolton"]
    teams = [(Competitor(f"fd:team:{500 + 2 * i}", names[i], None), Competitor(f"fd:team:{501 + 2 * i}", names[i] + "ville", None))
             for i in range(5)]
    def ev_at(i: int, home, away, t: float) -> FootballEvent:
        return FootballEvent(f"fd:{i}", "fd:UNL", datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "upcoming", home, away, Provenance("football-data.org", "x", None), "TIMED")
    events = [ev_at(i, h, a, near_t if i < 4 else far_t) for i, (h, a) in enumerate(teams)]
    api = FakeApi([line(10 + i, h.name, a.name, near_t if i < 4 else far_t) for i, (h, a) in enumerate(teams)])
    source = OddsSource(OddsStore(clock, catalog=True), lambda: events, max_per_cycle=4, clock=clock, sleep=no_sleep,
                        fetch=api)
    source._priced = {14: clock.now - 3.5 * 3600}                          # the far one: priced, not yet urgent
    asyncio.run(source.cycle())
    priced = [u for u in api.calls if "/outcomes/" in u]
    assert len(priced) == 4 and any("/line/14/" in u for u in priced)      # its turn comes within the budget



def test_the_far_share_counts_requests_pages_included(clock: FakeClock) -> None:
    near_t, far_t = clock.now + 3 * 3600, clock.now + 3 * 86400
    pages = [{"outcomeId": i, "groupTitle": "Corners", "outcomeTitle": f"o{i}", "odd": "1.5"} for i in range(1, 101)]

    class TwoPages(FakeApi):
        async def __call__(self, url: str):
            if "/outcomes/" in url and "lastId=0" in url and "/line/14/" not in url:
                self.calls.append(url)
                return 200, json.dumps({"lineMatchOutcomes": pages}).encode()      # near lines: two pages each
            return await super().__call__(url)

    a, b = Competitor("fd:team:601", "Aldton", None), Competitor("fd:team:602", "Brixton", None)
    c, d = Competitor("fd:team:603", "Corton", None), Competitor("fd:team:604", "Dalton", None)
    f1, f2 = Competitor("fd:team:605", "Fenton", None), Competitor("fd:team:606", "Gorton", None)
    def ev_at(i, h, w, t):
        return FootballEvent(f"fd:{i}", "fd:UNL", datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "upcoming", h, w, Provenance("football-data.org", "x", None), "TIMED")
    events = [ev_at(0, a, b, near_t), ev_at(1, c, d, near_t), ev_at(4, f1, f2, far_t)]
    api = TwoPages([line(10, "Aldton", "Brixton", near_t), line(11, "Corton", "Dalton", near_t),
                    line(14, "Fenton", "Gorton", far_t)])
    source = OddsSource(OddsStore(clock, catalog=True), lambda: events, max_per_cycle=4, clock=clock, sleep=no_sleep,
                        fetch=api)
    source._priced = {14: clock.now - 3.5 * 3600}
    asyncio.run(source.cycle())
    assert any("/line/14/" in u for u in api.calls)                         # the far match got its share



def test_a_two_page_far_line_fits_its_share_whole(clock: FakeClock) -> None:
    near_t, far_t = clock.now + 3 * 3600, clock.now + 3 * 86400
    page = [{"outcomeId": i, "groupTitle": "Corners", "outcomeTitle": f"o{i}", "odd": "1.5"} for i in range(1, 101)]

    class FarTwoPages(FakeApi):
        async def __call__(self, url: str):
            if "/line/13/outcomes/" in url and "lastId=0" in url:
                self.calls.append(url)
                return 200, json.dumps({"lineMatchOutcomes": page}).encode()
            return await super().__call__(url)                              # records the call itself

    names = ["Aldton", "Brixton", "Corton", "Dalton", "Elton", "Fenton", "Gorton", "Hilton"]
    comps = [Competitor(f"fd:team:{700 + i}", n, None) for i, n in enumerate(names)]
    def ev_at(i, h, w, t):
        return FootballEvent(f"fd:{i}", "fd:UNL", datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "upcoming", h, w, Provenance("football-data.org", "x", None), "TIMED")
    events = [ev_at(i, comps[2 * i], comps[2 * i + 1], near_t if i < 3 else far_t) for i in range(4)]
    api = FarTwoPages([line(10 + i, comps[2 * i].name, comps[2 * i + 1].name, near_t if i < 3 else far_t) for i in range(4)])
    store = OddsStore(clock, catalog=True)
    source = OddsSource(store, lambda: events, max_per_cycle=4, clock=clock, sleep=no_sleep, fetch=api)
    source._priced = {13: clock.now - 3.5 * 3600}
    asyncio.run(source.cycle())
    assert sum("/line/13/outcomes/" in u for u in api.calls) == 2              # both of its pages were read
    assert source._priced[13] == clock.now                                  # and the far line was priced


def test_a_held_near_line_keeps_its_place_ahead_of_less_urgent_ones(clock: FakeClock) -> None:
    far_t = clock.now + 3 * 86400
    names = ["Aldton", "Brixton", "Corton", "Dalton", "Elton", "Fenton", "Gorton", "Hilton", "Ilton", "Jolton"]
    comps = [Competitor(f"fd:team:{800 + i}", n, None) for i, n in enumerate(names)]
    kicks = [clock.now + 3600, clock.now + 4000, clock.now + 4400, far_t, clock.now + 5000]   # A B C, far D, E
    def ev_at(i, t):
        return FootballEvent(f"fd:{i}", "fd:UNL", datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "upcoming", comps[2 * i], comps[2 * i + 1], Provenance("football-data.org", "x", None), "TIMED")
    events = [ev_at(i, k) for i, k in enumerate(kicks)]
    api = FakeApi([line(10 + i, comps[2 * i].name, comps[2 * i + 1].name, k) for i, k in enumerate(kicks)])
    source = OddsSource(OddsStore(clock, catalog=True), lambda: events, max_per_cycle=4, clock=clock, sleep=no_sleep,
                        fetch=api)
    source._priced = {13: clock.now - 3.5 * 3600}
    asyncio.run(source.cycle())
    order = [int(u.split("/line/")[1].split("/")[0]) for u in api.calls if "/outcomes/" in u]
    assert order == [10, 11, 13, 12]                                        # C right after the far D; E waits
