# ruff: noqa: F811 — pytest fixtures imported from other test modules are used as parameters
"""Ads module, stage 7: admin side of display settings, odds mode, frequency caps, postback tokens and the
extended analytics (segments, partner conversions, 13-month aggregates)."""
from __future__ import annotations

import hashlib
import json

from miniapp.ads import policy
from miniapp.ads.telemetry import DAY, Telemetry
from miniapp.tests.test_admin_web import env, login, post  # noqa: F401 — env is a fixture
from miniapp.tests.test_ads_admin import U1, U2, ads_env, campaign_body, pub, publish  # noqa: F401


def active_campaign(client, csrf, pid, clock, **extra) -> int:
    cid = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()), **extra)).json()["id"]
    assert post(client, f"/api/admin/ads/campaigns/{cid}/status", csrf, {"action": "activate"}).status_code == 200
    return cid


def test_odds_mode_and_caps_are_saved_and_published(ads_env):
    config, clock, client, _, secrets_, csrf, pid = ads_env
    cid = active_campaign(client, csrf, pid, clock, oddsMode="required", caps={"perDay": 2, "total": 5})
    item = client.get(f"/api/admin/ads/campaigns/{cid}").json()
    assert item["oddsMode"] == "required" and item["caps"] == {"perDay": 2, "total": 5}
    assert publish(client, csrf, secrets_, clock).status_code == 200
    c = policy.parse(config.ads_policy_file.read_bytes(), pub(config)).campaigns[0]
    assert (c.odds_mode, c.cap_per_day, c.cap_total) == ("required", 2, 5)


def test_an_advertiser_can_never_set_odds(ads_env):
    _, clock, client, _, _, csrf, pid = ads_env
    body = campaign_body(pid, int(clock()))
    assert post(client, "/api/admin/ads/campaigns", csrf, {**body, "odds": 1.9}).json()["error"] == "odds_not_editable"
    body["variants"][0]["odds"] = 1.9
    assert post(client, "/api/admin/ads/campaigns", csrf, body).json()["error"] == "odds_not_editable"
    body = campaign_body(pid, int(clock()))
    body["variants"][0]["headline"] = "Кэф 2.15 на фаворита"
    assert post(client, "/api/admin/ads/campaigns", csrf, body).json()["error"] == "odds_in_text"
    for bad in ({"oddsMode": "sometimes"}, {"caps": {"perDay": 101}}, {"caps": {"weekly": 1}}):
        assert post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()), **bad)).status_code == 400


def test_display_settings_are_owner_only_and_published(ads_env):
    config, clock, client, accounts, secrets_, csrf, pid = ads_env
    assert client.get("/api/admin/ads/settings").json()["ageGate"] is False
    bad = post(client, "/api/admin/ads/settings", csrf, {"blockedCountries": ["XX"]})
    assert bad.status_code == 400
    ok = post(client, "/api/admin/ads/settings", csrf, {"blockedCountries": ["TR"], "ageGate": True, "ageMin": 21})
    assert ok.status_code == 200, ok.text
    active_campaign(client, csrf, pid, clock)
    assert publish(client, csrf, secrets_, clock).status_code == 200
    snap = policy.parse(config.ads_policy_file.read_bytes(), pub(config))
    assert snap.settings.blocked_countries == frozenset({"TR"}) and snap.settings.age_gate is True
    c = snap.campaigns[0]
    assert "TR" not in c.countries and c.age_min == 21
    assert accounts.events(action="ads.settings_save")[0]["after"]["ageMin"] == 21
    client.cookies.clear()
    csrf_admin = login(client, clock, secrets_, "admin@x.test")
    assert post(client, "/api/admin/ads/settings", csrf_admin, {"ageGate": False}).status_code == 403


def test_postback_token_is_shown_once_and_only_its_hash_is_published(ads_env):
    config, clock, client, accounts, secrets_, csrf, pid = ads_env
    r = post(client, f"/api/admin/ads/partners/{pid}/postback-token", csrf, {})
    token = r.json()["token"]
    assert r.status_code == 200 and len(token) >= 40
    partners = client.get("/api/admin/ads/partners").json()["items"]
    assert partners[0]["postback"] is True and token not in json.dumps(partners)
    assert token not in json.dumps(accounts.events(action="ads.postback_token"))
    assert client.get("/api/admin/ads/publication").json()["unpublishedChanges"] is True
    assert publish(client, csrf, secrets_, clock).status_code == 200
    raw = config.ads_policy_file.read_bytes()
    assert token.encode() not in raw
    assert policy.parse(raw, pub(config)).postback_hashes == {pid: hashlib.sha256(token.encode()).hexdigest()}
    assert post(client, f"/api/admin/ads/partners/{pid}/postback-token", csrf, {"revoke": True}).json()["token"] is None
    assert publish(client, csrf, secrets_, clock, key="publish-key-2").status_code == 200
    assert policy.parse(config.ads_policy_file.read_bytes(), pub(config)).postback_hashes == {}
    client.cookies.clear()
    csrf_admin = login(client, clock, secrets_, "admin@x.test")
    assert post(client, f"/api/admin/ads/partners/{pid}/postback-token", csrf_admin, {}).status_code == 403


def test_analytics_by_segment_with_partner_conversions_and_long_periods(ads_env):
    config, clock, client, _, _, csrf, pid = ads_env
    cid = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()))).json()["id"]
    now = int(clock())
    t = Telemetry(config.telemetry_db)
    t.init_schema()
    old = now - 120 * DAY                                     # beyond the raw retention, inside 13 months
    t.exposure("impression", U1, cid, "A", "quick_pick", "fd:1", "AZ", old, "new")
    t.click(U1, cid, "A", "quick_pick", "fd:1", "AZ", old + 5, "new", "c" * 24, pid)
    t.rollup(old + DAY)
    t.purge(now)                                              # the raw rows of that day are gone
    t.exposure("impression", U2, cid, "A", "quick_pick", "fd:2", "TR", now - 10, "active")
    t.click(U2, cid, "A", "quick_pick", "fd:2", "TR", now - 5, "active", "d" * 24, pid)
    assert t.record_conversion(pid, "txn-1", "d" * 24, "registration", now) == "accepted"

    short = client.get(f"/api/admin/ads/analytics?days=30&campaign={cid}").json()
    a = next(v for v in short["variants"] if v["variant"] == "A")
    assert short["aggregated"] is False and (a["impressions"], a["reach"], a["partnerConversions"]) == (1, 1, 1)
    assert short["segments"] == [{"segment": "active", "impressions": 1, "clicks": 1, "control": 0}]
    assert short["postbacks"] == [{"campaignId": cid, "variant": "A", "event": "registration", "count": 1, "users": 1}]

    year = client.get(f"/api/admin/ads/analytics?days=365&campaign={cid}").json()
    a = next(v for v in year["variants"] if v["variant"] == "A")
    assert year["aggregated"] is True and (a["impressions"], a["clicks"]) == (2, 2)
    assert a["reach"] is None and a["uniqueCtr"] is None              # distinct users are not kept that long
    assert {s["segment"]: s["impressions"] for s in year["segments"]} == {"active": 1, "new": 1}
    csv_text = client.get(f"/api/admin/ads/analytics.csv?days=365&campaign={cid}").text
    assert "partner_conversions" in csv_text.splitlines()[0]
    assert client.get("/api/admin/ads/analytics?days=500").json()["error"] == "bad_period"


def test_query_token_permission_is_a_partner_setting(ads_env):
    config, clock, client, _, secrets_, csrf, pid = ads_env
    from miniapp.tests.test_ads_admin import partner_body
    assert post(client, "/api/admin/ads/partners", csrf, {**partner_body(), "id": pid,
                                                          "postbackQueryToken": True}).status_code == 200
    post(client, f"/api/admin/ads/partners/{pid}/postback-token", csrf, {})
    assert publish(client, csrf, secrets_, clock).status_code == 200
    assert policy.parse(config.ads_policy_file.read_bytes(), pub(config)).query_token_partners == frozenset({pid})
    assert post(client, "/api/admin/ads/partners", csrf, {**partner_body(), "id": pid,
                                                          "postbackQueryToken": "yes"}).status_code == 400
