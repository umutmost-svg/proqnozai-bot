"""Ads module, stage 6: verified odds in offers, the AI recommended-bet placement, frequency caps, global display
settings, click ids, partner postbacks and the partner API hook, viewer classes and daily aggregates."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from miniapp.ads import engine, odds_source, policy
from miniapp.ads.partner_api import PartnerConversion, ingest
from miniapp.ads.routes import postback_token_hash
from miniapp.ads.telemetry import Telemetry, day_of, telemetry_path
from miniapp.app import create_app
from miniapp.tests.conftest import NOW, FakeClock, login
from miniapp.tests.signing import make_init_data
from miniapp.tests.test_ads_miniapp import DAY, EDGE, KEY, OTHER, USER, campaign, post, settings

PB_TOKEN = "t" * 40


def publish(tmp_path: Path, *campaigns: dict, version: int = 10, extra: dict | None = None) -> None:
    body = {"schema": 1, "version": version, "generatedAt": NOW, "campaigns": list(campaigns), "audiences": {},
            **(extra or {})}
    policy.write_atomic(tmp_path / "ads-policy.json", policy.canonical(policy.sign(body, KEY)))


class Prices:
    """The verified odds store as the ads routes see it: (event, market) -> price, switchable per test."""

    def __init__(self) -> None:
        self.book: dict[tuple[str, str], float] = {}
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, event_id: str, market: str) -> dict | None:
        self.calls.append((event_id, market))
        price = self.book.get((event_id, market))
        if price is None:
            return None
        return {"market": market, "odds": price, "fetchedAt": "2027-01-15T08:00:00Z", "validFor": 600}


class FakeAi:
    connected = True

    def __init__(self) -> None:
        self.market = "1X"

    async def analyse(self, event_id: str, user_id: int, create: bool = True):
        pick = SimpleNamespace(market=self.market)
        return SimpleNamespace(event_id=event_id, pick=pick, to_json=lambda: {"eventId": event_id, "pick": self.market})


class Env:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **over) -> None:
        self.tmp = tmp_path
        self.clock = FakeClock()
        self.prices = Prices()
        monkeypatch.setattr(odds_source, "make_odds_lookup", lambda event_of, odds: self.prices)
        self.ai = FakeAi()
        self.app = create_app(settings(tmp_path, **over), clock=self.clock, ai=self.ai)

    def user(self, uid: int = USER, country: str | None = "AZ") -> tuple[TestClient, str]:
        headers = {"X-Edge-Auth": EDGE}
        if country:
            headers["CF-IPCountry"] = country
        c = TestClient(self.app, base_url="http://127.0.0.1:8000", headers=headers)
        r = login(c, make_init_data({"id": uid, "first_name": "U", "language_code": "ru"}, auth_date=int(self.clock.now)))
        assert r.status_code == 200, r.text
        c.uid = uid  # type: ignore[attr-defined]
        return c, r.json()["session"]["csrf_token"]

    def pick(self, c: TestClient, event: str = "fd:1", market: str | None = "1", placement: str = "quick_pick"):
        self.app.state.ads_picks.add(c.uid, [(event, market)], self.clock.now, placement)

    def offer(self, c: TestClient, event: str = "fd:1", placement: str = "quick_pick"):
        return c.get("/api/ads/offer", params={"placement": placement, "event_id": event})

    def db(self) -> sqlite3.Connection:
        return sqlite3.connect(telemetry_path(self.tmp / "miniapp.db"))


def test_offer_price_comes_from_the_odds_store_for_the_recommended_market(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    publish(tmp_path, campaign())
    c, _ = env.user()
    env.pick(c, market="X2")
    offer = env.offer(c).json()["offer"]
    assert offer["odds"] is None and offer["oddsRequired"] is False       # optional: shown without a price
    env.prices.book[("fd:1", "X2")] = 1.85
    env.prices.book[("fd:1", "1")] = 9.99                                 # another market's price is never used
    offer = env.offer(c).json()["offer"]
    assert offer["odds"] == {"market": "X2", "odds": 1.85, "fetchedAt": "2027-01-15T08:00:00Z", "validFor": 600}
    assert env.prices.calls[-1] == ("fd:1", "X2")


def test_required_odds_hide_the_offer_and_refuse_the_click_without_a_price(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    publish(tmp_path, campaign(oddsMode="required"), campaign(2, priority=0, startsAt=NOW - 2 * DAY))
    c, csrf = env.user()
    env.pick(c)
    assert env.offer(c).json()["offer"]["id"] == 2                        # no price: the next campaign
    env.prices.book[("fd:1", "1")] = 2.1
    offer = env.offer(c).json()["offer"]
    assert offer["id"] == 1 and offer["oddsRequired"] is True and offer["odds"]["odds"] == 2.1
    del env.prices.book[("fd:1", "1")]
    r = post(c, "/api/ads/click", csrf, {"impressionToken": offer["impressionToken"]})
    assert r.status_code == 410                                           # the price is gone: no click-through


def test_a_market_without_a_price_or_no_market_means_no_price(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    publish(tmp_path, campaign(oddsMode="required"))
    c, _ = env.user()
    env.pick(c, market=None)
    assert env.offer(c).json() == {"offer": None}
    assert env.prices.calls == []                                         # nothing to look up without a market


def test_ai_recommended_bet_is_a_placement_once_the_user_saw_it(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    publish(tmp_path, campaign(placements=["ai_pick"]))
    c, _ = env.user()
    assert env.offer(c, "fd:9", "ai_pick").json() == {"offer": None}     # the AI screen was not shown yet
    assert c.get("/api/ai/analysis", params={"event_id": "fd:9"}).status_code == 200
    env.prices.book[("fd:9", "1X")] = 1.4
    offer = env.offer(c, "fd:9", "ai_pick").json()["offer"]
    assert offer["id"] == 1 and offer["odds"]["market"] == "1X"
    assert env.offer(c, "fd:9", "quick_pick").json() == {"offer": None}  # per placement
    env.ai.market = "NO_BET"
    assert c.get("/api/ai/analysis", params={"event_id": "fd:10"}).status_code == 200
    assert env.offer(c, "fd:10", "ai_pick").json() == {"offer": None}    # no recommended bet, no offer


def test_frequency_caps_count_other_matches(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    publish(tmp_path, campaign(caps={"perDay": 1, "total": 2}))
    c, csrf = env.user()
    for event in ("fd:1", "fd:2", "fd:3"):
        env.pick(c, event)
    token = env.offer(c, "fd:1").json()["offer"]["impressionToken"]
    assert post(c, "/api/ads/impression", csrf, {"impressionToken": token}).json()["counted"] is True
    assert env.offer(c, "fd:1").json()["offer"]["id"] == 1               # the same match stays visible
    assert env.offer(c, "fd:2").json() == {"offer": None}                # a second match today: capped
    env.clock.advance(DAY)
    for event in ("fd:2", "fd:3"):
        env.pick(c, event)
    token = env.offer(c, "fd:2").json()["offer"]["impressionToken"]
    assert post(c, "/api/ads/impression", csrf, {"impressionToken": token}).json()["counted"] is True
    env.clock.advance(DAY)
    env.pick(c, "fd:3")
    assert env.offer(c, "fd:3").json() == {"offer": None}                # two in all: the total cap


def test_an_offer_issued_before_the_cap_filled_is_reported_gone(tmp_path, monkeypatch):
    """Two tabs got offers for two matches before either impression landed (Codex ads merge r2 P2)."""
    env = Env(tmp_path, monkeypatch)
    publish(tmp_path, campaign(caps={"perDay": 1}))
    c, csrf = env.user()
    for event in ("fd:1", "fd:2"):
        env.pick(c, event)
    first = env.offer(c, "fd:1").json()["offer"]["impressionToken"]
    second = env.offer(c, "fd:2").json()["offer"]["impressionToken"]
    assert post(c, "/api/ads/impression", csrf, {"impressionToken": first}).json() == {
        "ok": True, "counted": True, "gone": False}
    assert post(c, "/api/ads/impression", csrf, {"impressionToken": first}).json()["gone"] is False  # a repeat
    assert post(c, "/api/ads/impression", csrf, {"impressionToken": second}).json() == {
        "ok": True, "counted": False, "gone": True}


def test_global_settings_block_countries_and_gate_age(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    publish(tmp_path, campaign(countries=["AZ", "TR"]), extra={"settings": {"blockedCountries": ["TR"]}})
    az, _ = env.user()
    tr, _ = env.user(OTHER, country="TR")
    env.pick(az)
    env.pick(tr)
    assert env.offer(az).json()["offer"]["id"] == 1
    assert env.offer(tr).json() == {"offer": None}
    publish(tmp_path, campaign(), version=11, extra={"settings": {"ageGate": True, "ageMin": 18}})
    env.clock.advance(31)
    assert env.offer(az).json() == {"offer": None, "ageGate": {"min": 18}}


def test_click_id_goes_into_the_link_and_the_postback_counts_once(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    url = "https://p.example.com/promo?sub={click_id}"
    variants = [{"key": "A", "weight": 100, "headline": "Бонус", "body": "", "extra": "", "cta": "Go", "url": url}]
    publish(tmp_path, campaign(variants=variants),
            extra={"partners": {"1": {"postbackSha256": postback_token_hash(PB_TOKEN)}}})
    c, csrf = env.user()
    env.pick(c)
    token = env.offer(c).json()["offer"]["impressionToken"]
    link = post(c, "/api/ads/click", csrf, {"impressionToken": token}).json()["url"]
    click_id = parse_qs(urlsplit(link).query)["sub"][0]
    assert link.startswith("https://p.example.com/promo?sub=") and "{" not in link
    # A replayed tap the same day opens the same link and adds no click row (Codex ads merge r2 P1).
    again = post(c, "/api/ads/click", csrf, {"impressionToken": token}).json()["url"]
    assert again == link
    with sqlite3.connect(telemetry_path(tmp_path / "miniapp.db")) as db:
        assert db.execute("SELECT COUNT(*) FROM ad_clicks").fetchone()[0] == 1
    edge = TestClient(env.app, base_url="http://127.0.0.1:8000", headers={"X-Edge-Auth": EDGE})
    q = {"partner": "1", "click_id": click_id, "txn": "dep-1", "event": "first_deposit"}
    auth = {"X-Postback-Token": PB_TOKEN}
    assert edge.get("/api/ads/postback", params=q, headers=auth).json() == {"status": "accepted"}
    assert edge.get("/api/ads/postback", params=q, headers=auth).json() == {"status": "duplicate"}
    assert edge.get("/api/ads/postback", params=q, headers={"X-Postback-Token": "x" * 40}).status_code == 403
    assert edge.get("/api/ads/postback", params={**q, "partner": "2"}, headers=auth).status_code == 403  # no token
    assert edge.get("/api/ads/postback", params={**q, "txn": "dep-2", "click_id": "z" * 24}, headers=auth).json() == {
        "status": "unknown_click"}
    assert edge.get("/api/ads/postback", params={**q, "txn": "bad txn"}, headers=auth).status_code == 400
    assert edge.get("/api/ads/postback", params=q).status_code == 400                       # no token at all
    # Never in a URL (proxy and access logs): refused before the token is even looked at.
    assert edge.get("/api/ads/postback", params={**q, "token": PB_TOKEN}).json()["error"] == "token_in_url"
    # A platform that cannot set headers POSTs the fields with the token in a form or JSON body.
    form = {**q, "txn": "dep-3", "token": PB_TOKEN}
    assert edge.post("/api/ads/postback", data=form).json() == {"status": "accepted"}
    assert edge.post("/api/ads/postback", json={**form, "txn": "dep-4"}).json() == {"status": "accepted"}
    assert edge.post("/api/ads/postback", json={**form, "txn": "dep-4"}).json() == {"status": "duplicate"}
    assert edge.post("/api/ads/postback", json={**form, "partner": 1}).status_code == 400   # strings only
    assert edge.post("/api/ads/postback", content=b"x", headers={"Content-Type": "text/plain"}).status_code == 415
    assert edge.post("/api/ads/postback", data={**form, "pad": "x" * 3000}).status_code == 413
    assert edge.post("/api/ads/postback", data={**form, "token": "y" * 40}).status_code == 403
    with env.db() as conn:
        rows = conn.execute("SELECT partner_id, txn_id, event, user_id, campaign_id, variant FROM ad_postbacks").fetchall()
    assert [r[1] for r in rows] == ["dep-1", "dep-3", "dep-4"] and rows[0] == (1, "dep-1", "first_deposit", USER, 1, "A")


def test_odds_in_texts_and_bad_macros_are_rejected():
    with pytest.raises(policy.PolicyError, match="odds_in_text"):
        policy.check_ad_text("Коэффициент 1.85 только сегодня", "headline", 80)
    for text in ("кф 2,5", "кэф 1.850", "odds 2·10", "معامل 1٫9"):
        with pytest.raises(policy.PolicyError, match="odds_in_text"):
            policy.check_ad_text(text, "body", 300)
    assert policy.check_ad_text("Бонус 100% до 500 AZN", "headline", 80)
    hosts = ["p.example.com"]
    assert policy.check_url("https://p.example.com/x?s={click_id}", hosts)
    for bad in ("https://p.example.com/x?s={user_id}", "https://{click_id}.p.example.com/", "https://p.example.com/{"):
        with pytest.raises(policy.PolicyError):
            policy.check_url(bad, hosts)
    with pytest.raises(policy.PolicyError):
        policy.render_url("https://p.example.com/x?s={click_id}", "short", hosts)


def test_snapshot_fields_are_optional_and_validated():
    body = {"schema": 1, "version": 1, "generatedAt": NOW, "campaigns": [campaign()], "audiences": {}}
    parsed = policy.parse(policy.canonical(policy.sign(body, KEY)), KEY.public_key())
    c = parsed.campaigns[0]
    assert (c.odds_mode, c.cap_per_day, c.cap_total, parsed.postback_hashes) == ("optional", 0, 0, {})
    for bad in ({"campaigns": [campaign(oddsMode="always")]}, {"campaigns": [campaign(caps={"perDay": -1})]},
                {"settings": {"blockedCountries": ["XX"]}}, {"partners": {"1": {"postbackSha256": "nope"}}}):
        with pytest.raises(policy.PolicyError):
            policy.parse(policy.canonical(policy.sign({**body, **bad}, KEY)), KEY.public_key())


def test_caps_without_counts_deny():
    body = {"schema": 1, "version": 1, "generatedAt": NOW, "campaigns": [campaign(caps={"perDay": 1})]}
    snap = policy.parse(policy.canonical(policy.sign({**body, "audiences": {}}, KEY)), KEY.public_key())
    v = engine.Viewer(USER, "ru", "AZ", 0, lambda n, p: False)
    assert engine.choose(snap, v, "quick_pick", NOW) is None
    broken = engine.Viewer(USER, "ru", "AZ", 0, lambda n, p: False, lambda cid, ev: 1 / 0)
    assert engine.choose(snap, broken, "quick_pick", NOW) is None
    ok = engine.Viewer(USER, "ru", "AZ", 0, lambda n, p: False, lambda cid, ev: (0, 5))
    assert engine.choose(snap, ok, "quick_pick", NOW).campaign.id == 1


def test_events_carry_the_viewer_class_and_days_roll_up(tmp_path):
    t = Telemetry(tmp_path / "t.db")
    t.init_schema()
    t.exposure("impression", 1, 7, "A", "quick_pick", "fd:1", "AZ", NOW, "new")
    t.exposure("impression", 2, 7, "A", "quick_pick", "fd:1", None, NOW, "other")
    t.click(1, 7, "A", "quick_pick", "fd:1", "AZ", NOW + 5, "new", "c" * 24, 3)
    assert {r["key"]: r["events"] for r in t.ad_totals(day_of(NOW), "segment") if r["kind"] == "impression"} == {
        "new": 1, "other": 1}
    assert t.rollup(NOW) == 0                                             # today is not complete
    assert t.rollup(NOW + DAY) == 1
    assert t.rollup(NOW + DAY) == 0                                       # once per day
    t.purge(NOW + 200 * DAY)                                              # raw events are gone after 90 days
    assert t.ad_totals(day_of(NOW), "variant") == []
    daily = {(r["key"], r["kind"]): r["events"] for r in t.daily_totals(day_of(NOW), "variant")}
    assert daily == {("A", "impression"): 2, ("A", "click"): 1}
    t.purge(NOW + 401 * DAY)
    assert t.daily_totals(day_of(NOW), "variant") == []


def test_telemetry_migrates_an_old_file_additively(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE ad_events (id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, "
                           "day TEXT NOT NULL, user_id INTEGER NOT NULL, campaign_id INTEGER NOT NULL, variant TEXT NOT "
                           "NULL, placement TEXT NOT NULL, event_key TEXT NOT NULL, country TEXT, kind TEXT NOT NULL);"
                           "INSERT INTO ad_events (at, day, user_id, campaign_id, variant, placement, event_key, kind) "
                           "VALUES (1, '2027-01-01', 1, 1, 'A', 'quick_pick', 'fd:1', 'impression');")
    t = Telemetry(path)
    t.init_schema()
    t.init_schema()                                                       # twice: the column is already there
    assert t.ad_totals("2027-01-01", "segment")[0]["key"] == ""


def test_partner_api_hook_uses_the_same_idempotent_path(tmp_path):
    t = Telemetry(tmp_path / "t.db")
    t.init_schema()
    t.click(USER, 7, "B", "quick_pick", "fd:1", "AZ", NOW, "active", "k" * 24, 3)

    class Source:
        partner_id = 3

        def fetch(self, since, until):
            return [PartnerConversion("r-1", "k" * 24, "registration"), PartnerConversion("r-1", "k" * 24),
                    PartnerConversion("bad id", "k" * 24), PartnerConversion("r-2", "q" * 24)]

    class Down:
        partner_id = 3

        def fetch(self, since, until):
            raise TimeoutError

    assert ingest(Source(), t, NOW - DAY, NOW, NOW + 60) == {"accepted": 1, "duplicate": 1, "invalid": 1,
                                                              "unknown_click": 1}
    assert ingest(Down(), t, NOW - DAY, NOW, NOW + 60) == {"failed": 1}
    assert t.postback_totals(day_of(NOW)) == [{"campaignId": 7, "variant": "B", "event": "registration", "count": 1,
                                               "users": 1}]
    assert t.record_conversion(3, "late", "k" * 24, "conversion", NOW + 31 * DAY) == "unknown_click"  # too old


def test_concurrent_impressions_cannot_both_pass_a_cap(tmp_path):
    t = Telemetry(tmp_path / "t.db")
    t.init_schema()
    assert t.exposure("impression", 1, 7, "A", "quick_pick", "fd:1", "AZ", NOW, None, (1, 0)) is True
    # An offer for fd:2 was issued before fd:1's impression landed: the insert itself enforces the cap.
    assert t.exposure("impression", 1, 7, "A", "quick_pick", "fd:2", "AZ", NOW, None, (1, 0)) is False
    assert t.exposure("impression", 1, 7, "A", "quick_pick", "fd:3", "AZ", NOW + DAY, None, (0, 1)) is False
    assert t.exposure("impression", 1, 7, "A", "quick_pick", "fd:2", "AZ", NOW + DAY, None, (1, 2)) is True


def test_the_total_cap_counts_the_same_match_on_earlier_days(tmp_path):
    t = Telemetry(tmp_path / "t.db")
    t.init_schema()
    assert t.exposure("impression", 1, 7, "A", "quick_pick", "fd:1", "AZ", NOW, None, (0, 1)) is True
    assert t.impression_counts(1, 7, "fd:1", NOW) == (0, 0)          # the card on screen today stays visible
    assert t.impression_counts(1, 7, "fd:1", NOW + DAY) == (0, 1)    # the same match tomorrow: counted
    assert t.exposure("impression", 1, 7, "A", "quick_pick", "fd:1", "AZ", NOW + DAY, None, (0, 1)) is False


def test_an_older_snapshot_with_a_price_in_a_text_drops_only_that_campaign():
    priced = campaign(2, variants=[{"key": "A", "weight": 100, "headline": "Ставка 2.5 AZN", "body": "", "extra": "",
                                    "cta": "Go", "url": "https://p.example.com/x"}])
    body = {"schema": 1, "version": 1, "generatedAt": NOW, "campaigns": [campaign(), priced], "audiences": {}}
    parsed = policy.parse(policy.canonical(policy.sign(body, KEY)), KEY.public_key())
    assert [c.id for c in parsed.campaigns] == [1]


def test_offer_prices_come_from_the_real_odds_store(clock: FakeClock):
    import asyncio

    from miniapp.odds.store import MAX_AGE_S, OddsStore
    from miniapp.tests.test_odds import _event, line
    from miniapp.tests.test_odds import ingest as push

    store = OddsStore(clock)
    push(store, clock, line())
    event = _event("TIMED")

    async def event_of(event_id):
        return (event if event_id == event.id else None), None

    lookup = odds_source.make_odds_lookup(event_of, store)
    found = asyncio.run(lookup(event.id, "2"))
    assert found["market"] == "2" and found["odds"] == 4.5 and 0 < found["validFor"] <= MAX_AGE_S
    assert found["fetchedAt"].endswith("Z")
    assert asyncio.run(lookup(event.id, "H1-0.5")) is None              # a market the store does not price
    assert asyncio.run(lookup("fd:404", "2")) is None                   # an unknown match
    clock.advance(MAX_AGE_S)                                            # stale prices are never shown
    assert asyncio.run(lookup(event.id, "2")) is None


def test_a_token_rechecks_the_price_of_the_market_it_was_issued_for(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    publish(tmp_path, campaign(oddsMode="required"))
    c, csrf = env.user()
    env.pick(c, market="X2")
    env.prices.book[("fd:1", "X2")] = 1.6
    old = env.offer(c).json()["offer"]
    assert old["odds"]["market"] == "X2"
    env.pick(c, market="1")                               # a later recommendation of the same match
    env.prices.book[("fd:1", "1")] = 2.4
    del env.prices.book[("fd:1", "X2")]                   # the price the old card showed is gone
    assert post(c, "/api/ads/impression", csrf, {"impressionToken": old["impressionToken"]}).json()["counted"] is False
    assert post(c, "/api/ads/click", csrf, {"impressionToken": old["impressionToken"]}).status_code == 410
    new = env.offer(c).json()["offer"]
    assert new["odds"]["market"] == "1"
    assert post(c, "/api/ads/click", csrf, {"impressionToken": new["impressionToken"]}).status_code == 200


def test_look_alike_digits_and_a_macro_in_the_fragment_are_refused():
    with pytest.raises(policy.PolicyError, match="odds_in_text"):
        policy.check_ad_text("кэф １．８５", "headline", 80)
    with pytest.raises(policy.PolicyError):
        policy.check_url("https://p.example.com/x#id={click_id}", ["p.example.com"])


def test_disclosure_fields_cannot_carry_a_price_and_links_fit_once_expanded():
    with pytest.raises(policy.PolicyError, match="odds_in_text"):
        policy.check_disclosure({"advertiser": "Odds 2.10 LLC"})
    for bad in ({"ageLabel": "2.10"}, {"adId": "2.10"}, {"adId": "ERID 1"}):
        with pytest.raises(policy.PolicyError, match="invalid_disclosure"):
            policy.check_disclosure(bad)
    assert policy.check_disclosure({"advertiser": "Partner LLC", "ageLabel": "18+", "adId": "2SDnjc7x-1"})["adId"]
    hosts = ["p.example.com"]
    base = "https://p.example.com/?s={click_id}&p="
    with pytest.raises(policy.PolicyError):                    # 500 as typed, over 500 once the id is in place
        policy.check_url(base + "x" * (policy.MAX_URL - len(base)), hosts)
    with pytest.raises(policy.PolicyError):
        policy.check_url("https://p.example.com/?a={click_id}&b={click_id}", hosts)
    ok = policy.check_url(base + "x" * (policy.MAX_URL - len(base) - 60), hosts)
    assert len(policy.render_url(ok, "k" * 64, hosts)) <= policy.MAX_URL


def test_click_ids_are_claimed_once_per_offer_and_day(tmp_path):
    t = Telemetry(tmp_path / "t.db")
    t.init_schema()
    first = t.claim_click_id(1, 7, "A", 3, "quick_pick", "fd:1", NOW, "a" * 24)
    assert first == "a" * 24
    assert t.claim_click_id(1, 7, "A", 3, "quick_pick", "fd:1", NOW + 5, "b" * 24) == first  # a replay
    assert t.claim_click_id(1, 7, "A", 3, "quick_pick", "fd:2", NOW, "c" * 24) == "c" * 24    # another match
    assert t.claim_click_id(1, 7, "A", 3, "quick_pick", "fd:1", NOW + DAY, "d" * 24) == "d" * 24  # next UTC day
    with sqlite3.connect(tmp_path / "t.db") as db:
        assert db.execute("SELECT COUNT(*) FROM ad_clicks").fetchone()[0] == 3
