# ruff: noqa: F811 — pytest fixtures imported from test_admin_web are used as parameters
"""Users list / card: activity and GEO from existing telemetry, MB id history and SQL search, block history."""
from __future__ import annotations

from miniapp.ads.telemetry import Telemetry
from miniapp.admin_web import crm_routes
from miniapp.admin_web.insights import UserInsights, geo
from miniapp.store import Store
from miniapp.tests.test_admin_crm import U1, U2, dangerous
from miniapp.tests.test_admin_web import code, env, login, post  # noqa: F401 — env is a fixture


def _telemetry(config, clock):
    t = Telemetry(config.telemetry_db)
    t.init_schema()
    now = int(clock())
    t.record_activity(U1, now - 86400)
    t.record_activity(U1, now, "quick_single_ok")
    t.record_activity(U1, now, "ai_created")
    t.exposure("impression", U1, 1, "A", "home", "ev-1", "de", now)
    t.exposure("impression", U2, 1, "A", "home", "ev-1", None, now)
    return now


def test_insights_are_empty_without_telemetry(tmp_path):
    ins = UserInsights(tmp_path / "missing.telemetry.db")
    assert not ins.available() and ins.activity([1], 0) == {} and ins.edge_countries(None) == {}
    assert geo({"id": 1, "lang": "az"}, {}) == {"country": "AZ", "source": "lang"}
    assert geo({"id": 1, "lang": "ru"}, {}) == {"country": None, "source": None}  # ru: no single country
    assert geo({"id": 1, "lang": "pt-BR"}, {})["country"] is None  # every Portuguese locale is pt-BR in the Mini App


def test_store_users_by_ids_batches_and_skips_unknown(env):
    config, clock, *_ = env
    rows = Store(config.miniapp_db).users_by_ids([U2, U1, 1, U1], int(clock()))
    assert [r["id"] for r in rows] == [U1, U2] and set(rows[0]) >= {"lang", "createdAt", "activeSessions"}


def test_an_older_telemetry_schema_is_not_collected(tmp_path):
    import sqlite3
    path = tmp_path / "old.telemetry.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE user_activity (day TEXT, user_id INTEGER, last_at INTEGER)")
    assert UserInsights(path).available() is False


def test_mb_search_applies_rules_before_any_cap(env, monkeypatch):
    _, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    post(client, f"/api/admin/users/{U1}/mb-id", csrf, {"mbId": "Q1"})
    post(client, f"/api/admin/users/{U2}/mb-id", csrf, {"mbId": "Q2"})
    post(client, f"/api/admin/users/{U2}/tags", csrf, {"tag": "VIP"})
    monkeypatch.setattr(crm_routes, "MAX_SCAN", 1)
    assert [u["id"] for u in client.get("/api/admin/users?q=q&tag=VIP").json()["items"]] == [U2]


def test_users_list_and_card_show_activity_and_geo(env):
    config, clock, client, _, secrets_ = env
    recorded = _telemetry(config, clock)
    login(client, clock, secrets_, "admin@x.test")
    page = client.get("/api/admin/users?sort=forecasts&order=desc&size=5").json()
    assert page["telemetry"] is True
    top = page["items"][0]
    assert top["id"] == U1 and top["forecasts"] == 2 and top["activeDays30"] == 2
    assert top["geo"] == {"country": "DE", "source": "edge"} and top["lastSeenAt"] >= recorded
    assert client.get("/api/admin/users?sort=seen&order=desc&size=1").json()["items"][0]["id"] in (U1, U2)
    card = client.get(f"/api/admin/users/{U1}").json()
    assert card["activity"]["collected"] is True and card["activity"]["summary"]["aiCreated"] == 1
    assert len(card["activity"]["daily"]) == 30 and sum(d["forecasts"] for d in card["activity"]["daily"]) == 2
    other = client.get(f"/api/admin/users/{U2}").json()
    assert other["geo"]["source"] in ("lang", None)  # a NULL edge country is never shown as a country


def test_mb_id_history_keeps_values_outside_the_journal(env):
    _, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    post(client, f"/api/admin/users/{U1}/mb-id", csrf, {"mbId": "111"})
    post(client, f"/api/admin/users/{U1}/mb-id", csrf, {"mbId": "111"})  # unchanged: no history row
    post(client, f"/api/admin/users/{U1}/mb-id", csrf, {"mbId": "222"})
    post(client, f"/api/admin/users/{U1}/mb-id", csrf, {"mbId": ""})
    history = client.get(f"/api/admin/users/{U1}").json()["mbIdHistory"]
    assert [(h["old"], h["new"]) for h in history] == [("222", None), ("111", "222"), (None, "111")]
    assert history[0]["by"] == "admin@x.test"
    assert "222" not in " ".join(str(e) for e in accounts.events())


def test_mb_search_finds_owners_beyond_the_in_memory_scan(env, monkeypatch):
    _, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    post(client, f"/api/admin/users/{U1}/mb-id", csrf, {"mbId": "XyZ77"})
    monkeypatch.setattr(crm_routes, "MAX_SCAN", 1)  # the owner is outside the scanned page
    found = client.get("/api/admin/users?q=xyz7").json()
    assert [u["id"] for u in found["items"]] == [U1] and found["items"][0]["mbId"] == "XyZ77"
    assert client.get("/api/admin/users?q=xyz7&chip=blocked").json()["total"] == 0  # rules still apply


def test_blocked_chip_and_block_history(env):
    config, clock, client, _, secrets_ = env
    Store(config.miniapp_db).create_session("tok-h", U1, "init-h", int(clock()), 86400)
    csrf = login(client, clock, secrets_, "admin@x.test")
    assert post(client, f"/api/admin/users/{U1}/block", csrf, {**dangerous(secrets_, clock), "blocked": True}).status_code == 200
    page = client.get("/api/admin/users?chip=blocked").json()
    assert [u["id"] for u in page["items"]] == [U1] and page["counts"]["chips"]["blocked"] == 1
    clock.t += 31
    body = {"reason": "ошибка", "idempotencyKey": "unblock-0001", "blocked": False, "code": code(secrets_["admin@x.test"], clock)}
    assert post(client, f"/api/admin/users/{U1}/block", csrf, body).status_code == 200
    history = client.get(f"/api/admin/users/{U1}").json()["blockHistory"]
    assert [(h["blocked"], h["reason"]) for h in history] == [(False, "ошибка"), (True, "проверка")]


def test_activity_sort_through_the_bulk_path_puts_unknown_last_both_ways(env, monkeypatch):
    config, clock, client, _, secrets_ = env
    _telemetry(config, clock)
    login(client, clock, secrets_, "admin@x.test")
    monkeypatch.setattr(crm_routes, "ACTIVITY_BULK", 0)  # force the whole-table aggregate
    desc = client.get("/api/admin/users?sort=forecasts&order=desc&size=100").json()["items"]
    asc = client.get("/api/admin/users?sort=forecasts&order=asc&size=100").json()["items"]
    assert desc[0]["id"] == U1 and desc[0]["forecasts"] == 2
    for items in (desc, asc):
        known = [u["forecasts"] for u in items if u["forecasts"] is not None]
        assert [u["forecasts"] for u in items[:len(known)]] == known and all(u["forecasts"] is None for u in items[len(known):])
    assert known == sorted(known)
