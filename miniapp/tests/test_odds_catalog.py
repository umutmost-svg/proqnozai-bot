"""Odds across the whole catalogue (docs/miniapp/odds-catalog.md): the bot's list metadata matched against the Mini
App's own matches by time and team names, never a guess; the bot told which lines to price."""
from __future__ import annotations

import json

import pytest

from miniapp.odds.feed import FeedError, FeedReceiver
from miniapp.odds.store import FAR_MAX_AGE_S, FAR_REFRESH_S, MAX_AGE_S, NEAR_REFRESH_S, OddsStore
from miniapp.sports.models import Competitor, FootballEvent, Provenance
from miniapp.tests.conftest import FakeClock
from miniapp.tests.test_odds import ARSENAL, CHELSEA, ODDS, SECRET, client_for, iso, signed

SPAIN = Competitor("fd:team:760", "Spain", None)
ITALY = Competitor("fd:team:784", "Italy", None)
GENK = Competitor("fd:team:9100", "KRC Genk", "Genk")
GENT = Competitor("fd:team:9101", "KAA Gent", "Gent")


def event(home: Competitor, away: Competitor, kickoff: float, *, comp: str = "fd:UNL", mid: str = "fd:1",
          status: str = "upcoming") -> FootballEvent:
    return FootballEvent(mid, comp, iso(kickoff), status, home, away,
                         Provenance("football-data.org", "2027-01-14T08:00:00Z", None), "TIMED")


def catalog_body(*lines: dict, nonce: str = "b" * 32) -> bytes:
    return json.dumps({"v": 1, "nonce": nonce, "kind": "catalog", "lines": list(lines)}).encode()


def cline(line_id: int, home: str, away: str, kickoff: float, *, country: str = "UEFA Nations League",
          league: str = "League A") -> dict:
    return {"id": line_id, "country": country, "league": league, "home": home, "away": away, "kickoff": iso(kickoff)}


def push(store: OddsStore, clock: FakeClock, *lines: dict, nonce: str = "b" * 32) -> None:
    kind, parsed = FeedReceiver(SECRET, clock).verify_push(*signed(clock, catalog_body(*lines, nonce=nonce)))
    assert kind == "catalog"
    store.ingest_catalog(parsed)


def test_a_catalogue_push_is_signed_and_carries_no_prices(clock: FakeClock) -> None:
    receiver = FeedReceiver(SECRET, clock)
    kind, lines = receiver.verify_push(*signed(clock, catalog_body(cline(1, "Spain", "Italy", clock.now + 7200))))
    assert kind == "catalog" and lines[0].home == "Spain"
    with pytest.raises(FeedError):  # the priced-lines entry point never takes a catalogue
        FeedReceiver(SECRET, clock).verify(*signed(clock, catalog_body(cline(1, "Spain", "Italy", clock.now + 7200),
                                                                        nonce="c" * 32)))


def test_a_line_outside_the_league_table_is_matched_by_time_and_both_names(clock: FakeClock) -> None:
    store = OddsStore(clock, catalog=True)
    t = clock.now + 3 * 3600
    push(store, clock, cline(10, "Spain", "Italy", t + 300))          # five minutes apart: within tolerance
    report = store.match_catalog([event(SPAIN, ITALY, t)])
    assert report.reasons == {"matched": 1} and report.wanted == [(10, NEAR_REFRESH_S)]


@pytest.mark.parametrize("pairs, shift, reason", [
    ([(10, "Spain", "Portugal")], 0, "unknown_names"),                    # one team differs
    ([(10, "Italy", "Spain")], 0, "reversed"),                            # home and away swapped
    ([(10, "Spain", "Italy"), (11, "Spain", "Italy")], 0, "ambiguous"),   # a duplicate in the feed
    ([(10, "Spain", "Italy")], 3600, "no_line"),                          # an hour apart
])
def test_anything_short_of_one_certain_line_gives_no_odds(clock: FakeClock, pairs, shift, reason) -> None:
    store = OddsStore(clock, catalog=True)
    t = clock.now + 3 * 3600
    push(store, clock, *(cline(i, h, a, t + shift) for i, h, a in pairs))
    report = store.match_catalog([event(SPAIN, ITALY, t)])
    assert report.reasons == {reason: 1} and report.wanted == []
    if reason != "no_line":
        assert reason in report.samples      # logged with the names, for the alias table


def test_a_line_mapped_to_another_competition_never_prices_this_one(clock: FakeClock) -> None:
    store = OddsStore(clock, catalog=True)
    t = clock.now + 3 * 3600
    # England / Premier League maps to PL: never a Nations League match, whatever the names.
    push(store, clock, cline(10, "Spain", "Italy", t, country="England", league="Premier League"))
    assert store.match_catalog([event(SPAIN, ITALY, t)]).wanted == []


def test_one_line_claimed_by_two_matches_is_priced_for_neither(clock: FakeClock) -> None:
    store = OddsStore(clock, catalog=True)
    t = clock.now + 3 * 3600
    push(store, clock, cline(10, "Genk", "Gent", t, country="Belgium", league="Cup"))
    report = store.match_catalog([event(GENK, GENT, t, comp="fd:BJL", mid="fd:1"),
                                  event(GENK, GENT, t, comp="fd:BJPP", mid="fd:2")])
    assert report.wanted == [] and report.reasons.get("duplicate") == 2 and report.reasons.get("matched") == 0


def test_far_matches_are_refreshed_less_often_and_served_longer(clock: FakeClock) -> None:
    store = OddsStore(clock, catalog=True)
    far = clock.now + 30 * 3600
    push(store, clock, cline(10, "Spain", "Italy", far))
    assert store.match_catalog([event(SPAIN, ITALY, far)]).wanted == [(10, FAR_REFRESH_S)]
    store.ingest(FeedReceiver(SECRET, clock).verify(*signed(clock, json.dumps({"v": 1, "nonce": "d" * 32, "lines": [
        {**cline(10, "Spain", "Italy", far), "live": False, "fetchedAt": iso(clock.now - 40 * 60), "odds": ODDS}]}).encode())))
    found = store.lookup(event(SPAIN, ITALY, far))
    assert found is not None and found.valid_for <= FAR_MAX_AGE_S           # 40 min old: still served far out
    near = OddsStore(clock, catalog=True)
    near.ingest(FeedReceiver(SECRET, clock).verify(*signed(clock, json.dumps({"v": 1, "nonce": "e" * 32, "lines": [
        {**cline(11, "Spain", "Italy", clock.now + 3600), "live": False, "fetchedAt": iso(clock.now - 40 * 60),
         "odds": ODDS}]}).encode())))
    assert near.lookup(event(SPAIN, ITALY, clock.now + 3600)) is None       # near kickoff the 15-minute limit stays
    assert MAX_AGE_S == 15 * 60


def test_without_catalogue_mode_lines_outside_the_table_stay_unknown(clock: FakeClock) -> None:
    store = OddsStore(clock)
    t = clock.now + 3 * 3600
    report = store.ingest(FeedReceiver(SECRET, clock).verify(*signed(clock, json.dumps({"v": 1, "nonce": "f" * 32, "lines": [
        {**cline(10, "Spain", "Italy", t), "live": False, "fetchedAt": iso(clock.now - 60), "odds": ODDS}]}).encode())))
    assert report.unknown_competition == 1 and store.lookup(event(SPAIN, ITALY, t)) is None


def test_catalogue_entries_expire_when_the_bot_stops_listing_them(clock: FakeClock) -> None:
    store = OddsStore(clock, catalog=True)
    t = clock.now + 20 * 3600
    push(store, clock, cline(10, "Spain", "Italy", t))
    clock.advance(41 * 60)
    push(store, clock, cline(11, "Arsenal", "Chelsea", t, country="England", league="Premier League"), nonce="c" * 32)
    assert store.match_catalog([event(SPAIN, ITALY, t)]).wanted == []


def test_the_route_tells_the_bot_what_to_price_only_in_catalogue_mode(tmp_path, clock: FakeClock, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from miniapp.app import create_app
    from miniapp.sports.search import SearchIndex
    from miniapp.tests.test_sports import base_settings

    t = clock.now + 3 * 3600
    monkeypatch.setattr(SearchIndex, "upcoming", lambda self: [event(ARSENAL, CHELSEA, t, comp="fd:PL")])
    for mode, expected in ((False, []), (True, [{"id": 10, "refresh": int(NEAR_REFRESH_S)}])):
        settings = base_settings(tmp_path / str(mode), odds_feed_secret="s" * 40, odds_catalog=mode)
        client = TestClient(create_app(settings, clock=clock), base_url="http://127.0.0.1:8000")
        payload = catalog_body(cline(10, "Arsenal", "Chelsea", t, country="England", league="Premier League"),
                               nonce=("1" if mode else "2") * 32)
        stamp, signature, _ = signed(clock, payload)
        r = client.post("/api/internal/odds", content=payload, headers={"X-Odds-Timestamp": stamp, "X-Odds-Signature": signature})
        assert r.status_code == 200 and r.json()["wanted"] == expected and r.json()["reasons"] == {"matched": 1}
    assert client_for  # imported helper stays used
