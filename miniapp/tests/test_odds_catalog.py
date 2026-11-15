"""Odds across the whole catalogue (docs/miniapp/odds-catalog.md): the bot's list metadata matched against the Mini
App's own matches by time and team names, never a guess; the bot told which lines to price."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from miniapp.odds.feed import FeedReceiver
from miniapp.odds.store import FAR_MAX_AGE_S, FAR_REFRESH_S, MAX_AGE_S, NEAR_REFRESH_S, OddsStore
from miniapp.sports.models import Competitor, FootballEvent, Provenance
from miniapp.tests.conftest import FakeClock
from miniapp.tests.test_odds import ARSENAL, CHELSEA, ODDS, SECRET, iso, signed

SPAIN = Competitor("fd:team:760", "Spain", None)
ITALY = Competitor("fd:team:784", "Italy", None)
GENK = Competitor("fd:team:9100", "KRC Genk", "Genk")
GENT = Competitor("fd:team:9101", "KAA Gent", "Gent")


def event(home: Competitor, away: Competitor, kickoff: float, *, comp: str = "fd:UNL", mid: str = "fd:1",
          status: str = "upcoming") -> FootballEvent:
    return FootballEvent(mid, comp, iso(kickoff), status, home, away,
                         Provenance("football-data.org", "2027-01-14T08:00:00Z", None), "TIMED")


def cline(line_id: int, home: str, away: str, kickoff: float, *, country: str = "UEFA Nations League",
          league: str = "League A") -> dict:
    return {"id": line_id, "country": country, "league": league, "home": home, "away": away, "kickoff": iso(kickoff)}


def push(store: OddsStore, clock: FakeClock, *lines: dict) -> None:
    """A complete Mostbet list as the Mini App's own source stores it."""
    from miniapp.odds.feed import CatalogLine
    store.ingest_catalog([CatalogLine(ln["id"], ln["country"], ln["league"], ln["home"], ln["away"],
                                      datetime.fromisoformat(ln["kickoff"].replace("Z", "+00:00")).timestamp())
                          for ln in lines])


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
    push(store, clock, cline(10, "Genk", "Gent", t, country="Belgium", league="Pro League"))
    report = store.match_catalog([event(GENK, GENT, t, comp="fd:BJL", mid="fd:1"),
                                  event(GENK, GENT, t, comp="fd:BJPP", mid="fd:2")])
    assert report.wanted == [] and report.reasons.get("duplicate") == 2 and report.reasons.get("matched") == 0


def test_far_matches_are_refreshed_less_often_and_served_longer(clock: FakeClock) -> None:
    store = OddsStore(clock, catalog=True)
    far = clock.now + 30 * 3600
    push(store, clock, cline(10, "Spain", "Italy", far))
    assert store.match_catalog([event(SPAIN, ITALY, far)]).wanted == [(10, FAR_REFRESH_S)]
    store.ingest(FeedReceiver(SECRET, clock).verify(*signed(clock, json.dumps({"v": 1, "nonce": "d" * 32, "lines": [
        {**cline(10, "Spain", "Italy", far), "live": False, "fetchedAt": iso(clock.now - 40 * 60), "odds": ODDS}]}).encode())),
        own=True)  # as the own source stores it (the bot's prices are not stored while the catalogue governs)
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


def test_womens_youth_and_reserve_lines_never_price_a_senior_match(clock: FakeClock) -> None:
    store = OddsStore(clock, catalog=True)
    t = clock.now + 3 * 3600
    push(store, clock, cline(10, "Spain W", "Italy W", t), cline(11, "Spain U21", "Italy U21", t + 60),
         cline(12, "Spain", "Italy", t + 120, league="Women. Nations League"))
    assert store.match_catalog([event(SPAIN, ITALY, t)]).wanted == []


def test_a_line_must_come_from_the_competitions_country(clock: FakeClock) -> None:
    store = OddsStore(clock, catalog=True)
    t = clock.now + 3 * 3600
    # A Belgian cup line never prices a Dutch league match of namesakes, and a domestic line never an international one.
    push(store, clock, cline(10, "Genk", "Gent", t, country="Belgium", league="Pro League"),
         cline(11, "Spain", "Italy", t, country="Spain", league="Friendlies"),
         cline(12, "Spain", "Italy", t + 60, country="España", league="Amistosos"))     # an unknown spelling
    assert store.match_catalog([event(GENK, GENT, t, comp="fd:DED")]).wanted == []
    assert store.match_catalog([event(SPAIN, ITALY, t)]).wanted == []
    assert store.match_catalog([event(GENK, GENT, t, comp="fd:BJL")]).wanted == [(10, NEAR_REFRESH_S)]


def test_a_cup_line_prices_only_the_cup_and_a_league_line_only_the_league(clock: FakeClock) -> None:
    store = OddsStore(clock, catalog=True)
    t = clock.now + 3 * 3600
    arsenal, chelsea = Competitor("fd:team:57", "Arsenal FC", "Arsenal"), Competitor("fd:team:61", "Chelsea FC", "Chelsea")
    push(store, clock, cline(10, "Arsenal", "Chelsea", t, country="England", league="FA Cup"))
    assert store.match_catalog([event(arsenal, chelsea, t, comp="fd:EL1")]).wanted == []      # a league: never
    assert store.match_catalog([event(arsenal, chelsea, t, comp="fd:FAC")]).wanted == [(10, NEAR_REFRESH_S)]


def test_international_womens_and_youth_competitions_name_themselves_in_the_country(clock: FakeClock) -> None:
    store = OddsStore(clock, catalog=True)
    t = clock.now + 3 * 3600
    push(store, clock, cline(10, "Spain", "Italy", t, country="UEFA Women's Euro", league="Group A"),
         cline(11, "Spain", "Italy", t + 60, country="UEFA U21 Championship", league="Group B"))
    assert store.match_catalog([event(SPAIN, ITALY, t)]).wanted == []


def _priced(store: OddsStore, clock: FakeClock, line_id: int, home: str, away: str, kickoff: float, *,
            own: bool = False, **where) -> None:
    raw = {**cline(line_id, home, away, kickoff, **where), "live": False, "fetchedAt": iso(clock.now - 60), "odds": ODDS}
    store.ingest(FeedReceiver(SECRET, clock).verify(*signed(clock, json.dumps(
        {"v": 1, "nonce": f"{line_id:032d}", "lines": [raw]}).encode())), own=own)


def test_a_line_claimed_by_two_matches_is_served_for_neither(clock: FakeClock) -> None:
    store = OddsStore(clock, catalog=True)
    t = clock.now + 3 * 3600
    _priced(store, clock, 10, "Genk", "Gent", t, own=True, country="Belgium", league="Pro League")  # before the 2nd calendar
    league, cup = event(GENK, GENT, t, comp="fd:BJL", mid="fd:1"), event(GENK, GENT, t, comp="fd:BJPP", mid="fd:2")
    assert store.lookup(league) is None                    # not assigned by any catalogue match yet
    push(store, clock, cline(10, "Genk", "Gent", t, country="Belgium", league="Pro League"))
    store.match_catalog([league])
    assert store.lookup(league) is not None                # assigned to the one match
    store.match_catalog([league, cup])
    assert store.lookup(league) is None and store.lookup(cup) is None


def test_mapped_fallback_lines_keep_working_without_a_catalogue_match(clock: FakeClock) -> None:
    store = OddsStore(clock, catalog=True)
    t = clock.now + 3 * 3600
    _priced(store, clock, 10, "Arsenal", "Chelsea", t, country="England", league="Premier League")
    assert store.lookup(event(ARSENAL, CHELSEA, t, comp="fd:PL")) is not None



def test_local_words_for_womens_and_youth_football_are_known(clock: FakeClock) -> None:
    from miniapp.odds.matching import senior
    assert not senior("Frauen Bundesliga", "Bayern Munich", "VfL Wolfsburg")
    assert not senior("Liga F. Femenina", "Barcelona", "Real Madrid")
    assert not senior("Serie A Femminile", "Juventus", "Roma")
    assert not senior("Primavera 1", "Inter", "Milan")
    assert senior("Bundesliga", "Bayern Munich", "VfL Wolfsburg")


def test_a_line_gone_from_a_complete_list_is_gone_at_once(clock: FakeClock) -> None:
    from miniapp.odds.feed import CatalogLine
    store = OddsStore(clock, catalog=True)
    t = clock.now + 30 * 3600
    store.ingest_catalog([CatalogLine(10, "UEFA Nations League", "League A", "Spain", "Italy", t)])
    assert store.match_catalog([event(SPAIN, ITALY, t)]).wanted
    store.ingest_catalog([])                       # the next full list no longer has it
    assert store.match_catalog([event(SPAIN, ITALY, t)]).wanted == []



def test_while_the_list_is_fresh_the_catalogue_decides_every_line(clock: FakeClock) -> None:
    from miniapp.odds.feed import CatalogLine
    t = clock.now + 3 * 3600
    pl = event(ARSENAL, CHELSEA, t, comp="fd:PL")
    store = OddsStore(clock, catalog=True)
    _priced(store, clock, 10, "Arsenal", "Chelsea", t, country="England", league="Premier League")   # the bot's price
    england = ("England", "Premier League", "Arsenal", "Chelsea", t)
    store.ingest_catalog([CatalogLine(10, *england)])
    store.match_catalog([pl])
    assert store.lookup(pl) is None                                  # the own source governs: never the bot's price
    _priced(store, clock, 10, "Arsenal", "Chelsea", t, own=True, country="England", league="Premier League")
    assert store.lookup(pl) is not None                              # listed, uniquely matched, own price
    store.ingest_catalog([CatalogLine(10, *england), CatalogLine(11, *england)])
    store.match_catalog([pl])
    assert store.lookup(pl) is None                                  # two lines for one match: none
    store.ingest_catalog([])
    store.match_catalog([pl])
    assert store.lookup(pl) is None                                  # withdrawn from a complete list: none
    clock.advance(41 * 60)                                           # the own source stopped: the bot's feed again
    _priced(store, clock, 12, "Arsenal", "Chelsea", t, country="England", league="Premier League")
    assert store.lookup(pl) is not None



def test_czechia_is_the_feeds_czech_republic(clock: FakeClock) -> None:
    store = OddsStore(clock, catalog=True)
    t = clock.now + 3 * 3600
    croatia, czechia = Competitor("fd:team:799", "Croatia", None), Competitor("fd:team:798", "Czechia", None)
    push(store, clock, cline(10, "Croatia", "Czech Republic", t))
    assert store.match_catalog([event(croatia, czechia, t)]).wanted == [(10, NEAR_REFRESH_S)]


@pytest.mark.parametrize("feed_home", ["Manchester City", "United", "Real", "Manchester United Women", "Manchester United B",
                                       "Manchester", "Manchester United Femeni", "Inter Manchester United",
                                       "Manchester United RJ"])
def test_a_similar_name_is_never_a_match(clock: FakeClock, feed_home) -> None:
    store = OddsStore(clock, catalog=True)
    t = clock.now + 3 * 3600
    mu, ars = Competitor("fd:team:66", "Manchester United FC", "Man United"), Competitor("fd:team:57", "Arsenal FC", "Arsenal")
    push(store, clock, cline(10, feed_home, "Arsenal", t, country="England", league="Premier League"))
    assert store.match_catalog([event(mu, ars, t, comp="fd:PL")]).wanted == []


def test_vasco_is_the_feeds_vasco_da_gama_rj(clock: FakeClock) -> None:
    store = OddsStore(clock, catalog=True)
    t = clock.now + 3 * 3600
    chape, vasco = Competitor("fd:team:1772", "Chapecoense AF", "Chapecoense"), Competitor("fd:team:1780", "CR Vasco da Gama", None)
    push(store, clock, cline(10, "Chapecoense", "Vasco da Gama RJ", t, country="Brazil", league="Serie A"))
    assert store.match_catalog([event(chape, vasco, t, comp="fd:BSA")]).wanted == [(10, NEAR_REFRESH_S)]


def test_aliases_from_the_unmatched_log(clock: FakeClock) -> None:
    store = OddsStore(clock, catalog=True)
    t = clock.now + 3 * 3600
    h, a = Competitor("fd:team:11346", "CD Moquegua", None), Competitor("fd:team:8741", "CA Grau", None)
    push(store, clock, cline(10, "Deportivo Moquegua", "Atletico Grau", t, country="Peru", league="Liga 1"))
    assert store.match_catalog([event(h, a, t, comp="fd:PPD")]).wanted == [(10, NEAR_REFRESH_S)]
    other = OddsStore(clock, catalog=True)                              # the same names from another country
    push(other, clock, cline(11, "Deportivo Moquegua", "Atletico Grau", t, country="Uruguay", league="Primera Division"))
    assert other.match_catalog([event(h, a, t, comp="fd:PPD")]).wanted == []


def test_the_rest_of_the_week_refreshes_less_often_and_serves_at_most_an_hour_per_load(clock: FakeClock) -> None:
    from miniapp.odds.store import WEEK_REFRESH_S
    store = OddsStore(clock, catalog=True)
    t = clock.now + 4 * 86400
    push(store, clock, cline(10, "Spain", "Italy", t))
    assert store.match_catalog([event(SPAIN, ITALY, t)]).wanted == [(10, WEEK_REFRESH_S)]
