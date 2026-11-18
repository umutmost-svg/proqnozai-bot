"""Ads module, stage 3: Mini App API behind MINIAPP_ADS_ENABLED — offer, impression, click, age, telemetry."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from miniapp.ads import policy
from miniapp.ads.service import PolicyReader
from miniapp.ads.telemetry import telemetry_path
from miniapp.app import create_app
from miniapp.settings import Settings, SettingsError
from miniapp.tests.conftest import NOW, ORIGIN, FakeClock, login
from miniapp.tests.signing import TEST_BOT_TOKEN, make_init_data

KEY = Ed25519PrivateKey.generate()
PUBLIC_HEX = policy.public_key_hex(KEY)
EDGE = "e" * 40
DAY = 86400
USER, OTHER = 123456789, 555


def settings(tmp_path: Path, **over) -> Settings:
    base = dict(bot_token=TEST_BOT_TOKEN, db_path=tmp_path / "miniapp.db", trusted_origins=frozenset({ORIGIN}),
                environment="development", session_ttl_s=6 * 86400, ads_enabled=True, ads_policy_public_key=PUBLIC_HEX,
                edge_secret=EDGE)
    base.update(over)
    return Settings(**base)


def campaign(cid: int = 1, **over) -> dict:
    base = {"id": cid, "tier": "general", "priority": 0, "startsAt": NOW - DAY, "endsAt": NOW + 10 * DAY,
            "langIn": [], "placements": ["quick_pick"], "countries": ["AZ"], "allowUnknownCountry": False,
            "ageMin": 0, "partnerId": 1, "hosts": ["p.example.com"],
            "disclosure": {"advertiser": "Partner LLC", "adId": "", "ageLabel": ""}, "condition": None,
            "experiment": {"salt": "salt-salt-1", "controlPct": 0},
            "variants": [{"key": "A", "weight": 100, "headline": "Бонус", "body": "Текст", "extra": "", "cta": "Забрать",
                          "url": "https://p.example.com/promo"}]}
    base.update(over)
    return base


def publish(tmp_path: Path, *campaigns: dict, version: int = 10, audiences: dict | None = None,
            key: Ed25519PrivateKey = KEY) -> None:
    body = {"schema": 1, "version": version, "generatedAt": NOW, "campaigns": list(campaigns),
            "audiences": audiences or {}}
    policy.write_atomic(tmp_path / "ads-policy.json", policy.canonical(policy.sign(body, key)))


class Env:
    def __init__(self, tmp_path: Path, **over) -> None:
        self.tmp = tmp_path
        self.clock = FakeClock()
        self.app = create_app(settings(tmp_path, **over), clock=self.clock)

    def user(self, uid: int = USER, country: str | None = "AZ", lang: str = "ru") -> tuple[TestClient, str]:
        headers = {"X-Edge-Auth": EDGE}
        if country:
            headers["CF-IPCountry"] = country
        c = TestClient(self.app, base_url="http://127.0.0.1:8000", headers=headers)
        r = login(c, make_init_data({"id": uid, "first_name": "U", "language_code": lang}, auth_date=int(self.clock.now)))
        assert r.status_code == 200, r.text
        c.uid = uid  # type: ignore[attr-defined]
        return c, r.json()["session"]["csrf_token"]

    def offer(self, c: TestClient, event: str = "fd:1", placement: str = "quick_pick", picked: bool = True):
        if picked and placement in policy.PLACEMENTS and getattr(self.app.state, "ads_picks", None) is not None:
            self.app.state.ads_picks.add(c.uid, [(event, "1")], self.clock.now, placement)  # the server recommended it
        return c.get("/api/ads/offer", params={"placement": placement, "event_id": event})

    def rows(self, kind: str) -> list[tuple]:
        with sqlite3.connect(telemetry_path(self.tmp / "miniapp.db")) as conn:
            return conn.execute("SELECT user_id, campaign_id, variant, event_key, country FROM ad_events WHERE kind = ?",
                                (kind,)).fetchall()


def post(c: TestClient, path: str, csrf: str, body: dict):
    return c.post(path, json=body, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})


def test_flag_off_means_no_routes_and_no_files(tmp_path):
    env = Env(tmp_path, ads_enabled=False, ads_policy_public_key=None)
    publish(tmp_path, campaign())
    c, _ = env.user()
    assert env.offer(c).status_code == 404
    assert not telemetry_path(tmp_path / "miniapp.db").exists()
    assert not (tmp_path / "ads-policy.applied").exists()


def test_flag_on_requires_the_public_key(tmp_path):
    with pytest.raises(SettingsError):
        settings(tmp_path, ads_policy_public_key=None)
    with pytest.raises(SettingsError):
        settings(tmp_path, ads_policy_public_key="zz")


def test_offer_texts_without_link_and_geo_from_the_trusted_edge(tmp_path):
    env = Env(tmp_path)
    c, _ = env.user()
    assert env.offer(c).json() == {"offer": None}  # no snapshot yet
    publish(tmp_path, campaign())
    env.clock.advance(31)
    r = env.offer(c)
    offer = r.json()["offer"]
    assert r.headers["cache-control"].startswith("no-store")
    assert offer["headline"] == "Бонус" and offer["cta"] == "Забрать" and offer["disclosure"] == {"advertiser": "Partner LLC"}
    assert "url" not in offer and "p.example.com" not in r.text
    ru, _ = env.user(OTHER, country="RU")
    assert env.offer(ru).json() == {"offer": None}
    unknown, _ = env.user(OTHER, country="XX")
    assert env.offer(unknown).json() == {"offer": None}
    assert env.offer(c, placement="banner").status_code == 400
    assert env.offer(c, event="x" * 41).status_code == 400
    assert (tmp_path / "ads-policy.applied").read_text() == "10"


def test_country_header_is_ignored_without_the_edge_secret(tmp_path):
    env = Env(tmp_path, edge_secret=None)
    publish(tmp_path, campaign())
    c, _ = env.user(country="AZ")
    assert env.offer(c).json() == {"offer": None}  # header not trusted: unknown country, not allowed
    publish(tmp_path, campaign(allowUnknownCountry=True), version=11)
    env.clock.advance(31)
    assert env.offer(c).json()["offer"]["id"] == 1


def test_impression_once_per_day_and_tokens_bound_to_the_user(tmp_path):
    env = Env(tmp_path)
    publish(tmp_path, campaign())
    c, csrf = env.user()
    token = env.offer(c).json()["offer"]["impressionToken"]
    assert post(c, "/api/ads/impression", csrf, {"impressionToken": token}).json() == {"ok": True, "counted": True,
                                                                                        "gone": False}
    assert post(c, "/api/ads/impression", csrf, {"impressionToken": token}).json()["counted"] is False
    assert env.rows("impression") == [(USER, 1, "A", "fd:1", "AZ")]
    other, other_csrf = env.user(OTHER)
    assert post(other, "/api/ads/impression", other_csrf, {"impressionToken": token}).status_code == 403
    assert post(c, "/api/ads/impression", csrf, {"impressionToken": token[:-2] + "xx"}).status_code == 403
    assert c.post("/api/ads/impression", json={"impressionToken": token}, headers={"Origin": ORIGIN}).status_code == 403
    assert post(c, "/api/ads/impression", csrf, {"impressionToken": token, "x": 1}).status_code == 400
    env.clock.advance(2 * DAY)
    assert post(c, "/api/ads/impression", csrf, {"impressionToken": token}).json()["error"] == "token_expired"


def test_click_returns_the_current_link_and_is_deduplicated(tmp_path):
    env = Env(tmp_path)
    publish(tmp_path, campaign())
    c, csrf = env.user()
    token = env.offer(c).json()["offer"]["impressionToken"]
    assert post(c, "/api/ads/click", csrf, {"impressionToken": token}).json() == {"url": "https://p.example.com/promo"}
    env.clock.advance(5)
    assert post(c, "/api/ads/click", csrf, {"impressionToken": token}).status_code == 200
    assert len(env.rows("click")) == 1
    env.clock.advance(11)  # a replay later the same UTC day opens the link but is not counted (CRM audit B3)
    assert post(c, "/api/ads/click", csrf, {"impressionToken": token}).json() == {"url": "https://p.example.com/promo"}
    assert len(env.rows("click")) == 1
    env.clock.advance(DAY - int(env.clock()) % DAY)  # the next UTC day: counted again
    post(c, "/api/ads/click", csrf, {"impressionToken": token})
    assert len(env.rows("click")) == 2
    # The link changes for everyone with the next snapshot; a removed campaign is gone at once.
    edited = campaign()
    edited["variants"][0]["url"] = "https://p.example.com/new"
    publish(tmp_path, edited, version=11)
    env.clock.advance(31)
    assert post(c, "/api/ads/click", csrf, {"impressionToken": token}).json() == {"url": "https://p.example.com/new"}
    publish(tmp_path, version=12)
    env.clock.advance(31)
    assert post(c, "/api/ads/click", csrf, {"impressionToken": token}).status_code == 410


def test_control_group_sees_nothing_but_is_counted(tmp_path):
    env = Env(tmp_path)
    publish(tmp_path, campaign(experiment={"salt": "salt-salt-1", "controlPct": 50}))
    shown = control = 0
    for uid in range(1000, 1012):
        c, _ = env.user(uid)
        if env.offer(c).json()["offer"] is None:
            control += 1
        else:
            shown += 1
    assert shown and control and len(env.rows("control")) == control


def test_age_gate_asks_once_then_shows(tmp_path):
    env = Env(tmp_path)
    publish(tmp_path, campaign(ageMin=18))
    c, csrf = env.user()
    assert env.offer(c).json() == {"offer": None, "ageGate": {"min": 18}}
    assert post(c, "/api/ads/age", csrf, {"min": 18}).json() == {"ok": True}
    assert env.offer(c).json()["offer"]["id"] == 1
    assert post(c, "/api/ads/age", csrf, {}).status_code == 400
    assert post(c, "/api/ads/age", csrf, {"min": 99}).status_code == 400
    publish(tmp_path, campaign(ageMin=21), version=11)    # a stricter partner asks again, for its own age
    env.clock.advance(31)
    assert env.offer(c).json() == {"offer": None, "ageGate": {"min": 21}}
    post(c, "/api/ads/age", csrf, {"min": 21})
    assert env.offer(c).json()["offer"]["id"] == 1


def test_activity_feeds_segments(tmp_path):
    env = Env(tmp_path)
    publish(tmp_path, campaign(condition={"segment": {"name": "active", "params": {"days": 7, "min_visits": 2}}}))
    c, _ = env.user()
    assert env.offer(c).json() == {"offer": None}  # one visit day so far
    env.clock.advance(DAY)
    assert c.get("/api/me").status_code == 200       # a visit on the next day
    env.clock.advance(31)
    assert env.offer(c).json()["offer"]["id"] == 1
    new_only = campaign(condition={"segment": {"name": "new", "params": {"days": 1}}})
    publish(tmp_path, new_only, version=11)
    env.clock.advance(DAY + 31)
    assert env.offer(c).json() == {"offer": None}    # registered more than a day ago


def test_reader_keeps_the_last_good_policy_and_never_goes_back(tmp_path):
    clock = FakeClock()
    path, applied = tmp_path / "ads-policy.json", tmp_path / "ads-policy.applied"
    reader = PolicyReader(path, applied, KEY.public_key(), clock)
    assert reader.current() is None
    publish(tmp_path, campaign(), version=20)
    clock.advance(31)
    assert reader.current().version == 20 and applied.read_text() == "20"
    publish(tmp_path, campaign(), version=19)               # older: rejected
    clock.advance(31)
    assert reader.current().version == 20
    publish(tmp_path, campaign(), version=30, key=Ed25519PrivateKey.generate())   # foreign signature
    clock.advance(31)
    assert reader.current().version == 20
    path.write_bytes(b"{broken")
    clock.advance(31)
    assert reader.current().version == 20
    fresh = PolicyReader(path, applied, KEY.public_key(), clock)  # restart: the applied mark still holds
    publish(tmp_path, campaign(), version=15)
    assert fresh.current() is None
    publish(tmp_path, campaign(), version=21)
    clock.advance(31)
    assert fresh.current().version == 21


def test_telemetry_failure_never_breaks_the_app(tmp_path, monkeypatch):
    env = Env(tmp_path)
    from miniapp.ads.telemetry import Telemetry

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(Telemetry, "record_activity", boom)
    c, _ = env.user()
    assert c.get("/api/me").status_code == 200


def test_an_implausible_applied_mark_is_ignored_by_the_reader(tmp_path):
    clock = FakeClock()
    applied = tmp_path / "ads-policy.applied"
    applied.write_text(str(2**62))
    publish(tmp_path, campaign(), version=int(clock.now * 1000))
    reader = PolicyReader(tmp_path / "ads-policy.json", applied, KEY.public_key(), clock)
    assert reader.current() is not None and applied.read_text() == str(int(clock.now * 1000))


def test_the_app_starts_without_the_ads_package_when_the_flag_is_off(tmp_path):
    # The stand image does not ship miniapp/ads yet: with the flag off nothing may import it.
    import subprocess
    import sys

    code = (
        "import builtins\n"
        "real = builtins.__import__\n"
        "def guard(name, *a, **k):\n"
        "    if name == 'miniapp.ads' or name.startswith('miniapp.ads.'):\n"
        "        raise ImportError('ads package not shipped')\n"
        "    return real(name, *a, **k)\n"
        "builtins.__import__ = guard\n"
        "from pathlib import Path\n"
        "from miniapp.app import create_app\n"
        "from miniapp.settings import Settings\n"
        "from miniapp.tests.signing import TEST_BOT_TOKEN\n"
        f"create_app(Settings(bot_token=TEST_BOT_TOKEN, db_path=Path({str(tmp_path / 'm.db')!r}), "
        "trusted_origins=frozenset({'http://127.0.0.1:5173'}), environment='development'))\n"
        "print('ok')\n"
    )
    root = Path(__file__).resolve().parents[2]
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stderr[-2000:]


def test_a_successful_quick_pick_counts_as_activity():
    from types import SimpleNamespace

    from fastapi import FastAPI

    from miniapp.quick.routes import register_quick_routes

    class Answer:
        def __init__(self, mode, status):
            self.body = {"mode": mode, "status": status}

        def to_json(self):
            return self.body

    answers = [Answer("single", "ok"), Answer("coupon", "ok"), Answer("single", "no_bet")]

    class Service:
        def first_cost(self, start, end):
            return 0  # the per-user share (prod line): nothing cold to admit

        async def pick(self, *a, **k):
            return answers.pop(0)

    async def session(_request):
        return "token", SimpleNamespace(user_id=USER)

    seen = []
    app = FastAPI()
    register_quick_routes(app, Service(), session, on_pick=lambda uid, field, events: seen.append((uid, field)))
    c = TestClient(app)
    params = {"mode": "single", "from": "2026-11-08T00:00:00Z", "to": "2026-11-09T00:00:00Z"}
    for _ in range(3):
        assert c.get("/api/quick/pick", params=params).status_code == 200
    assert seen == [(USER, "quick_single_ok"), (USER, "quick_coupon_ok")]


@pytest.mark.parametrize("change", [
    {"countries": ["TR"]},                                   # the user's country was removed
    {"ageMin": 18},                                          # an age minimum was added
    {"condition": {"user_in": [OTHER]}},                     # the audience was narrowed
    {"experiment": {"salt": "another-salt-2", "controlPct": 100 // 2}, "variants": [
        {"key": "B", "weight": 100, "headline": "B", "body": "", "extra": "", "cta": "Go",
         "url": "https://p.example.com/b"}]},                # the variant no longer exists
])
def test_an_old_token_follows_the_current_targeting(tmp_path, change):
    env = Env(tmp_path)
    publish(tmp_path, campaign())
    c, csrf = env.user()
    token = env.offer(c).json()["offer"]["impressionToken"]
    publish(tmp_path, campaign(**change), version=11)
    env.clock.advance(31)
    assert post(c, "/api/ads/click", csrf, {"impressionToken": token}).status_code == 410
    assert post(c, "/api/ads/impression", csrf, {"impressionToken": token}).json()["counted"] is False


def test_telemetry_failures_degrade_without_breaking_answers(tmp_path, monkeypatch):
    from miniapp.ads.telemetry import Telemetry

    env = Env(tmp_path)
    publish(tmp_path, campaign(), campaign(2, priority=5, ageMin=18))
    c, csrf = env.user()
    token = env.offer(c).json()["offer"]["impressionToken"]  # campaign 1 (2 needs an age statement)

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(Telemetry, "click", boom)
    monkeypatch.setattr(Telemetry, "exposure", boom)
    monkeypatch.setattr(Telemetry, "confirm_age", boom)
    assert post(c, "/api/ads/click", csrf, {"impressionToken": token}).json() == {"url": "https://p.example.com/promo"}
    assert post(c, "/api/ads/impression", csrf, {"impressionToken": token}).json()["counted"] is False
    assert post(c, "/api/ads/age", csrf, {"min": 18}).status_code == 503
    monkeypatch.setattr(Telemetry, "confirmed_age", boom)
    answer = env.offer(c).json()
    assert answer["offer"]["id"] == 1 and "ageGate" not in answer  # unreadable age: gated offers denied, no prompt


def test_unusable_telemetry_keeps_the_app_up_without_ads(tmp_path):
    from miniapp.ads.telemetry import telemetry_path as tpath

    tpath(tmp_path / "miniapp.db").mkdir(parents=True)  # a directory where the database file should be
    env = Env(tmp_path)
    c, _ = env.user()
    assert c.get("/api/me").status_code == 200
    assert env.offer(c).status_code == 404


def test_retention_runs_without_a_restart(tmp_path):
    env = Env(tmp_path)
    c, _ = env.user()
    old = int(env.clock.now) - 91 * DAY
    with sqlite3.connect(telemetry_path(tmp_path / "miniapp.db")) as conn:
        conn.execute("INSERT INTO ad_events (at, day, user_id, campaign_id, variant, placement, event_key, country, kind) "
                     "VALUES (?, '2000-01-01', 1, 1, 'A', 'quick_pick', 'fd:1', NULL, 'impression')", (old,))
    env.clock.advance(DAY)
    assert c.get("/api/me").status_code == 200
    assert env.rows("impression") == []


def test_inactive_needs_an_earlier_visit(tmp_path):
    env = Env(tmp_path)
    publish(tmp_path, campaign(condition={"segment": {"name": "inactive", "params": {"days": 3}}}))
    c, _ = env.user()
    assert env.offer(c).json() == {"offer": None}   # first visit ever: not "inactive"
    env.clock.advance(4 * DAY)
    assert c.get("/api/me").status_code == 200       # back after 4 days
    env.clock.advance(31)
    assert env.offer(c).json()["offer"]["id"] == 1


def test_offers_only_for_matches_the_server_recommended(tmp_path):
    env = Env(tmp_path)
    publish(tmp_path, campaign(placements=["quick_pick", "ai_pick"]))
    c, _ = env.user()
    assert env.offer(c, event="fd:777", picked=False).json() == {"offer": None}   # invented id
    assert env.offer(c, event="fd:777").json()["offer"]["id"] == 1                  # after a real pick of it
    assert env.offer(c, event="fd:777", placement="ai_pick", picked=False).json() == {"offer": None}  # other placement
    other, _ = env.user(OTHER)
    assert env.offer(other, event="fd:777", picked=False).json() == {"offer": None}  # another user's pick
    env.clock.advance(6 * 3600 + 1)
    assert env.offer(c, event="fd:777", picked=False).json() == {"offer": None}   # the pick expired


def test_recommended_matches_are_shared_between_workers(tmp_path):
    first = Env(tmp_path)
    publish(tmp_path, campaign())
    c, _ = first.user()
    first.app.state.ads_picks.add(USER, [("fd:42", "1")], first.clock.now)   # the pick was served by worker 1
    second = Env(tmp_path)                                             # worker 2, same data directory
    second.clock.now = first.clock.now
    c2 = TestClient(second.app, base_url="http://127.0.0.1:8000", headers={"X-Edge-Auth": EDGE, "CF-IPCountry": "AZ"})
    for k, v in c.cookies.items():
        c2.cookies.set(k, v)
    c2.uid = USER
    assert second.offer(c2, event="fd:42", picked=False).json()["offer"]["id"] == 1


def test_a_click_follows_the_newest_publication_at_once(tmp_path):
    env = Env(tmp_path)
    publish(tmp_path, campaign())
    c, csrf = env.user()
    token = env.offer(c).json()["offer"]["impressionToken"]
    replaced = campaign()
    replaced["variants"][0]["url"] = "https://p.example.com/new"
    publish(tmp_path, replaced, version=11)
    env.clock.advance(1)  # well inside the 30 s reload interval of offers
    assert post(c, "/api/ads/click", csrf, {"impressionToken": token}).json() == {"url": "https://p.example.com/new"}
    publish(tmp_path, version=12)  # paused: an empty snapshot
    env.clock.advance(1)
    assert post(c, "/api/ads/click", csrf, {"impressionToken": token}).status_code == 410


def test_a_campaign_paused_after_the_offer_hides_at_its_impression(tmp_path):
    """The impression reads the newest snapshot on disk: a pause published after the offer was issued hides the card
    at once (gone), not after the 30 s reload."""
    env = Env(tmp_path)
    publish(tmp_path, campaign())
    c, csrf = env.user()
    token = env.offer(c).json()["offer"]["impressionToken"]
    publish(tmp_path, version=11)                          # emergency stop: an empty snapshot
    assert post(c, "/api/ads/impression", csrf, {"impressionToken": token}).json() == {
        "ok": True, "counted": False, "gone": True}
