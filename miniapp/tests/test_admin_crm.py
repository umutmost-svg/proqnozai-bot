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
    assert client.get("/api/admin/broadcasts").status_code == 403  # texts and rules are free text
    analytics = client.get("/api/admin/analytics?days=30").json()
    assert analytics["real"]["tags"] == []  # tag labels are free text too


def test_analytics_real_parts_and_demo_only_on_request(env):
    _, clock, client, _, secrets_ = env
    login(client, clock, secrets_, "owner@x.test")
    a = client.get("/api/admin/analytics?days=90").json()
    assert a["real"]["totalUsers"] == 64 and sum(l["count"] for l in a["real"]["languages"]) == 64
    assert len(a["real"]["newByDay"]) == 91 and sum(map(sum, a["real"]["registrationHeat"])) == a["real"]["newUsers"]
    assert sum(d["new"] for d in a["real"]["newByDay"]) == a["real"]["newUsers"]
    day = client.get("/api/admin/analytics?days=1").json()["real"]
    assert sum(d["new"] for d in day["newByDay"]) == day["newUsers"] and len(day["newByDay"]) == 2
    assert a["demo"] is None and a["real"]["leagues"]
    assert client.get("/api/admin/analytics?days=30&demo=1").json()["demo"]["source"] == "demo"
    assert client.get("/api/admin/dialogs").json() == {"source": "not_collected"}
    assert client.get("/api/admin/dialogs?demo=1").json()["source"] == "demo"


def test_group_credit_key_is_bound_to_the_whole_user_list(env):
    _, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    U3 = DEMO_ID_BASE + 1074
    body = {"reason": "бонус", "idempotencyKey": "group-key-1", "kind": "grant", "amount": 5}
    ok = post(client, "/api/admin/credits/change", csrf, {**body, "userIds": [U1, U2], "code": code(secrets_["admin@x.test"], clock)})
    assert ok.status_code == 200
    clock.t += 30
    other = post(client, "/api/admin/credits/change", csrf, {**body, "userIds": [U1, U3], "code": code(secrets_["admin@x.test"], clock)})
    assert other.status_code == 409


def test_block_retry_after_a_crash_still_signs_the_user_out(env, monkeypatch):
    config, clock, client, _, secrets_ = env
    store = Store(config.miniapp_db)
    store.create_session("tok-crash", U1, "init-crash", int(clock()), 86400)
    csrf = login(client, clock, secrets_, "admin@x.test")

    def crash(*_a, **_k):
        raise RuntimeError("crash after the block was committed")

    monkeypatch.setattr(Store, "revoke_user_sessions", crash)
    body = {"reason": "спам", "idempotencyKey": "block-crash-1", "blocked": True}
    assert post(client, f"/api/admin/users/{U1}/block", csrf, {**body, "code": code(secrets_["admin@x.test"], clock)}).status_code == 500
    monkeypatch.undo()
    assert store.get_session("tok-crash").revoked_at is None
    clock.t += 30
    retry = post(client, f"/api/admin/users/{U1}/block", csrf, {**body, "code": code(secrets_["admin@x.test"], clock)})
    assert retry.json()["resumed"] is True and retry.json()["revokedSessions"] == 1
    assert store.get_session("tok-crash").revoked_at is not None


def test_journal_holds_no_crm_free_text(env):
    _, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    post(client, f"/api/admin/users/{U1}/tags", csrf, {"tag": "ivan@example.com"})
    post(client, "/api/admin/segments", csrf, {"name": "Сегмент Ивана", "rules": {"tag_has": "ivan@example.com"}})
    post(client, "/api/admin/broadcasts", csrf, {"name": "Секретная акция", "body": "текст"})
    dump = " ".join(str(e) for e in accounts.events())
    assert "ivan@example.com" not in dump and "Ивана" not in dump and "Секретная" not in dump
    card = client.get(f"/api/admin/users/{U1}").json()
    assert any("ivan@example.com" in t["title"] for t in card["timeline"])  # visible in the CRM, not the journal


def test_schedule_retry_survives_an_audience_change(env):
    _, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    bid = post(client, "/api/admin/broadcasts", csrf, {"name": "x", "body": "y", "rules": {"tag_has": "VIP"}}).json()["id"]
    at = int(clock()) + 7200
    body = {"reason": "план", "idempotencyKey": "sched-retry-1", "at": at}
    assert post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**body, "code": code(secrets_["admin@x.test"], clock)}).status_code == 200
    post(client, f"/api/admin/users/{U1}/tags", csrf, {"tag": "VIP"})  # the audience grows
    clock.t += 30
    retry = post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**body, "code": code(secrets_["admin@x.test"], clock)})
    assert retry.status_code == 200 and retry.json()["replayed"] is True and retry.json()["after"]["recipients"] == 0


def test_analytics_names_its_dataset(env):
    _, clock, client, _, secrets_ = env
    login(client, clock, secrets_, "owner@x.test")
    assert client.get("/api/admin/analytics?days=7").json()["dataset"] == "demo"


def test_completed_block_replay_never_revokes_later_sessions(env):
    config, clock, client, _, secrets_ = env
    store = Store(config.miniapp_db)
    csrf = login(client, clock, secrets_, "admin@x.test")
    body = {"reason": "спам", "idempotencyKey": "block-done-1", "blocked": True}
    assert post(client, f"/api/admin/users/{U1}/block", csrf, {**body, "code": code(secrets_["admin@x.test"], clock)}).status_code == 200
    store.create_session("tok-later", U1, "init-later", int(clock()), 86400)  # e.g. the Mini App ignored the flag
    replay = post(client, f"/api/admin/users/{U1}/block", csrf, {**body, "code": ""})
    assert replay.json()["replayed"] is True
    assert store.get_session("tok-later").revoked_at is None


def test_pending_block_is_completed_at_startup(env, monkeypatch):
    config, clock, client, accounts, secrets_ = env
    from miniapp.admin_web.app import create_admin_app
    store = Store(config.miniapp_db)
    store.create_session("tok-pend", U1, "init-pend", int(clock()), 86400)
    csrf = login(client, clock, secrets_, "admin@x.test")

    def crash(*_a, **_k):
        raise RuntimeError("crash")

    monkeypatch.setattr(Store, "revoke_user_sessions", crash)
    post(client, f"/api/admin/users/{U1}/block", csrf, {"reason": "спам", "idempotencyKey": "block-pend-1", "blocked": True,
                                                         "code": code(secrets_["admin@x.test"], clock)})
    monkeypatch.undo()
    assert accounts.pending_requests()
    create_admin_app(config, clock=clock)  # a restart finishes it
    assert not accounts.pending_requests()
    assert store.get_session("tok-pend").revoked_at is not None


def test_bulk_tags_are_atomic_and_need_existing_users(env):
    _, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    r = post(client, "/api/admin/users/bulk/tags", csrf, {"userIds": [U1, 123], "tag": "x"})
    assert r.status_code == 404 and client.get("/api/admin/users?tag=x").json()["total"] == 0
    assert post(client, "/api/admin/users/bulk/tags", csrf, {"userIds": [U1, U1, U2], "tag": "x"}).json()["tagged"] == 2


def test_schedule_replay_after_the_time_passed(env):
    _, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    bid = post(client, "/api/admin/broadcasts", csrf, {"name": "x", "body": "y"}).json()["id"]
    at = int(clock()) + 120
    body = {"reason": "план", "idempotencyKey": "sched-late-1", "at": at}
    assert post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**body, "code": code(secrets_["admin@x.test"], clock)}).status_code == 200
    clock.t += 1500  # the scheduled time is now past (the admin session is still alive)
    assert post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**body, "code": ""}).json()["replayed"] is True
