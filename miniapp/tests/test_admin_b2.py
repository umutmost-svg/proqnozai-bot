# ruff: noqa: F811 — pytest fixtures imported from test_admin_web are used as parameters
"""CRM audit part B2: every user is scanned (no silent 50k cut), heatmaps are averages per weekday x hour, creates are
idempotent per client key."""
from __future__ import annotations

from miniapp.admin_web.insights import per_slot_average, slot_occurrences
from miniapp.store import Store
from miniapp.tests.test_admin_web import env, login, post  # noqa: F401 — env is a fixture


def test_users_and_analytics_scan_every_user(env, monkeypatch):
    config, clock, client, accounts, secrets_ = env
    total = Store(config.miniapp_db).list_users(int(clock()), limit=1)[0]
    assert total > 20
    login(client, clock, secrets_, "owner@x.test")
    assert client.get("/api/admin/users?size=100").json()["total"] == total
    assert client.get("/api/admin/analytics?days=30").json()["real"]["totalUsers"] == total
    # Always fresh: a user who signs up is in the very next audience (no cached scan).
    from miniapp.telegram_auth import TelegramUser
    Store(config.miniapp_db).upsert_user(TelegramUser(990000123, "Новый", None, None, "ru"), int(clock()))
    assert client.get("/api/admin/users?size=100").json()["total"] == total + 1


def test_heatmap_cells_are_averages_per_occurrence():
    start = 1_799_884_800  # a Monday 00:00 UTC
    occ = slot_occurrences(start, start + 30 * 86400)
    assert sorted({sum(row) // 24 for row in occ}) == [4, 5]  # 30 days: some weekdays occur 5 times, others 4
    heat = [[occ[d][h] * 3 for h in range(24)] for d in range(7)]  # the same 3 per occurrence everywhere
    assert {v for row in per_slot_average(heat, start, start + 30 * 86400) for v in row} == {3}


def test_creates_with_the_same_key_make_one_record(env):
    config, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "owner@x.test")
    uid = Store(config.miniapp_db).list_users(int(clock()), limit=1)[1][0]["id"]
    n1 = post(client, f"/api/admin/users/{uid}/notes", csrf, {"body": "заметка", "idempotencyKey": "note-key-01"}).json()
    n2 = post(client, f"/api/admin/users/{uid}/notes", csrf, {"body": "заметка", "idempotencyKey": "note-key-01"}).json()
    assert n1["id"] == n2["id"] and n2["replayed"] is True
    s1 = post(client, "/api/admin/segments", csrf, {"name": "Сег", "rules": {}, "idempotencyKey": "seg-key-001"}).json()
    s2 = post(client, "/api/admin/segments", csrf, {"name": "Сег", "rules": {}, "idempotencyKey": "seg-key-001"}).json()
    assert s1["id"] == s2["id"]
    b1 = post(client, "/api/admin/broadcasts", csrf, {"name": "Б", "body": "т", "idempotencyKey": "bc-key-0001"}).json()
    b2 = post(client, "/api/admin/broadcasts", csrf, {"name": "Б", "body": "т", "idempotencyKey": "bc-key-0001"}).json()
    assert (b1["id"], b1["digest"]) == (b2["id"], b2["digest"]) and b2["replayed"] is True
    r1 = post(client, "/api/admin/referrals", csrf, {"name": "Р", "channel": "bot", "idempotencyKey": "ref-key-001"}).json()
    r2 = post(client, "/api/admin/referrals", csrf, {"name": "Р", "channel": "bot", "idempotencyKey": "ref-key-001"}).json()
    assert (r1["id"], r1["code"]) == (r2["id"], r2["code"])
    # The key is bound to its kind and author: reusing it for another kind is a conflict.
    assert post(client, "/api/admin/segments", csrf, {"name": "Другой", "rules": {}, "idempotencyKey": "note-key-01"}
                ).status_code == 409
    assert len([b for b in client.get("/api/admin/broadcasts").json()["items"] if b["name"] == "Б"]) == 1
    # The key is bound to the submitted data: a retry with edited fields is refused, never the old record returned.
    edited = post(client, "/api/admin/broadcasts", csrf, {"name": "Б", "body": "другой", "idempotencyKey": "bc-key-0001"})
    assert (edited.status_code, edited.json()["error"]) == (409, "idempotency_key_reused")
