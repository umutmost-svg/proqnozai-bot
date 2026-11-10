"""CRM layer of the admin pilot: tags, notes, messages, segments, broadcasts, credits, blocks, analytics."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from miniapp.admin_web.demo import DEMO_ID_BASE
from miniapp.store import Store
from miniapp.tests.test_admin_web import ORIGIN, code, env, login, post  # noqa: F401 — env is a fixture

U1 = DEMO_ID_BASE + 1000
U2 = DEMO_ID_BASE + 1037


def dangerous(secrets_, clock, email="admin@x.test", **extra):
    return {"reason": "проверка", "idempotencyKey": f"key-{clock.t}-{len(extra)}", "code": code(secrets_[email], clock),
            **extra}


def test_tags_filter_users_and_are_journalled(env):
    _, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    assert post(client, f"/api/admin/users/{U1}/tags", csrf, {"tag": "VIP"}).json()["created"] is True
    assert post(client, f"/api/admin/users/{U1}/tags", csrf, {"tag": "VIP"}).json()["created"] is False
    assert post(client, "/api/admin/users/bulk/tags", csrf, {"userIds": [U1, U2], "tag": "Ла Лига"}).json()["tagged"] == 2
    page = client.get("/api/admin/users?tag=VIP").json()
    assert page["total"] == 1 and page["items"][0]["tags"] == ["VIP", "Ла Лига"]
    assert {t["tag"] for t in page["tags"]} == {"VIP", "Ла Лига"}
    post(client, f"/api/admin/users/{U1}/tags/remove", csrf, {"tag": "VIP"})
    assert client.get("/api/admin/users?tag=VIP").json()["total"] == 0
    actions = [e["action"] for e in accounts.events(target=("user", str(U1)))]
    assert actions.count("user.tag_add") == 2 and "user.tag_remove" in actions


def test_note_and_message_are_stored_but_never_sent_in_the_pilot(env):
    _, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    post(client, f"/api/admin/users/{U1}/notes", csrf, {"body": "частный комментарий"})
    msg = post(client, f"/api/admin/users/{U1}/messages", csrf, {"body": "Привет!"}).json()
    assert msg["status"] == "not_sent_pilot"
    card = client.get(f"/api/admin/users/{U1}").json()
    assert card["notes"][0]["body"] == "частный комментарий" and card["messages"][0]["status"] == "not_sent_pilot"
    assert any(e["kind"] == "message" for e in card["timeline"]) and card["credits"]["enabled"] is False
    dump = " ".join(str(e) for e in accounts.events())
    assert "частный комментарий" not in dump and "Привет!" not in dump  # texts never enter the journal
    assert post(client, f"/api/admin/users/{U1}/messages", csrf, {"body": ""}).status_code == 400


def test_segments_validate_rules_and_count_audience(env):
    _, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    assert post(client, "/api/admin/segments", csrf, {"name": "Bad", "rules": {"sql": "1=1"}}).json()["error"] == "invalid_rules"
    post(client, f"/api/admin/users/{U1}/tags", csrf, {"tag": "VIP"})
    sid = post(client, "/api/admin/segments", csrf, {"name": "VIP ru", "rules": {"tag_has": "VIP"}}).json()["id"]
    seg = next(s for s in client.get("/api/admin/segments").json()["items"] if s["id"] == sid)
    assert seg["size"] == 1
    assert client.get(f"/api/admin/users?segment={sid}").json()["total"] == 1
    assert post(client, "/api/admin/segments", csrf, {"name": "VIP ru", "rules": {}}).status_code == 409
    preview = post(client, "/api/admin/audience/preview", csrf, {"rules": {"registered_within_days": 3650}}).json()
    assert preview["size"] == 64


def test_broadcast_draft_then_scheduled_with_step_up(env):
    _, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    bad = post(client, "/api/admin/broadcasts", csrf, {"name": "x", "body": "t", "buttons": [{"text": "a", "url": "javascript:1"}]})
    assert bad.json()["error"] == "invalid_buttons"
    bid = post(client, "/api/admin/broadcasts", csrf, {"name": "Тур ЛЧ", "body": "Текст", "rules": {"lang_in": ["ru"]},
                                                      "buttons": [{"text": "Открыть", "url": "miniapp"}]}).json()["id"]
    draft = next(b for b in client.get("/api/admin/broadcasts").json()["items"] if b["id"] == bid)
    assert draft["status"] == "draft" and draft["audience"] > 0
    at = int(clock()) + 3600
    no_code = post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**dangerous(secrets_, clock), "code": "000000", "at": at})
    assert no_code.json()["error"] == "invalid_code"
    ok = post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**dangerous(secrets_, clock), "at": at})
    assert ok.status_code == 200 and ok.json()["after"]["recipients"] == draft["audience"]
    assert post(client, "/api/admin/broadcasts", csrf, {"id": bid, "name": "x", "body": "y"}).json()["error"] == "not_a_draft"
    assert post(client, f"/api/admin/broadcasts/{bid}/cancel", csrf, {}).json()["status"] == "cancelled"


def test_credits_grant_debit_unlimited_and_ledger_is_append_only(env):
    config, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    grant = post(client, "/api/admin/credits/change", csrf, {**dangerous(secrets_, clock), "userIds": [U1], "kind": "grant", "amount": 50})
    assert grant.json()["after"]["results"][0]["balance"] == 50
    clock.t += 30
    over = post(client, "/api/admin/credits/change", csrf, {**dangerous(secrets_, clock), "userIds": [U1], "kind": "debit", "amount": 60})
    assert over.json()["error"] == "insufficient_balance"
    clock.t += 30
    group = post(client, "/api/admin/credits/change", csrf,
                 {**dangerous(secrets_, clock), "userIds": [U1, U2], "kind": "unlimited", "days": 30})
    assert group.status_code == 200
    page = client.get("/api/admin/credits").json()
    assert page["enabled"] is False and page["totals"]["unlimited"] == 2 and page["totals"]["granted"] == 50
    assert [l["kind"] for l in page["ledger"]][:3] == ["unlimited", "unlimited", "grant"]
    with sqlite3.connect(accounts.path) as conn, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM credit_ledger")
    assert post(client, "/api/admin/credits/change", csrf,
                {**dangerous(secrets_, clock), "userIds": [123], "kind": "grant", "amount": 1}).status_code == 404


def test_block_needs_step_up_and_signs_the_user_out(env):
    config, clock, client, _, secrets_ = env
    Store(config.miniapp_db).create_session("tok-block", U1, "init-block", int(clock()), 86400)
    csrf = login(client, clock, secrets_, "admin@x.test")
    r = post(client, f"/api/admin/users/{U1}/block", csrf, {**dangerous(secrets_, clock), "blocked": True})
    assert r.status_code == 200 and r.json()["revokedSessions"] >= 1
    assert Store(config.miniapp_db).get_session("tok-block").revoked_at is not None
    card = client.get(f"/api/admin/users/{U1}").json()
    assert card["blocked"]["reason"] == "проверка" and client.get("/api/admin/users?q=" + str(U1)).json()["items"][0]["blocked"]


def test_analyst_cannot_touch_crm(env):
    _, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "analyst@x.test")
    assert client.get("/api/admin/credits").status_code == 403
    assert client.get("/api/admin/segments").status_code == 403
    assert post(client, f"/api/admin/users/{U1}/tags", csrf, {"tag": "x"}).status_code == 403
    assert client.get("/api/admin/broadcasts").status_code == 200  # aggregate list, no personal data
    assert client.get("/api/admin/analytics?days=30").status_code == 200


def test_analytics_real_parts_and_demo_only_on_request(env):
    _, clock, client, _, secrets_ = env
    login(client, clock, secrets_, "owner@x.test")
    a = client.get("/api/admin/analytics?days=90").json()
    assert a["real"]["totalUsers"] == 64 and sum(l["count"] for l in a["real"]["languages"]) == 64
    assert len(a["real"]["newByDay"]) == 90 and sum(map(sum, a["real"]["registrationHeat"])) == a["real"]["newUsers"]
    assert a["demo"] is None and a["real"]["leagues"]
    assert client.get("/api/admin/analytics?days=30&demo=1").json()["demo"]["source"] == "demo"
    assert client.get("/api/admin/dialogs").json() == {"source": "not_collected"}
    assert client.get("/api/admin/dialogs?demo=1").json()["source"] == "demo"
