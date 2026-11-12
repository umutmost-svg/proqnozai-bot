"""Odds from the bot's feed: margin removal, checks, strict matching, signed push — offline."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from miniapp.app import create_app
from miniapp.odds import margin
from miniapp.odds.book import read_book
from miniapp.odds.feed import MAX_BODY_BYTES, TRANSIT_ALLOWANCE_S, FeedError, FeedReceiver, sign
from miniapp.odds.matching import Candidate, load_leagues, match, team_key
from miniapp.odds.store import MAX_AGE_S, OddsStore
from miniapp.odds.value import compare
from miniapp.sports.models import Competitor
from miniapp.tests.conftest import FakeClock
from miniapp.tests.test_ai_analysis import FakeModel, make_ai
from miniapp.tests.test_sports import DAY, base_settings, run

SECRET = b"s" * 40
KICKOFF = f"{DAY}T15:00:00Z"
ARSENAL = Competitor("fd:team:57", "Arsenal FC", "Arsenal")
CHELSEA = Competitor("fd:team:61", "Chelsea FC", "Chelsea")
BAYERN = Competitor("fd:team:5", "FC Bayern München", "Bayern")
DORTMUND = Competitor("fd:team:4", "Borussia Dortmund", "Dortmund")
ODDS = {"w1": 1.80, "x": 3.80, "w2": 4.50, "dc_1x": 1.22, "dc_x2": 2.00, "dc_12": 1.25,
        "over25": 1.75, "under25": 2.10, "btts_yes": 1.70, "btts_no": 2.15, "hcp_w1": 1.9, "dnb_w1": 1.3}


def ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- margin removal ----------------------------------------------------------------------------

BOOKS = [[1.5, 4.0, 7.0], [2.1, 3.4, 3.6], [1.18, 7.5, 15.0], [1.9, 1.9], [1.3, 3.4]]


@pytest.mark.parametrize("method", [margin.proportional, margin.power, margin.shin])
@pytest.mark.parametrize("odds", BOOKS)
def test_every_method_sums_to_one_and_keeps_the_order(method, odds) -> None:
    p = method(odds)
    assert math.isclose(sum(p), 1.0, abs_tol=1e-12)
    assert sorted(range(len(p)), key=lambda i: -p[i]) == sorted(range(len(odds)), key=lambda i: odds[i])
    assert all(0 < x < 1 for x in p)


@pytest.mark.parametrize("method", [margin.proportional, margin.power, margin.shin])
def test_a_book_without_margin_is_left_as_it_is(method) -> None:
    fair = [0.5, 0.3, 0.2]
    assert [round(x, 12) for x in method([1 / x for x in fair])] == fair


def test_power_is_one_exponent_for_every_outcome() -> None:
    odds = [1.5, 4.0, 7.0]
    p = margin.power(odds)
    exponents = [math.log(pi) / math.log(1 / o) for pi, o in zip(p, odds)]
    assert max(exponents) - min(exponents) < 1e-9 and exponents[0] > 1


def test_shin_is_one_insider_share_for_every_outcome() -> None:
    odds = [1.5, 4.0, 7.0]
    q = [1 / o for o in odds]
    s = sum(q)
    p = margin.shin(odds)
    # Shin's inverse: q_i² / S = z·p_i + (1 − z)·p_i², the same z for every outcome.
    zs = [(qi * qi / s - pi * pi) / (pi - pi * pi) for qi, pi in zip(q, p)]
    assert max(zs) - min(zs) < 1e-9 and 0 < zs[0] < 0.1


def test_power_and_shin_take_more_margin_from_the_long_shot() -> None:
    odds = [1.5, 4.0, 7.0]
    prop, power, shin = margin.proportional(odds), margin.power(odds), margin.shin(odds)
    assert power[2] < prop[2] and shin[2] < prop[2]
    assert power[0] > prop[0] and shin[0] > prop[0]


@pytest.mark.parametrize("odds", [[1.0, 3.0], [0.5, 2.0], [math.inf, 2.0], [math.nan, 2.0], [2.0], [3.0, 3.0, 3.0, 3.0, 3.0]])
def test_invalid_or_arbitrage_books_raise(odds) -> None:
    # [3.0 × 5] sums 5/3 > 1 and is valid; the others are not.
    if odds == [3.0] * 5:
        assert math.isclose(sum(margin.shin(odds)), 1.0)
        return
    with pytest.raises(ValueError):
        margin.shin(odds)
    with pytest.raises(ValueError):
        margin.power(odds)


def test_a_book_below_one_is_refused_not_scaled_up() -> None:
    with pytest.raises(ValueError):
        margin.proportional([2.2, 2.2])


# --- the book of one match ---------------------------------------------------------------------

def test_only_complete_plausible_groups_are_kept_in_our_keys() -> None:
    book = read_book(ODDS)
    assert book == {"1": 1.8, "X": 3.8, "2": 4.5, "1X": 1.22, "X2": 2.0, "12": 1.25,
                    "O2.5": 1.75, "U2.5": 2.1, "BTTS_Y": 1.7, "BTTS_N": 2.15}


@pytest.mark.parametrize("change", [
    {"w2": None}, {"w2": "4.5"}, {"w2": True}, {"w2": 1.0}, {"w2": 1.005}, {"w2": 250.0}, {"w2": 100.004},
    {"w2": math.nan},
    {"w1": 3.0, "x": 3.5, "w2": 3.5},  # Σ1/odds < 1: not one consistent book
    {"w1": 1.2, "x": 2.0, "w2": 2.0},  # 33 % margin: implausible
])
def test_a_failing_group_is_dropped_whole_and_the_others_stay(change) -> None:
    book = read_book({**ODDS, **change})
    assert not {"1", "X", "2"} & set(book) and book["O2.5"] == 1.75


# --- strict matching --------------------------------------------------------------------------

LEAGUES = load_leagues()


def cand(home="Arsenal", away="Chelsea", *, comp="PL", kickoff=KICKOFF, line=1) -> Candidate:
    return Candidate(line, comp, ts(kickoff), home, away)


def find(*cands: Candidate, home=ARSENAL, away=CHELSEA, comp="PL"):
    return match(cands, competition=comp, kickoff=ts(KICKOFF), home=home, away=away, table=LEAGUES)


def test_the_competition_pair_is_exact_never_a_substring() -> None:
    assert LEAGUES.code("England", "Premier League") == "PL"
    assert LEAGUES.code("ENGLAND", "premier-league") == "PL"
    assert LEAGUES.code("Ukraine", "Premier League") is None
    assert LEAGUES.code("England", "Premier League 2") is None
    assert LEAGUES.code("Brazil", "Serie A") == "BSA" and LEAGUES.code("Italy", "Serie A") == "SA"
    assert LEAGUES.code("Austria", "Bundesliga") is None and LEAGUES.code("Germany", "2. Bundesliga") is None
    assert LEAGUES.code("UEFA Champions League", "League phase") == "CL"
    # No wildcard stage: a women's, youth, unknown or empty stage of the same name is no match.
    for stage in ("Women", "U21", "Youth League", "", "Qualification"):
        assert LEAGUES.code("UEFA Champions League", stage) is None
    assert LEAGUES.code("Champions League", "League phase") is None


def test_the_live_feed_names_of_2026_11_12_map_to_their_competitions() -> None:
    """Names as the Mostbet feed really spells them (window of 2026-11-12): a season suffix is dropped,
    Eredivisie and Primeira Liga are 'Premier League' of their country, the CL country is reversed."""
    live = {("England", "Premier League. Season 26/27"): "PL", ("Italy", "Serie A. Season 26/27"): "SA",
            ("Spain", "Primera Division. Season 26/27"): "PD", ("Germany", "Bundesliga. Season 26/27"): "BL1",
            ("England", "Championship"): "ELC", ("France", "Ligue 1"): "FL1", ("Brazil", "Serie A"): "BSA",
            ("Netherlands", "Premier League"): "DED", ("Portugal", "Premier League"): "PPL",
            ("Champions League UEFA", "League phase"): "CL"}
    for (country, league), code in live.items():
        assert LEAGUES.code(country, league) == code, (country, league)
    # Only a trailing season goes: other suffixes and other countries stay unmatched.
    for country, league in [("Ukraine", "Premier League. Season 26/27"), ("Germany", "Bundesliga 2"),
                            ("England", "FA Trophy. Round 128"), ("Russia", "Premier League. Season 26/27"),
                            ("Europe League UEFA", "League phase"), ("England", "Isthmian. Premier League"),
                            ("England", "Season 26/27")]:
        assert LEAGUES.code(country, league) is None, (country, league)


def test_names_match_through_the_short_name_club_forms_and_aliases() -> None:
    assert team_key("Arsenal FC") == team_key("arsenal") == "arsenal"
    assert find(cand("Arsenal", "Chelsea"))[1] == "matched"
    assert find(cand("Bayern Munich", "Borussia Dortmund", comp="BL1"), home=BAYERN, away=DORTMUND, comp="BL1")[1] == "matched"
    assert find(cand("Bayern München", "Dortmund", comp="BL1"), home=BAYERN, away=DORTMUND, comp="BL1")[1] == "matched"


@pytest.mark.parametrize(("line", "reason"), [
    (cand("Chelsea", "Arsenal"), "reversed"),
    (cand("Arsenal", "Chelsea U21"), "unknown_names"),
    (cand("Arsenal Women", "Chelsea Women"), "unknown_names"),
    (cand("Arsenal", "Chelsea", comp="PD"), "no_line"),
    (cand("Arsenal", "Chelsea", kickoff=f"{DAY}T15:16:00Z"), "no_line"),
])
def test_anything_short_of_an_exact_match_gives_no_odds(line, reason) -> None:
    assert find(line) == (None, reason)


def test_kickoff_within_fifteen_minutes_matches() -> None:
    assert find(cand(kickoff=f"{DAY}T15:15:00Z"))[1] == "matched"
    assert find(cand(kickoff=f"{DAY}T14:45:00Z"))[1] == "matched"


def test_two_candidates_or_a_reversed_twin_give_nothing() -> None:
    assert find(cand(line=1), cand(line=2)) == (None, "ambiguous")
    assert find(cand(line=1), cand("Chelsea", "Arsenal", line=2)) == (None, "reversed")


def test_a_cl_line_of_another_category_never_prices_the_mens_match(clock: FakeClock) -> None:
    from miniapp.sports.models import FootballEvent, Provenance
    store = OddsStore(clock)
    cl_event = FootballEvent("fd:9", "fd:CL", KICKOFF, "upcoming", ARSENAL, CHELSEA,
                             Provenance("football-data.org", "2027-01-14T08:00:00Z", None), "TIMED")
    ingest(store, clock, line(country="UEFA Champions League", league="Women"))
    assert store.lookup(cl_event) is None
    store.ingest(FeedReceiver(SECRET, clock).verify(*signed(clock, body(
        clock, line(id=102, country="UEFA Champions League", league="League phase"), nonce="9" * 32))))
    assert store.lookup(cl_event).line_id == 102


def test_a_wildcard_stage_in_the_table_is_refused(tmp_path) -> None:
    path = tmp_path / "l.json"
    path.write_text(json.dumps({"competitions": [{"code": "CL", "country": ["UEFA Champions League"], "anyLeague": True,
                                                  "league": []}]}))
    with pytest.raises(ValueError):
        load_leagues(path)


def test_the_leagues_table_never_maps_one_pair_to_two_competitions(tmp_path) -> None:
    bad = {"competitions": [{"code": "PL", "country": ["England"], "league": ["X"]},
                            {"code": "ELC", "country": ["England"], "league": ["x"]}]}
    path = tmp_path / "l.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        load_leagues(path)


# --- the signed push ---------------------------------------------------------------------------

def line(**over) -> dict:
    base = {"id": 101, "country": "England", "league": "Premier League", "home": "Arsenal", "away": "Chelsea",
            "kickoff": KICKOFF, "live": False, "fetchedAt": None, "odds": ODDS}
    base.update(over)
    return base


def body(clock: FakeClock, *lines: dict, nonce: str = "a" * 32) -> bytes:
    filled = [{**ln, "fetchedAt": ln["fetchedAt"] or iso(clock.now - 60)} for ln in lines]
    return json.dumps({"v": 1, "nonce": nonce, "lines": filled}).encode()


def signed(clock: FakeClock, payload: bytes, *, secret: bytes = SECRET, at: float | None = None) -> tuple[str, str, bytes]:
    stamp = str(int(clock.now if at is None else at))
    return stamp, sign(secret, stamp, payload), payload


def test_a_signed_push_is_read(clock: FakeClock) -> None:
    receiver = FeedReceiver(SECRET, clock)
    lines = receiver.verify(*signed(clock, body(clock, line())))
    assert len(lines) == 1 and lines[0].book["1"] == 1.8 and lines[0].kickoff == ts(KICKOFF)


@pytest.mark.parametrize(("tamper", "code"), [
    (lambda c, s, b: (s, "0" * 64, b), "bad_signature"),
    (lambda c, s, b: (s, sign(b"x" * 40, s, b), b), "bad_signature"),
    (lambda c, s, b: (s, sign(SECRET, s, b), b + b" "), "bad_signature"),
    (lambda c, s, b: (None, sign(SECRET, s, b), b), "bad_signature"),
    (lambda c, s, b: ("-5", sign(SECRET, "-5", b), b), "bad_signature"),
    (lambda c, s, b: (str(int(c.now) - 200), sign(SECRET, str(int(c.now) - 200), b), b), "stale"),
    (lambda c, s, b: (str(int(c.now) + 200), sign(SECRET, str(int(c.now) + 200), b), b), "stale"),
])
def test_unsigned_wrongly_signed_or_old_pushes_are_refused(clock: FakeClock, tamper, code) -> None:
    receiver = FeedReceiver(SECRET, clock)
    stamp, _, payload = signed(clock, body(clock, line()))
    with pytest.raises(FeedError) as e:
        receiver.verify(*tamper(clock, stamp, payload))
    assert e.value.code == code


def test_a_replayed_push_is_refused(clock: FakeClock) -> None:
    receiver = FeedReceiver(SECRET, clock)
    push = signed(clock, body(clock, line()))
    receiver.verify(*push)
    with pytest.raises(FeedError) as e:
        receiver.verify(*push)
    assert e.value.code == "replayed"


@pytest.mark.parametrize("payload", [b"not json", b"[]", b'{"v": 2, "nonce": "' + b"a" * 32 + b'", "lines": []}',
                                     b'{"v": 1, "nonce": "short", "lines": []}', b'{"v": 1, "nonce": "' + b"a" * 32 + b'"}'])
def test_a_signed_but_malformed_payload_is_refused(clock: FakeClock, payload) -> None:
    with pytest.raises(FeedError) as e:
        FeedReceiver(SECRET, clock).verify(*signed(clock, payload))
    assert e.value.code == "bad_payload"


def test_a_malformed_line_is_skipped_on_its_own(clock: FakeClock) -> None:
    future = iso(clock.now + 600)
    lines = FeedReceiver(SECRET, clock).verify(*signed(clock, body(
        clock, line(), line(id=0), line(id=True), line(home=""), line(kickoff="2027-01-15 15:00"),
        line(live="no"), line(odds=[1, 2]), line(id=102, fetchedAt=future), line(id=103, home="x" * 200))))
    assert [ln.line_id for ln in lines] == [101]


def test_a_too_large_push_is_refused_before_anything_else(clock: FakeClock) -> None:
    with pytest.raises(FeedError) as e:
        FeedReceiver(SECRET, clock).verify("1", "x", b" " * (MAX_BODY_BYTES + 1))
    assert e.value.code == "too_large"


def test_a_short_secret_is_refused() -> None:
    with pytest.raises(ValueError):
        FeedReceiver(b"short", lambda: 0.0)


# --- the store ----------------------------------------------------------------------------------

def _event(status: str):
    from miniapp.sports.models import FootballEvent, Provenance
    return FootballEvent("fd:7", "fd:PL", KICKOFF, "upcoming" if status == "TIMED" else "live", ARSENAL, CHELSEA,
                         Provenance("football-data.org", "2027-01-14T08:00:00Z", None), status)


def ingest(store: OddsStore, clock: FakeClock, *lines: dict) -> None:
    store.ingest(FeedReceiver(SECRET, clock).verify(*signed(clock, body(clock, *lines))))


def test_a_fresh_strictly_matched_line_gives_the_odds(clock: FakeClock) -> None:
    store = OddsStore(clock)
    ingest(store, clock, line(), line(id=5, country="Ukraine"))
    found = store.lookup(_event("TIMED"))
    assert found is not None and found.line_id == 101 and found.book["2"] == 4.5
    assert len(store) == 1


def test_old_prices_a_started_match_or_a_live_line_give_nothing(clock: FakeClock) -> None:
    store = OddsStore(clock)
    ingest(store, clock, line())  # fetched 60 s before the push
    # 15 minutes less the delivery allowance, counted from the fetch (60 s before the push).
    assert store.lookup(_event("TIMED")).valid_for == MAX_AGE_S - TRANSIT_ALLOWANCE_S - 60
    clock.advance(MAX_AGE_S - TRANSIT_ALLOWANCE_S - 60)  # no longer served, even far from kickoff
    assert store.lookup(_event("TIMED")) is None
    store2 = OddsStore(clock)
    ingest(store2, clock, line(live=True, fetchedAt=iso(clock.now - 30)))
    assert store2.lookup(_event("TIMED")) is None
    assert store2.lookup(_event("IN_PLAY")) is None


def test_an_older_push_never_replaces_newer_prices(clock: FakeClock) -> None:
    store = OddsStore(clock)
    ingest(store, clock, line(fetchedAt=iso(clock.now - 30), odds={**ODDS, "w1": 1.9}))
    old = body(clock, line(fetchedAt=iso(clock.now - 90)), nonce="b" * 32)
    store.ingest(FeedReceiver(SECRET, clock).verify(*signed(clock, old)))
    assert store.lookup(_event("TIMED")).book["1"] == 1.9


def test_lines_are_dropped_after_their_kickoff(clock: FakeClock) -> None:
    store = OddsStore(clock)
    ingest(store, clock, line())
    clock.now = ts(KICKOFF) + 4 * 3600
    store.ingest([])
    assert len(store) == 0


# --- model against the market -------------------------------------------------------------------

def test_market_probabilities_margins_and_expected_values() -> None:
    model = {"1": 0.60, "X": 0.24, "2": 0.16, "1X": 0.84, "X2": 0.40, "12": 0.76, "O2.5": 0.5, "U2.5": 0.5,
             "BTTS_Y": 0.55, "BTTS_N": 0.45}
    quotes = compare(read_book(ODDS), model)
    fair = margin.shin([1.8, 3.8, 4.5])
    assert math.isclose(quotes["1"].market, fair[0]) and math.isclose(quotes["1X"].market, fair[0] + fair[1])
    assert math.isclose(quotes["1"].margin, 1 / 1.8 + 1 / 3.8 + 1 / 4.5 - 1)
    assert math.isclose(quotes["1X"].margin, 1 / 1.22 + 1 / 2.0 + 1 / 1.25 - 2)
    assert math.isclose(quotes["1"].expected_value, 0.60 * 1.8 - 1) and math.isclose(quotes["1"].edge, 0.60 - fair[0])
    assert math.isclose(quotes["O2.5"].market, margin.power([1.75, 2.1])[0])
    assert quotes["1"].to_json() == {"market": "1", "odds": 1.8, "impliedProbability": 55.6,
                                     "marketProbability": round(fair[0] * 100, 1), "modelProbability": 60.0,
                                     "edge": round((0.60 - fair[0]) * 100, 1), "expectedValue": 8.0,
                                     "margin": round((1 / 1.8 + 1 / 3.8 + 1 / 4.5 - 1) * 100, 1)}


def test_double_chance_without_a_1x2_book_has_no_market_probability() -> None:
    book = {k: v for k, v in read_book(ODDS).items() if k not in ("1", "X", "2")}
    assert "1X" not in compare(book, {"1X": 0.8, "X2": 0.4, "12": 0.8})


# --- HTTP and the AI analysis -------------------------------------------------------------------

def client_for(tmp_path, clock: FakeClock, secret: str | None = "s" * 40) -> TestClient:
    settings = base_settings(tmp_path, odds_feed_secret=secret)
    return TestClient(create_app(settings, clock=clock), base_url="http://127.0.0.1:8000")


def post(client: TestClient, clock: FakeClock, payload: bytes, *, secret: bytes = SECRET):
    stamp, signature, _ = signed(clock, payload, secret=secret)
    return client.post("/api/internal/odds", content=payload,
                       headers={"X-Odds-Timestamp": stamp, "X-Odds-Signature": signature})


def test_the_push_route_reads_signed_pushes_and_answers_only_counts(tmp_path, clock: FakeClock) -> None:
    client = client_for(tmp_path, clock)
    r = post(client, clock, body(clock, line(), line(id=5, country="Ukraine")))
    assert r.status_code == 200 and r.json() == {"received": 2, "accepted": 1, "unknownCompetition": 1, "withoutOdds": 0}
    assert post(client, clock, body(clock, line(), nonce="c" * 32), secret=b"x" * 40).status_code == 401
    assert client.post("/api/internal/odds", content=b"{}").status_code == 401
    big = b" " * (MAX_BODY_BYTES + 1)
    assert client.post("/api/internal/odds", content=big).status_code == 413


def test_without_a_secret_the_route_does_not_exist(tmp_path, clock: FakeClock) -> None:
    client = client_for(tmp_path, clock, secret=None)
    assert post(client, clock, body(clock, line())).status_code == 404


def test_unsigned_traffic_never_uses_up_the_real_senders_allowance(tmp_path, clock: FakeClock) -> None:
    client = client_for(tmp_path, clock)
    assert all(client.post("/api/internal/odds", content=b"{}").status_code == 401 for _ in range(100))
    assert post(client, clock, body(clock, line())).status_code == 200


def test_signed_pushes_are_limited_per_minute(tmp_path, clock: FakeClock) -> None:
    client = client_for(tmp_path, clock)
    codes = [post(client, clock, body(clock, line(), nonce=f"{i:032x}")).status_code for i in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429
    clock.advance(61)
    assert post(client, clock, body(clock, line(), nonce="f" * 32)).status_code == 200


def test_an_analysis_carries_the_current_prices_and_is_complete_with_them(clock: FakeClock) -> None:
    ai, _ = make_ai(clock, FakeModel())
    store = OddsStore(clock)
    ai.odds = store
    without = run(ai.analyse("fd:7", user_id=1))
    assert without.market is None and without.kind == "partial" and without.reasons == ("no_odds_source",)
    ingest(store, clock, line())
    priced = run(ai.analyse("fd:7", user_id=1))  # cached explanation, fresh prices: no new model call
    assert priced.kind == "ready" and priced.reasons == () and priced.estimated is False
    assert priced.factors == without.factors and len(ai.client.calls) == 1
    quotes = {q["market"]: q for q in priced.market["quotes"]}
    assert set(quotes) == {"1", "X", "2", "1X", "X2", "12", "O2.5", "U2.5", "BTTS_Y", "BTTS_N"}
    assert quotes["1"]["odds"] == 1.8 and priced.market["fetchedAt"] == iso(clock.now - 60)
    assert priced.to_json()["market"] == priced.market


# --- quick prediction and coupons ----------------------------------------------------------------

def test_quick_legs_carry_their_price_and_a_coupon_only_a_computed_product(clock: FakeClock) -> None:
    from miniapp.forecast.quick import Criteria
    from miniapp.quick.service import QuickService
    from miniapp.tests.test_quick import StubForecaster, StubSports, at, ev, forecast

    clock.now = ts(f"{DAY}T08:00:00Z")
    events = [ev(1), ev(2, home=(1, "A FC"), away=(2, "B FC"))]
    by_id = {"fd:1": forecast(("1", 0.64, 0.45)), "fd:2": forecast(("1", 0.64, 0.45))}
    store = OddsStore(clock)
    svc = QuickService(StubSports(events), clock=clock, forecaster=StubForecaster(by_id), odds=store)  # type: ignore[arg-type]

    def coupon() -> dict:
        return run(svc.pick(1, at(f"{DAY}T08:00:00Z"), at(f"{DAY}T23:00:00Z"), None,
                            Criteria("coupon", "any", "balanced", 2))).to_json()

    ingest(store, clock, line())  # only the first match is priced
    first = coupon()
    prices = {leg["event"]["id"]: leg["price"] for leg in first["legs"]}
    assert prices["fd:1"]["odds"] == 1.8 and prices["fd:1"]["market"] == "1" and prices["fd:2"] is None
    assert first["couponOdds"] is None  # not every leg has a price
    clock.advance(30)
    second = line(id=202, home="A FC", away="B FC", odds={**ODDS, "w1": 2.0, "x": 3.4, "w2": 3.6})
    store.ingest(FeedReceiver(SECRET, clock).verify(*signed(clock, body(clock, second, nonce="d" * 32))))
    clock.advance(60)
    full = coupon()
    assert full["couponOdds"] == {"odds": 3.6, "fetchedAt": iso(clock.now - 90 - 60),
                                  "validFor": int(MAX_AGE_S - TRANSIT_ALLOWANCE_S - 150)}
    assert "expectedValue" not in full["couponOdds"]  # no value claimed for a combination
    single = run(svc.pick(1, at(f"{DAY}T08:00:00Z"), at(f"{DAY}T23:00:00Z"), None,
                          Criteria("single", "any", "balanced", 1))).to_json()
    assert single["couponOdds"] is None and single["legs"][0]["price"] is not None


def test_prices_stop_well_before_the_earlier_of_both_kickoffs(clock: FakeClock) -> None:
    store = OddsStore(clock)
    clock.now = ts(f"{DAY}T14:25:30Z")
    # The line says 14:50, football-data 15:00: matched (10 min apart). Fetched at 14:25, its age
    # would allow 14:40, but prices stop CUTOFF_BEFORE_KICKOFF_S before the earlier kickoff: 14:35.
    ingest(store, clock, line(kickoff=f"{DAY}T14:50:00Z", fetchedAt=f"{DAY}T14:25:00Z"))
    assert int(store.lookup(_event("TIMED")).valid_for) == 570
    clock.now = ts(f"{DAY}T14:34:00Z")
    assert int(store.lookup(_event("TIMED")).valid_for) == 60
    clock.now = ts(f"{DAY}T14:35:00Z")
    assert store.lookup(_event("TIMED")) is None


def test_prices_are_used_in_cents_so_every_figure_agrees_with_the_shown_odds() -> None:
    book = read_book({**ODDS, "w1": 1.735, "x": 3.8049, "w2": 4.5})
    assert book["1"] in (1.73, 1.74) and book["X"] == 3.8
    q = compare(book, {"1": 0.6, "X": 0.24, "2": 0.16})["1"].to_json()
    assert q["impliedProbability"] == round(100 / q["odds"], 1)
    assert q["expectedValue"] == round((0.6 * q["odds"] - 1) * 100, 1)


def test_under_a_second_of_validity_is_not_served(clock: FakeClock) -> None:
    store = OddsStore(clock)
    clock.now = ts(f"{DAY}T14:44:58Z")  # 2 s before the cutoff (15 min before kickoff)
    ingest(store, clock, line(fetchedAt=iso(clock.now - 30)))
    assert int(store.lookup(_event("TIMED")).valid_for) == 2
    clock.now += 1.5
    assert store.lookup(_event("TIMED")) is None  # 0.5 s left: never sent as "valid for 0"


def test_a_live_marker_replaces_pre_match_prices_at_once(clock: FakeClock) -> None:
    store = OddsStore(clock)
    ingest(store, clock, line())
    assert store.lookup(_event("TIMED")) is not None
    clock.advance(30)
    marker = line(live=True, odds={}, fetchedAt=iso(clock.now))
    store.ingest(FeedReceiver(SECRET, clock).verify(*signed(clock, body(clock, marker, nonce="e" * 32))))
    assert store.lookup(_event("TIMED")) is None


def test_a_double_chance_book_that_contradicts_the_1x2_book_is_dropped() -> None:
    book = read_book({"w1": 1.5, "x": 4.0, "w2": 7.0, "dc_1x": 1.8, "dc_x2": 1.8, "dc_12": 1.05})
    assert {"1", "X", "2"} <= set(book) and not {"1X", "X2", "12"} & set(book)
    assert {"1X", "X2", "12"} <= set(read_book(ODDS))  # a consistent one stays


@pytest.mark.parametrize("skew", [-110, -3, 3, 60, 110])
def test_a_bot_clock_off_by_up_to_the_signature_window_keeps_the_same_freshness(clock: FakeClock, skew) -> None:
    store = OddsStore(clock)
    bot_now = clock.now + skew
    payload = json.dumps({"v": 1, "nonce": "c" * 32, "lines": [line(fetchedAt=iso(bot_now - 60))]}).encode()
    stamp = str(int(bot_now))
    lines = FeedReceiver(SECRET, clock).verify(stamp, sign(SECRET, stamp, payload), payload)
    assert len(lines) == 1
    store.ingest(lines)
    found = store.lookup(_event("TIMED"))
    # A bot clock AHEAD (skew > 0) is corrected exactly: 60 s old by its clock = 60 s here. A bot
    # clock behind (skew < 0) looks like transit time and is never credited: shorter, never longer.
    expected = MAX_AGE_S - 60 - max(-skew, 0) - TRANSIT_ALLOWANCE_S
    assert found is not None and abs(found.valid_for - expected) <= 1
    assert found.valid_for <= MAX_AGE_S - 60 - TRANSIT_ALLOWANCE_S + 1


def test_a_fetch_time_after_the_push_itself_is_refused(clock: FakeClock) -> None:
    stamp = str(int(clock.now))
    payload = json.dumps({"v": 1, "nonce": "d" * 32, "lines": [line(fetchedAt=iso(clock.now + 30))]}).encode()
    assert FeedReceiver(SECRET, clock).verify(stamp, sign(SECRET, stamp, payload), payload) == []


def test_an_expired_old_copy_of_a_fixture_does_not_block_a_fresh_one(clock: FakeClock) -> None:
    store = OddsStore(clock)
    ingest(store, clock, line(id=301))
    clock.advance(MAX_AGE_S + 60)  # the old line id is expired (still stored until kickoff)
    fresh = body(clock, line(id=302, fetchedAt=iso(clock.now - 10)), nonce="7" * 32)
    store.ingest(FeedReceiver(SECRET, clock).verify(*signed(clock, fresh)))
    assert store.lookup(_event("TIMED")).line_id == 302


def test_the_kill_switch_answers_signed_pushes_with_410_and_shows_no_odds(tmp_path, clock: FakeClock) -> None:
    settings = base_settings(tmp_path, odds_feed_secret="s" * 40, odds_feed_enabled=False)
    client = TestClient(create_app(settings, clock=clock), base_url="http://127.0.0.1:8000")
    assert post(client, clock, body(clock, line())).status_code == 410
    assert client.post("/api/internal/odds", content=b"{}").status_code == 401  # unsigned: still 401
    store = OddsStore(clock, enabled=False)
    ingest(store, clock, line())
    assert store.lookup(_event("TIMED")) is None


def test_a_repeated_live_marker_is_idempotent(clock: FakeClock) -> None:
    store = OddsStore(clock)
    ingest(store, clock, line())
    for i in range(3):
        clock.advance(10)
        marker = line(live=True, odds={}, fetchedAt=iso(clock.now))
        store.ingest(FeedReceiver(SECRET, clock).verify(*signed(clock, body(clock, marker, nonce=f"{i:032x}"))))
        assert store.lookup(_event("TIMED")) is None and len(store) == 1


# --- the match page ------------------------------------------------------------------------------

def test_the_match_page_carries_fresh_odds_as_numbers_only(clock: FakeClock) -> None:
    from miniapp.sports.routes import _event_odds
    store = OddsStore(clock)
    assert _event_odds(store, _event("TIMED")) is None          # nothing pushed yet
    assert _event_odds(None, _event("TIMED")) is None           # feed switched off
    ingest(store, clock, line())
    got = _event_odds(store, _event("TIMED"))
    assert got is not None and got["fetchedAt"] == iso(clock.now - 60) and 0 < got["validFor"] <= 15 * 60
    quotes = {q["market"]: q for q in got["quotes"]}
    assert set(quotes) == {"1", "X", "2", "1X", "X2", "12", "O2.5", "U2.5", "BTTS_Y", "BTTS_N"}
    assert quotes["1"]["odds"] == 1.8 and 0 < quotes["1"]["marketProbability"] < 100
    assert set(quotes["1"]) == {"market", "odds", "marketProbability", "margin"}  # never the source's name
    assert "mostbet" not in json.dumps(got).lower()
    assert _event_odds(store, _event("IN_PLAY")) is None        # a started match shows no pre-match odds
    clock.advance(16 * 60)
    assert _event_odds(store, _event("TIMED")) is None          # stale prices are not shown


def test_the_market_book_keeps_compare_unchanged(clock: FakeClock) -> None:
    from miniapp.odds.value import book_json, compare, market_book
    book = {"1": 1.8, "X": 3.9, "2": 4.5, "O2.5": 1.9, "U2.5": 1.95}
    model = {"1": 0.5, "X": 0.25, "2": 0.25, "O2.5": 0.55, "U2.5": 0.45}
    quotes = compare(book, model)
    view = {q["market"]: q for q in book_json(book)}
    for k, (price, fair, group_margin) in market_book(book).items():
        assert quotes[k].odds == price and quotes[k].market == fair and quotes[k].margin == group_margin
        assert view[k]["marketProbability"] == round(fair * 100, 1)
