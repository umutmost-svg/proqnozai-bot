# ruff: noqa: F811 — pytest fixtures imported from other test modules are used as parameters
"""Ads module, stage 4: analytics over the read-only telemetry, confirmed conversions, CSV."""
from __future__ import annotations

import sqlite3

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


def test_unique_ctr_counts_only_clickers_shown_the_variant_in_the_period(ads_env):
    # Codex P1: a click can outlive its impression; clickers > reach must not break the Wilson interval (500).
    config, clock, client, _, _, csrf, pid = ads_env
    cid = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()))).json()["id"]
    now = int(clock())
    t = Telemetry(config.telemetry_db)
    t.init_schema()
    t.exposure("impression", U1, cid, "A", "quick_pick", "fd:1", "AZ", now - 100)
    t.exposure("impression", U2, cid, "A", "quick_pick", "fd:1", "AZ", now - 30 * 86400)  # before the period
    for uid in (U1, U2, U2 + 1):  # U2+1 never had an impression recorded
        t.click(uid, cid, "A", "quick_pick", "fd:1", "AZ", now - 50)
    resp = client.get(f"/api/admin/ads/analytics?days=7&campaign={cid}")
    assert resp.status_code == 200
    a = next(v for v in resp.json()["variants"] if v["variant"] == "A")
    assert (a["reach"], a["clickers"], a["uniqueCtr"]) == (1, 3, 1.0)
    assert a["uniqueCtrCi"] == list(wilson(1, 1))


def test_unreadable_telemetry_is_reported_as_unavailable_not_zero(ads_env):
    config, clock, client, _, _, csrf, pid = ads_env
    post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock())))
    config.telemetry_db.parent.mkdir(parents=True, exist_ok=True)
    config.telemetry_db.write_bytes(b"not a database" * 100)
    report = client.get("/api/admin/ads/analytics?days=7").json()
    assert report["source"] == "unavailable" and report["days"] == []


def test_conversions_are_refused_while_telemetry_is_unreadable_and_attributed_on_retry(ads_env):
    # Codex P1: an unreadable telemetry must not store conversions as permanently unattributed.
    config, clock, client, accounts, _, csrf, pid = ads_env
    cid = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()))).json()["id"]
    post(client, f"/api/admin/users/{U1}/mb-id", csrf, {"mbId": "MB777"})
    body = {"mbIds": ["MB777"]}
    missing = post(client, f"/api/admin/ads/campaigns/{cid}/conversions", csrf, body)
    assert (missing.status_code, missing.json()["error"]) == (503, "telemetry_unavailable")
    config.telemetry_db.parent.mkdir(parents=True, exist_ok=True)
    config.telemetry_db.write_bytes(b"not a database" * 100)
    corrupt = post(client, f"/api/admin/ads/campaigns/{cid}/conversions", csrf, body)
    assert corrupt.status_code == 503
    assert accounts.events(action="ads.conversions") == []
    config.telemetry_db.unlink()
    seed(config, int(clock()), cid)
    assert post(client, f"/api/admin/ads/campaigns/{cid}/conversions", csrf, body).json()["attributed"] == 1


def test_largest_conversion_report_fits_the_request_body_limit(ads_env):
    from miniapp.admin_web.ads_analytics import MAX_REPORT
    from miniapp.admin_web.app import MAX_BODY
    import json
    assert len(json.dumps({"mbIds": ["M" * 32] * MAX_REPORT})) < MAX_BODY
    config, clock, client, _, _, csrf, pid = ads_env
    cid = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()))).json()["id"]
    seed(config, int(clock()), cid)
    ids = [f"{i:032d}" for i in range(MAX_REPORT)]
    assert post(client, f"/api/admin/ads/campaigns/{cid}/conversions", csrf, {"mbIds": ids}).json()["matched"] == 0
    too_many = post(client, f"/api/admin/ads/campaigns/{cid}/conversions", csrf, {"mbIds": ids[:1] * (MAX_REPORT + 1)})
    assert too_many.json()["error"] == "invalid_mb_ids"


def test_purge_marks_the_day_only_after_success_and_retries_hourly(tmp_path, monkeypatch):
    t = Telemetry(tmp_path / "t.db")
    t.init_schema()
    calls: list[int] = []

    def failing(now: int) -> None:
        calls.append(now)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(t, "purge", failing)
    now = 1_800_000_000
    with pytest.raises(sqlite3.OperationalError):
        t.maybe_purge(now)
    t.maybe_purge(now + 60)  # within the hour: no new attempt
    assert len(calls) == 1
    monkeypatch.setattr(t, "purge", lambda n: calls.append(n))
    t.maybe_purge(now + 3600)
    t.maybe_purge(now + 3700)  # done for the day
    assert len(calls) == 2


def test_unique_ctr_counts_only_clickers_who_were_shown_the_variant(ads_env):
    config, clock, client, _, _, csrf, pid = ads_env
    cid = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()))).json()["id"]
    t = seed(config, int(clock()), cid)
    t.click(U2 + 5, cid, "A", "quick_pick", "fd:9", "AZ", int(clock()) - 10)  # its impression is outside the data
    a = next(v for v in client.get(f"/api/admin/ads/analytics?days=7&campaign={cid}").json()["variants"]
             if v["variant"] == "A")
    assert a["clickers"] == 2 and a["reach"] == 2 and a["uniqueCtr"] == 0.5


def test_conversions_need_readable_telemetry(ads_env):
    _, clock, client, _, _, csrf, pid = ads_env
    cid = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()))).json()["id"]
    post(client, f"/api/admin/users/{U1}/mb-id", csrf, {"mbId": "MB777"})
    r = post(client, f"/api/admin/ads/campaigns/{cid}/conversions", csrf, {"mbIds": ["MB777"]})
    assert r.status_code == 503 and r.json()["error"] == "telemetry_unavailable"
