# ruff: noqa: F811 — pytest fixtures imported from other test modules are used as parameters
"""Ads module, stage 4: analytics over the read-only telemetry, confirmed conversions, CSV."""
from __future__ import annotations

import pytest

from miniapp.ads.telemetry import Telemetry
from miniapp.admin_web.ads_analytics import wilson
from miniapp.tests.test_admin_web import env, login, post  # noqa: F401 — env is a fixture
from miniapp.tests.test_ads_admin import U1, U2, ads_env, campaign_body  # noqa: F401 — ads_env is a fixture


def test_wilson_interval():
    assert wilson(0, 0) is None
    low, high = wilson(5, 10)
    assert abs(low - 0.2366) < 1e-3 and abs(high - 0.7634) < 1e-3
    assert wilson(0, 10)[0] == 0.0 and wilson(10, 10)[1] == 1.0


def seed(config, now: int, cid: int) -> Telemetry:
    t = Telemetry(config.telemetry_db)
    t.init_schema()
    for uid, variant in ((U1, "A"), (U2, "A"), (U2 + 1, "B")):
        t.exposure("impression", uid, cid, variant, "quick_pick", "fd:1", "AZ", now - 100)
        t.exposure("impression", uid, cid, variant, "quick_pick", "fd:2", "TR", now - 90)
    t.exposure("control", U2 + 2, cid, "control", "quick_pick", "fd:1", "AZ", now - 80)
    t.click(U1, cid, "A", "quick_pick", "fd:1", "AZ", now - 50)
    t.click(U1, cid, "A", "quick_pick", "fd:2", "AZ", now - 30)
    return t


def test_report_by_variant_day_and_country_for_every_role(ads_env):
    config, clock, client, _, secrets_, csrf, pid = ads_env
    cid = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()), name="=HYPERLINK(1)")).json()["id"]
    assert client.get("/api/admin/ads/analytics?days=7").json()["source"] == "not_collected"
    seed(config, int(clock()), cid)
    report = client.get(f"/api/admin/ads/analytics?days=7&campaign={cid}").json()
    a, b, = [v for v in report["variants"] if v["variant"] in ("A", "B")]
    assert (a["impressions"], a["reach"], a["clicks"], a["clickers"]) == (4, 2, 2, 1)
    assert a["ctr"] == 0.5 and a["uniqueCtr"] == 0.5 and a["uniqueCtrCi"] == list(wilson(1, 2))
    assert (b["impressions"], b["reach"], b["clicks"]) == (2, 1, 0)
    control = next(v for v in report["variants"] if v["variant"] == "control")
    assert control["control"] == 1 and control["impressions"] == 0
    assert {c["country"]: c["impressions"] for c in report["countries"]} == {"AZ": 3, "TR": 3}
    assert sum(d["impressions"] for d in report["days"]) == 6
    assert str(U1) not in str(report)  # aggregates only
    csv_text = client.get(f"/api/admin/ads/analytics.csv?days=7&campaign={cid}").text
    assert "'=HYPERLINK(1)" in csv_text and ",=HYPERLINK" not in csv_text
    assert client.get("/api/admin/ads/analytics?days=5").json()["error"] == "bad_period"
    assert client.get("/api/admin/ads/analytics?days=7&campaign=x").json()["error"] == "bad_filter"
    client.cookies.clear()
    login(client, clock, secrets_, "analyst@x.test")
    assert client.get("/api/admin/ads/analytics?days=7").status_code == 200


def test_confirmed_conversions_are_attributed_to_the_last_clicked_variant(ads_env):
    config, clock, client, accounts, secrets_, csrf, pid = ads_env
    cid = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()))).json()["id"]
    seed(config, int(clock()), cid)
    post(client, f"/api/admin/users/{U1}/mb-id", csrf, {"mbId": "MB777"})
    post(client, f"/api/admin/users/{U2}/mb-id", csrf, {"mbId": "MB888"})
    r = post(client, f"/api/admin/ads/campaigns/{cid}/conversions", csrf, {"mbIds": ["mb777", "MB888", "NOPE"]}).json()
    assert r == {"reported": 3, "matched": 2, "added": 2, "attributed": 1}
    again = post(client, f"/api/admin/ads/campaigns/{cid}/conversions", csrf, {"mbIds": ["MB777"]}).json()
    assert again["added"] == 0  # one conversion per campaign and user
    report = client.get(f"/api/admin/ads/analytics?days=7&campaign={cid}").json()
    by_variant = {v["variant"]: v["conversions"] for v in report["variants"]}
    assert by_variant["A"] == 1 and by_variant["unattributed"] == 1
    journal = str(accounts.events(action="ads.conversions"))
    assert "MB777" not in journal and str(U1) not in journal
    assert post(client, f"/api/admin/ads/campaigns/{cid}/conversions", csrf, {"mbIds": []}).json()["error"] == "invalid_mb_ids"
    assert post(client, "/api/admin/ads/campaigns/999/conversions", csrf, {"mbIds": ["X"]}).status_code == 404
    client.cookies.clear()
    csrf = login(client, clock, secrets_, "analyst@x.test")
    assert post(client, f"/api/admin/ads/campaigns/{cid}/conversions", csrf, {"mbIds": ["MB777"]}).status_code == 403


@pytest.mark.parametrize("bad", ["", 0, "AZ"])
def test_malformed_countries_are_rejected_not_widened(ads_env, bad):
    _, clock, client, _, _, csrf, pid = ads_env
    body = campaign_body(pid, int(clock()), countries=bad)
    assert post(client, "/api/admin/ads/campaigns", csrf, body).json()["error"] == "invalid_countries"
