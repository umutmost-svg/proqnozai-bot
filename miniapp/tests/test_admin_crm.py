# ruff: noqa: F811 — pytest fixtures imported from test_admin_web are used as parameters
"""CRM layer of the admin pilot: tags, notes, messages, segments, broadcasts, credits, blocks, analytics."""
from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from miniapp.admin_web.demo import DEMO_ID_BASE
from miniapp.admin_web.insights import slot_occurrences
from miniapp.credits import Credits
from miniapp.store import Store
from miniapp.tests.test_admin_web import digest_of, reviewed, fresh, ORIGIN, code, env, login, post  # noqa: F401 — env is a fixture

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


def test_note_and_message_are_stored_and_the_message_queued_for_the_bot(env):
    _, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    post(client, f"/api/admin/users/{U1}/notes", csrf, {"body": "частный комментарий"})
    msg = post(client, f"/api/admin/users/{U1}/messages", csrf, {"body": "Привет!", "idempotencyKey": "msg-key-0001"}).json()
    assert msg["status"] == "queued"
    card = client.get(f"/api/admin/users/{U1}").json()
    assert card["notes"][0]["body"] == "частный комментарий" and card["messages"][0]["status"] == "queued"
    assert any(e["kind"] == "message" for e in card["timeline"]) and card["credits"]["enabled"] is False
    dump = " ".join(str(e) for e in accounts.events())
    assert "частный комментарий" not in dump and "Привет!" not in dump  # texts never enter the journal
    assert post(client, f"/api/admin/users/{U1}/messages", csrf, {"body": "", "idempotencyKey": "msg-key-0002"}).status_code == 400


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
    no_code = post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**dangerous(secrets_, clock), "code": "000000", **reviewed(client, bid), "at": at})
    assert no_code.json()["error"] == "invalid_code"
    ok = post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**dangerous(secrets_, clock), **reviewed(client, bid), "at": at})
    assert ok.status_code == 200 and ok.json()["after"]["recipients"] == draft["audience"]
    assert post(client, "/api/admin/broadcasts", csrf, {"id": bid, "name": "x", "body": "y"}).json()["error"] == "not_a_draft"
    assert post(client, f"/api/admin/broadcasts/{bid}/cancel", csrf, {}).json()["error"] == "reason_required"
    clock.t += 30
    cancel = post(client, f"/api/admin/broadcasts/{bid}/cancel", csrf, {**dangerous(secrets_, clock), "idempotencyKey": "cancel-key-1"})
    assert cancel.json()["after"] == {"status": "cancelled"}


def _apply_commands(config, clock) -> Credits:
    """The Mini App's view of the credits the admin wrote (directly into miniapp.db)."""
    return Credits(Store(config.miniapp_db), clock)


def test_credits_are_queued_to_the_mini_app_and_read_back_from_it(env):
    config, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    grant = post(client, "/api/admin/credits/change", csrf, {**dangerous(secrets_, clock), "userIds": [U1], "kind": "grant", "amount": 50})
    assert grant.json()["after"]["applied"] == 1
    clock.t += 30
    over = post(client, "/api/admin/credits/change", csrf, {**dangerous(secrets_, clock), "userIds": [U1], "kind": "debit", "amount": 1000})
    assert over.status_code == 200  # the Mini App floors a debit at zero
    clock.t += 30
    group = post(client, "/api/admin/credits/change", csrf,
                 {**dangerous(secrets_, clock), "userIds": [U1, U2], "kind": "unlimited", "days": 30})
    assert group.status_code == 200
    page = client.get("/api/admin/credits").json()
    assert page["enabled"] is False and page["totals"]["unlimited"] == 2
    assert [l["kind"] for l in page["ledger"] if l["userId"] == U1][:4] == ["unlimited", "monthly", "debit", "grant"]  # 0 -> the month's top-up
    card = client.get(f"/api/admin/users/{U1}").json()
    assert card["credits"]["balance"] == 50 and card["credits"]["unlimitedUntil"]
    assert post(client, "/api/admin/credits/change", csrf,
                {**dangerous(secrets_, clock), "userIds": [123], "kind": "grant", "amount": 1}).status_code == 404


def test_the_credits_switch_is_the_owners_and_reaches_the_mini_app(env):
    config, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    assert post(client, "/api/admin/settings/credits.enabled", csrf, {**dangerous(secrets_, clock), "value": True}).status_code == 403
    client.cookies.clear()
    csrf = login(client, clock, secrets_, "owner@x.test")
    r = post(client, "/api/admin/settings/credits.enabled", csrf,
             {"reason": "запуск", "code": code(secrets_["owner@x.test"], clock), "idempotencyKey": "switch-on-0001", "value": True})
    assert r.status_code == 200, r.text
    assert _apply_commands(config, clock).enabled()


def test_owner_approves_mb_ids_entered_in_the_mini_app(env):
    config, clock, client, accounts, secrets_ = env
    credits = Credits(Store(config.miniapp_db), clock)
    credits.submit_mb(U1, "MBX1")
    credits.submit_mb(U2, "MBX2")
    csrf = login(client, clock, secrets_, "admin@x.test")
    assert client.get("/api/admin/mb").json()["counts"]["pending"] == 2
    assert post(client, f"/api/admin/mb/{U1}/decide", csrf, {"decision": "approve", "mbId": "MBX1", "submission": "x"}).status_code == 403
    client.cookies.clear()
    csrf = login(client, clock, secrets_, "owner@x.test")
    items = {i["userId"]: i for i in client.get("/api/admin/mb").json()["items"]}
    no_code = post(client, f"/api/admin/mb/{U1}/decide", csrf,
                   {"decision": "approve", "mbId": "MBX1", "submission": items[U1]["submission"]})
    assert no_code.json()["error"] == "invalid_code"  # an approval needs a fresh 2FA code (CRM audit B)
    ok = post(client, f"/api/admin/mb/{U1}/decide", csrf,
              {"decision": "approve", "mbId": "MBX1", "submission": items[U1]["submission"], "code": fresh(secrets_, clock)})
    assert ok.json() == {"decision": "approved"}
    assert post(client, f"/api/admin/mb/{U2}/decide", csrf,
                {"decision": "reject", "mbId": "MBX2", "submission": items[U2]["submission"]}).json()["error"] == "reason_required"
    post(client, f"/api/admin/mb/{U2}/decide", csrf,
         {"decision": "reject", "mbId": "MBX2", "submission": items[U2]["submission"], "reason": "нет аккаунта"})
    assert client.get("/api/admin/mb").json()["counts"] == {"pending": 0, "approved": 1, "rejected": 1}
    assert credits.summary(U2)["mb"]["reason"] == "нет аккаунта"
    # A decision on a submission the user replaced since is refused (never a false verified ID here).
    credits.submit_mb(U2, "MBX3")
    stale = post(client, f"/api/admin/mb/{U2}/decide", csrf, {"decision": "approve", "mbId": "MBX2", "submission": items[U2]["submission"],
                                                              "code": fresh(secrets_, clock)})
    assert stale.json()["error"] == "mb_stale" and client.get(f"/api/admin/users/{U2}").json()["mbId"] is None
    card = client.get(f"/api/admin/users/{U1}").json()
    assert card["mbId"] == "MBX1" and card["mbLink"]["state"] == "approved"  # also the ads' MB ID


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
    occ = slot_occurrences(int(clock()) - 90 * 86400, int(clock()))  # cells are averages per occurrence (CRM audit B2)
    assert len(a["real"]["newByDay"]) == 91 and round(sum(
        v * occ[d][h] for d, row in enumerate(a["real"]["registrationHeat"]) for h, v in enumerate(row))) == a["real"]["newUsers"]
    assert sum(d["new"] for d in a["real"]["newByDay"]) == a["real"]["newUsers"]
    day = client.get("/api/admin/analytics?days=1").json()["real"]
    assert sum(d["new"] for d in day["newByDay"]) == day["newUsers"] and len(day["newByDay"]) == 2
    assert a["demo"] is None and a["real"]["leagues"]
    assert client.get("/api/admin/analytics?days=30&demo=1").json()["demo"]["source"] == "demo"
    assert client.get("/api/admin/dialogs").json()["error"] == "dialogs_not_allowed"  # no explicit grant yet


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
    body = {"reason": "план", "idempotencyKey": "sched-retry-1", "at": at, **reviewed(client, bid)}
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
    body = {"reason": "план", "idempotencyKey": "sched-late-1", "at": at, **reviewed(client, bid)}
    assert post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**body, "code": code(secrets_["admin@x.test"], clock)}).status_code == 200
    clock.t += 1500  # the scheduled time is now past (the admin session is still alive)
    assert post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**body, "code": ""}).json()["replayed"] is True


def test_analyst_journal_is_an_allowlist_even_for_group_events(env):
    _, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    post(client, "/api/admin/credits/change", csrf, {"reason": "бонус Ивану", "idempotencyKey": "group-analyst-1", "kind": "grant",
                                                     "amount": 3, "userIds": [U1, U2], "code": code(secrets_["admin@x.test"], clock)})
    analyst = TestClient(client.app, base_url=ORIGIN)
    login(analyst, clock, secrets_, "analyst@x.test")
    body = analyst.get("/api/admin/events?days=7").text
    assert str(U1) not in body and "Ивану" not in body and "@x.test" not in body


def test_reasons_live_outside_the_immutable_journal(env):
    config, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    post(client, "/api/admin/credits/change", csrf, {"reason": "личное: Иван Петров", "idempotencyKey": "reason-key-1", "kind": "grant",
                                                     "amount": 3, "userIds": [U1], "code": code(secrets_["admin@x.test"], clock)})
    with sqlite3.connect(config.admin_db) as conn:
        journal = " ".join(str(r) for r in conn.execute("SELECT * FROM admin_events"))
        ledger = " ".join(str(r) for r in conn.execute("SELECT * FROM credit_ledger"))
        conn.execute("DELETE FROM admin_event_reasons")  # erasable on request (no immutability trigger)
    assert "Иван" not in journal and "Иван" not in ledger
    assert any(e["reason"] is None for e in accounts.events() if e["action"] == "credits.change")


def test_recovered_revocation_count_is_unknown(env, monkeypatch):
    config, clock, client, accounts, secrets_ = env
    from miniapp.admin_web.app import create_admin_app
    csrf = login(client, clock, secrets_, "admin@x.test")
    monkeypatch.setattr(Store, "revoke_user_sessions", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("crash")))
    post(client, f"/api/admin/users/{U1}/block", csrf, {"reason": "спам", "idempotencyKey": "recover-key-1", "blocked": True,
                                                         "code": code(secrets_["admin@x.test"], clock)})
    monkeypatch.undo()
    create_admin_app(config, clock=clock)
    done = [e for e in accounts.events(target=("user", str(U1))) if e["result"] == "completed"]
    assert done and done[0]["after"] == {"revoked": None}


def test_broadcast_audience_excludes_blocked_users(env):
    _, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    everyone = post(client, "/api/admin/audience/preview", csrf, {"rules": {}, "forBroadcast": True}).json()["size"]
    post(client, f"/api/admin/users/{U1}/block", csrf, {**dangerous(secrets_, clock), "blocked": True})
    assert post(client, "/api/admin/audience/preview", csrf, {"rules": {}, "forBroadcast": True}).json()["size"] == everyone - 1
    assert post(client, "/api/admin/audience/preview", csrf, {"rules": {}}).json()["size"] == everyone  # segments count all


def test_replay_still_works_after_the_reason_text_is_erased(env):
    config, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    body = {"reason": "личная причина", "idempotencyKey": "erase-key-1", "kind": "grant", "amount": 2, "userIds": [U1]}
    assert post(client, "/api/admin/credits/change", csrf, {**body, "code": code(secrets_["admin@x.test"], clock)}).status_code == 200
    with sqlite3.connect(config.admin_db) as conn:
        conn.execute("DELETE FROM admin_event_reasons")
    replay = post(client, "/api/admin/credits/change", csrf, {**body, "code": ""})
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    other = post(client, "/api/admin/credits/change", csrf, {**body, "reason": "другая", "code": ""})
    assert other.status_code == 409


def test_mb_id_set_search_unique_and_kept_out_of_the_journal(env):
    _, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "owner@x.test")
    assert post(client, f"/api/admin/users/{U1}/mb-id", csrf, {"mbId": "  123456789 ", "code": fresh(secrets_, clock)}).json() == {"mbId": "123456789"}
    assert client.get(f"/api/admin/users/{U1}").json()["mbId"] == "123456789"
    found = client.get("/api/admin/users?q=123456789").json()
    assert [u["id"] for u in found["items"]] == [U1] and found["items"][0]["mbId"] == "123456789"
    assert [u["id"] for u in client.get("/api/admin/users?q=34567").json()["items"]] == [U1]  # partial, like other fields
    assert post(client, f"/api/admin/users/{U2}/mb-id", csrf, {"mbId": "123456789", "code": fresh(secrets_, clock)}).status_code == 409
    assert post(client, f"/api/admin/users/{U2}/mb-id", csrf, {"mbId": "AbC", "code": fresh(secrets_, clock)}).json() == {"mbId": "AbC"}
    assert post(client, f"/api/admin/users/{U1}/mb-id", csrf, {"mbId": "abc", "code": fresh(secrets_, clock)}).status_code == 409  # case-insensitive
    assert [u["id"] for u in client.get("/api/admin/users?q=aBc").json()["items"]] == [U2]
    analyst_view = TestClient(client.app, base_url=ORIGIN)
    login(analyst_view, clock, secrets_, "analyst@x.test")
    assert analyst_view.get(f"/api/admin/users/{U2}").status_code == 403
    assert "AbC" not in analyst_view.get("/api/admin/events").text
    assert post(client, f"/api/admin/users/{U2}/mb-id", csrf, {"mbId": "bad id!", "code": fresh(secrets_, clock)}).json()["error"] == "invalid_mb_id"
    assert "123456789" not in " ".join(str(e) for e in accounts.events())
    assert post(client, f"/api/admin/users/{U1}/mb-id", csrf, {"mbId": "", "code": fresh(secrets_, clock)}).json() == {"mbId": None}
    assert client.get(f"/api/admin/users/{U1}").json()["mbId"] is None
    analyst = TestClient(client.app, base_url=ORIGIN)
    login(analyst, clock, secrets_, "analyst@x.test")
    assert post(analyst, f"/api/admin/users/{U1}/mb-id", analyst.get("/api/admin/auth/me").json()["csrf"], {"mbId": "1", "code": fresh(secrets_, clock)}).status_code == 403


def test_a_broadcast_can_ask_the_bot_to_delete_it_later(env):
    """Auto-delete (owner 15.11): 1, 6, 12, 24 or 47 hours after sending (Telegram allows a bot 48 h); stored with the
    draft and passed to every queued job."""
    _, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    for bad in (2, 48, 0, True, "6"):
        r = post(client, "/api/admin/broadcasts", csrf, {"name": "x", "body": "t", "deleteAfterHours": bad})
        assert r.json()["error"] == "invalid_delete_after"
    bid = post(client, "/api/admin/broadcasts", csrf, {"name": "Временная", "body": "Текст", "rules": {"lang_in": ["ru"]},
                                                      "deleteAfterHours": 6}).json()["id"]
    draft = next(b for b in client.get("/api/admin/broadcasts").json()["items"] if b["id"] == bid)
    assert draft["deleteAfterHours"] == 6


def test_behaviour_filters_never_count_users_without_data(env):
    """No telemetry collected: a "did not" filter must give 0, not everyone (unknown is not "no")."""
    _, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    everyone = post(client, "/api/admin/audience/preview", csrf, {"rules": {}}).json()["size"]
    assert everyone > 0
    for rules in ({"opened_miniapp": False}, {"used_ai": False}, {"country_in": ["AZ"]}, {"clicked_ad": False}):
        assert post(client, "/api/admin/audience/preview", csrf, {"rules": rules}).json()["size"] == 0, rules
    assert post(client, "/api/admin/audience/preview", csrf, {"rules": {"country_in": ["az"]}}).status_code == 400


def test_deposit_rates_by_country_reach_the_mini_app(env):
    config, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "owner@x.test")
    owner = lambda key: {"reason": "курсы", "code": code(secrets_["owner@x.test"], clock), "idempotencyKey": key}  # noqa: E731
    bad = post(client, "/api/admin/settings/credits.rates", csrf, {**owner("rates-bad-001"), "value": [{"country": "AZE"}]})
    assert bad.json()["error"] == "invalid_rates"
    clock.t += 30
    rates = [{"country": "TR", "currency": "TRY", "perForecast": 100}]
    assert post(client, "/api/admin/settings/credits.rates", csrf, {**owner("rates-ok-0001"), "value": rates}).status_code == 200
    assert client.get("/api/admin/credits").json()["rates"] == rates
    assert _apply_commands(config, clock).summary(U1, "TR")["deposit"] == {"currency": "TRY", "perForecast": 100}


def test_mb_ids_entered_earlier_in_the_panel_are_approved_in_the_mini_app(env):
    from miniapp.admin_web.app import create_admin_app
    config, clock, client, accounts, secrets_ = env
    with accounts._connect() as conn:  # an ID the owner typed into a card before the Mini App knew MB IDs
        conn.execute("INSERT INTO crm_external_ids (user_id, mb_id, updated_at, admin_id) VALUES (?, 'OLD7', 1, 1)",
                     (U1,))
    create_admin_app(config, clock=clock)  # the next start of the panel
    [link] = Store(config.miniapp_db).mb_links(user_ids=[U1])
    assert (link["mbId"], link["state"]) == ("OLD7", "approved")
    create_admin_app(config, clock=clock)  # once: a second start changes nothing
    assert len(Store(config.miniapp_db).mb_links(user_ids=[U1])) == 1
    # A user who already has a Mini App link keeps it: a CRM value is never pushed over it.
    with accounts._connect() as conn:
        conn.execute("UPDATE crm_external_ids SET mb_id = 'STALE7' WHERE user_id = ?", (U1,))
    create_admin_app(config, clock=clock)
    assert Store(config.miniapp_db).mb_links(user_ids=[U1])[0]["mbId"] == "OLD7"


# ---- CRM audit (17.11): broadcast text, delivery status, reachability ----------------------------------------------

def test_broadcast_html_is_checked_and_rendered_per_recipient():
    from miniapp.admin_web.crm import render_broadcast

    assert render_broadcast("<b>{имя}</b>, a < b & c > d &amp; <a href=\"https://x.example/p\">тут</a>", "Ali <&>") == \
        "<b>Ali &lt;&amp;&gt;</b>, a &lt; b &amp; c &gt; d &amp; <a href=\"https://x.example/p\">тут</a>"
    assert render_broadcast("Привет, {имя}!", None) == "Привет, !"
    for bad in ("<b>open", "<i>x</b>", "<a>no href</a>", "<b href=\"https://x.example\">x</b>", "x</b>",
                "<a href=\"https://x.example:bad\">x</a>", "<code><b>x</b></code>", "<pre><i>x</i></pre>",
                "<a href=\"https://x.example\"><a href=\"https://y.example\">x</a></a>"):
        try:
            render_broadcast(bad, "A")
        except ValueError:
            continue
        raise AssertionError(bad)
    assert render_broadcast("<script>x</script>", "A") == "&lt;script&gt;x&lt;/script&gt;"   # unknown tags are text


def test_broadcast_jobs_carry_the_name_and_html_and_the_list_settles_as_sent(env):
    import sqlite3 as sq
    from bot_outbox import BotOutbox, Outcome
    import asyncio

    config, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    assert post(client, "/api/admin/broadcasts", csrf, {"name": "x", "body": "<b>open"}).json()["error"] == "invalid_html"
    assert post(client, "/api/admin/broadcasts", csrf, {"name": "x", "body": "t", "buttons": [
        {"text": "a", "url": "https://"}]}).json()["error"] == "invalid_buttons"
    first = client.get(f"/api/admin/users/{U1}").json()["firstName"]
    bid = post(client, "/api/admin/broadcasts", csrf, {"name": "Имя", "body": "<b>{имя}</b>, привет",
                                                      "rules": {"tag_has": "Получатели"}}).json()["id"]
    post(client, f"/api/admin/users/{U1}/tags", csrf, {"tag": "Получатели"})
    at = int(clock()) + 60
    assert post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**dangerous(secrets_, clock), **reviewed(client, bid), "at": at}).status_code == 200
    with sq.connect(config.data_dir / "bot-outbox.db") as conn:
        text, mode = conn.execute("SELECT text, parse_mode FROM jobs WHERE batch_id = ?", (f"bc:{bid}",)).fetchone()
    assert text.startswith("<b>") and "{имя}" not in text and (first or "") in text and mode == "HTML"
    item = lambda: next(b for b in client.get("/api/admin/broadcasts").json()["items"] if b["id"] == bid)  # noqa: E731
    assert item()["status"] == "scheduled" and item()["delivery"] == {"queued": 1}
    bot = BotOutbox(config.data_dir / "bot-outbox.db")

    async def send(job):
        return Outcome("sent", message_id=1)
    asyncio.run(bot.run_once(send, lambda: at))
    clock.t = at + 1
    sent = item()
    assert sent["status"] == "sent" and sent["delivery"] == {"sent": 1}
    cancel = post(client, f"/api/admin/broadcasts/{bid}/cancel", csrf, {**dangerous(secrets_, clock), "idempotencyKey": "late-cancel-1"})
    assert cancel.json()["error"] == "not_cancellable"


def test_users_telegram_refused_and_users_blocked_later_get_no_broadcast(env):
    import sqlite3 as sq

    config, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    everyone = post(client, "/api/admin/audience/preview", csrf, {"rules": {}, "forBroadcast": True}).json()["size"]
    with sq.connect(config.data_dir / "bot-outbox.db") as conn:   # Telegram answered 403 for U2 last time
        conn.execute("INSERT INTO jobs (id, idempotency_key, user_id, text, not_before, expires_at, status, "
                     "last_error_code, created_at, claimed_at) VALUES ('x', 'msg:x', ?, 't', 0, 1, 'failed', "
                     "'blocked_by_user', 1, 1)", (U2,))
    assert post(client, "/api/admin/audience/preview", csrf, {"rules": {}, "forBroadcast": True}).json()["size"] == everyone - 1
    bid = post(client, "/api/admin/broadcasts", csrf, {"name": "B", "body": "t"}).json()["id"]
    post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**dangerous(secrets_, clock), **reviewed(client, bid), "at": int(clock()) + 3600})
    clock.t += 31
    post(client, f"/api/admin/users/{U1}/block", csrf, {**dangerous(secrets_, clock), "blocked": True})
    with sq.connect(config.data_dir / "bot-outbox.db") as conn:
        assert conn.execute("SELECT status FROM jobs WHERE idempotency_key = ?", (f"bc:{bid}:{U1}",)).fetchone() == ("cancelled",)
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE batch_id = ? AND status = 'queued'", (f"bc:{bid}",)).fetchone()[0] > 0


def test_an_inquiry_from_a_bot_only_user_can_be_answered(env):
    from miniapp.admin_web.bot_crm import BotCrmFile

    config, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    stranger = 555_000_111
    body = {"body": "Ответ", "idempotencyKey": "reply-key-01"}
    assert post(client, f"/api/admin/users/{stranger}/messages", csrf, body).status_code == 404
    f = BotCrmFile(config.bot_crm_db)
    f.init_schema()
    with f._connect() as conn:
        conn.execute("INSERT INTO inquiry_notices (user_id, at, days) VALUES (?, 1, 90)", (stranger,))
        conn.execute("INSERT INTO inquiries (user_id, at, text) VALUES (?, ?, 'не открывается')", (stranger, int(clock())))
    assert post(client, f"/api/admin/users/{stranger}/messages", csrf, body).json()["status"] == "queued"


def test_odd_digit_searches_are_text_not_a_server_error(env):
    _, clock, client, _, secrets_ = env
    login(client, clock, secrets_, "admin@x.test")
    for q in ("²", "1" * 25):
        assert client.get(f"/api/admin/users?q={q}").status_code == 200


def test_buttons_with_a_malformed_port_are_refused():
    from miniapp.admin_web.crm import valid_button_url

    assert not valid_button_url("https://x.example:bad") and valid_button_url("https://x.example:8443/p")
    assert not valid_button_url("https://..") and not valid_button_url("https://x..example")
    assert render_ok("<pre><code>x</code></pre>")


def render_ok(body: str) -> bool:
    from miniapp.admin_web.crm import render_broadcast
    try:
        render_broadcast(body, "A")
        return True
    except ValueError:
        return False


def test_a_body_that_is_only_the_name_is_refused_and_never_blocks_a_queue(env):
    import sqlite3 as sq
    from miniapp.admin_web.app import create_admin_app

    config, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    assert post(client, "/api/admin/broadcasts", csrf, {"name": "n", "body": "<b>{имя}</b>"}).json()["error"] == "invalid_body"
    post(client, f"/api/admin/users/{U1}/tags", csrf, {"tag": "N"})
    bid = post(client, "/api/admin/broadcasts", csrf, {"name": "n", "body": "ok", "rules": {"tag_has": "N"}}).json()["id"]
    with sq.connect(config.admin_db) as conn:          # an old body: only the placeholder; the recipient has no name
        conn.execute("UPDATE crm_broadcasts SET body = '{имя}', status = 'scheduled', scheduled_at = ? WHERE id = ?",
                     (int(clock()) + 60, bid))
        conn.execute("INSERT INTO crm_broadcast_recipients (broadcast_id, user_id) VALUES (?, ?)", (bid, 999_999_991))
        conn.execute("INSERT INTO crm_broadcast_recipients (broadcast_id, user_id) VALUES (?, ?)", (bid, U1))
    create_admin_app(config, clock=clock)              # must not raise
    with sq.connect(config.data_dir / "bot-outbox.db") as conn:
        users = [r[0] for r in conn.execute("SELECT user_id FROM jobs WHERE batch_id = ?", (f"bc:{bid}",))]
    assert 999_999_991 not in users


def test_a_legacy_scheduled_body_never_stops_the_admin_and_is_sent_as_typed(env):
    import sqlite3 as sq
    from miniapp.admin_web.app import create_admin_app

    config, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    post(client, f"/api/admin/users/{U1}/tags", csrf, {"tag": "L"})
    bid = post(client, "/api/admin/broadcasts", csrf, {"name": "old", "body": "ok", "rules": {"tag_has": "L"}}).json()["id"]
    with sq.connect(config.admin_db) as conn:          # saved before 17.11: not valid markup
        conn.execute("UPDATE crm_broadcasts SET body = ?, status = 'scheduled', scheduled_at = ? WHERE id = ?",
                     ("1 < 2 <b> {имя}", int(clock()) + 60, bid))
        conn.execute("INSERT INTO crm_broadcast_recipients (broadcast_id, user_id) VALUES (?, ?)", (bid, U1))
    create_admin_app(config, clock=clock)              # startup queues it: must not raise
    with sq.connect(config.data_dir / "bot-outbox.db") as conn:
        text, mode = conn.execute("SELECT text, parse_mode FROM jobs WHERE batch_id = ?", (f"bc:{bid}",)).fetchone()
    assert mode is None and text.startswith("1 < 2 <b> ") and "{имя}" not in text   # plain text, exactly as typed

    from miniapp.admin_web.crm import MAX_TEXT, html_fits, render_plain
    long_body = "<b>" + "&" * 1000                     # valid once, 5x longer escaped: plain, never a failed queue
    assert not html_fits(long_body) and len(render_plain(long_body, "N")) <= MAX_TEXT
    assert len(render_plain("{имя}" * 1000, "x" * 64)) == MAX_TEXT


def test_a_cancel_after_delivery_is_refused_without_a_list_refresh(env):
    import asyncio
    from bot_outbox import BotOutbox, Outcome

    config, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    post(client, f"/api/admin/users/{U1}/tags", csrf, {"tag": "C"})
    bid = post(client, "/api/admin/broadcasts", csrf, {"name": "c", "body": "t", "rules": {"tag_has": "C"}}).json()["id"]
    at = int(clock()) + 60
    post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**dangerous(secrets_, clock), **reviewed(client, bid), "at": at})

    async def send(job):
        return Outcome("sent", message_id=1)
    asyncio.run(BotOutbox(config.data_dir / "bot-outbox.db").run_once(send, lambda: at))
    clock.t = at + 1
    r = post(client, f"/api/admin/broadcasts/{bid}/cancel", csrf, {**dangerous(secrets_, clock), "idempotencyKey": "c-after-1"})
    assert r.json()["error"] == "not_cancellable"


def test_startup_cancels_queued_broadcast_jobs_of_blocked_users(env):
    import sqlite3 as sq
    from miniapp.admin_web.app import create_admin_app

    config, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    bid = post(client, "/api/admin/broadcasts", csrf, {"name": "B", "body": "t"}).json()["id"]
    post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**dangerous(secrets_, clock), **reviewed(client, bid), "at": int(clock()) + 3600})
    with sq.connect(config.admin_db) as conn:          # the block committed, then a crash before the queue was touched
        conn.execute("INSERT INTO crm_blocks (user_id, reason, created_at, admin_id) VALUES (?, 'x', 1, 1)", (U1,))
    create_admin_app(config, clock=clock)
    with sq.connect(config.data_dir / "bot-outbox.db") as conn:
        assert conn.execute("SELECT status FROM jobs WHERE idempotency_key = ?", (f"bc:{bid}:{U1}",)).fetchone() == ("cancelled",)


def test_a_due_broadcast_with_no_recipients_settles_as_sent(env):
    _, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    bid = post(client, "/api/admin/broadcasts", csrf, {"name": "e", "body": "t", "rules": {"tag_has": "nobody"}}).json()["id"]
    at = int(clock()) + 60
    post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**dangerous(secrets_, clock), **reviewed(client, bid), "at": at})
    clock.t = at + 301
    assert next(b for b in client.get("/api/admin/broadcasts").json()["items"] if b["id"] == bid)["status"] == "sent"


def test_the_broadcast_confirmation_points_out_recent_tag_changes(env):
    """A tag-based audience is whatever the tags say at confirmation: recent changes are shown (CRM B1 r4 P2)."""
    _, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    ask = {"rules": {}, "forBroadcast": True, "withTagChanges": True}
    assert "recentTagChanges" not in post(client, "/api/admin/audience/preview", csrf,
                                          {"rules": {}, "forBroadcast": True}).json()  # previews while editing
    before = post(client, "/api/admin/audience/preview", csrf, ask).json()
    assert before["recentTagChanges"]["recipients"] == 0
    post(client, f"/api/admin/users/{U1}/tags", csrf, {"tag": "VIP"})
    after = post(client, "/api/admin/audience/preview", csrf, ask).json()
    assert after["recentTagChanges"]["recipients"] == 1 and after["recentTagChanges"]["by"] and \
        after["recentTagChanges"]["hours"] == 24
    vip = post(client, "/api/admin/audience/preview", csrf, {**ask, "rules": {"tag_not": "VIP"}}).json()
    assert vip["recentTagChanges"]["outside"] == 1         # U1 is out of this audience but was just retagged
    plain = post(client, "/api/admin/audience/preview", csrf, {"rules": {}}).json()
    assert "recentTagChanges" not in plain
