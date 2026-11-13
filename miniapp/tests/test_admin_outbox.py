# ruff: noqa: F811 — pytest fixtures imported from test_admin_web are used as parameters
"""Messages and broadcasts go to the existing bot through bot-outbox.db; the admin never calls Telegram."""
from __future__ import annotations

import sqlite3

from miniapp.admin_web.app import create_admin_app
from miniapp.tests.test_admin_crm import U1, dangerous
from miniapp.tests.test_admin_web import env, login, post  # noqa: F401 — env is a fixture


def jobs(config) -> list[tuple]:
    with sqlite3.connect(config.outbox_db) as conn:
        return conn.execute("SELECT idempotency_key, user_id, text, status, not_before, batch_id FROM jobs "
                            "ORDER BY idempotency_key").fetchall()


def test_a_message_becomes_one_bot_job_and_the_journal_has_no_text(env):
    config, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    msg = post(client, f"/api/admin/users/{U1}/messages", csrf, {"body": "  Привет от поддержки  ",
                                                                      "idempotencyKey": "msg-key-0001"}).json()
    assert jobs(config) == [(f"msg:{msg['id']}", U1, "Привет от поддержки", "queued", int(clock()), None)]
    kinds = [e["result"] for e in accounts.events(action="user.message")]
    assert sorted(kinds) == ["completed", "requested"]
    assert "Привет" not in str(accounts.events())


def test_a_crash_between_the_steps_is_finished_at_startup(env, monkeypatch):
    config, clock, client, accounts, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    from miniapp.admin_web.outbox import Outbox

    def crash(*a, **k):
        raise RuntimeError("crash before the job was queued")
    monkeypatch.setattr(Outbox, "enqueue", crash)
    assert post(client, f"/api/admin/users/{U1}/messages", csrf, {"body": "Текст", "idempotencyKey": "msg-key-0001"}).status_code == 500
    monkeypatch.undo()
    assert jobs(config) == []
    create_admin_app(config, clock=clock)  # restart
    assert [j[2] for j in jobs(config)] == ["Текст"]
    create_admin_app(config, clock=clock)  # a second restart adds nothing
    assert len(jobs(config)) == 1 and sorted(e["result"] for e in accounts.events(action="user.message")) == [
        "completed", "requested"]


def test_broadcast_schedule_queues_one_job_per_recipient_and_cancel_stops_them(env):
    config, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    bid = post(client, "/api/admin/broadcasts", csrf, {"name": "Тур", "body": "Новый тур", "rules": {"lang_in": ["ru"]},
                                                      "buttons": [{"text": "Открыть", "url": "miniapp"}]}).json()["id"]
    at = int(clock()) + 3600
    r = post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**dangerous(secrets_, clock), "at": at}).json()
    queued = jobs(config)
    assert len(queued) == r["after"]["recipients"] > 0
    assert {(j[3], j[4], j[5]) for j in queued} == {("queued", at, f"bc:{bid}")}
    item = next(b for b in client.get("/api/admin/broadcasts").json()["items"] if b["id"] == bid)
    assert item["delivery"] == {"queued": len(queued)}
    clock.t += 30
    cancel = post(client, f"/api/admin/broadcasts/{bid}/cancel", csrf, dangerous(secrets_, clock, key="cancel-1")).json()
    assert cancel["cancelledJobs"] == len(queued) and {j[3] for j in jobs(config)} == {"cancelled"}


def test_a_retried_message_request_is_sent_once(env):
    config, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    body = {"body": "Один раз", "idempotencyKey": "msg-key-retry"}
    first = post(client, f"/api/admin/users/{U1}/messages", csrf, body).json()
    again = post(client, f"/api/admin/users/{U1}/messages", csrf, body).json()
    assert first["id"] == again["id"] and len(jobs(config)) == 1
    other = post(client, f"/api/admin/users/{U1}/messages", csrf, {**body, "body": "Другой текст"})
    assert other.status_code == 409 and other.json()["error"] == "idempotency_key_reused"
    assert post(client, f"/api/admin/users/{U1}/messages", csrf, {"body": "x"}).json()["error"] == "idempotency_key_required"


def test_a_replayed_schedule_after_cancel_queues_nothing_new(env):
    config, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    bid = post(client, "/api/admin/broadcasts", csrf, {"name": "Т", "body": "Т", "rules": {"lang_in": ["ru"]}}).json()["id"]
    at = int(clock()) + 3600
    schedule = {**dangerous(secrets_, clock), "at": at}
    post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, schedule)
    frozen = len(jobs(config))
    clock.t += 30
    post(client, f"/api/admin/broadcasts/{bid}/cancel", csrf, dangerous(secrets_, clock, key="cancel-1"))
    # a new user who would now match the rules, then the schedule request is replayed
    from miniapp.store import Store
    from miniapp.telegram_auth import TelegramUser
    Store(config.miniapp_db).upsert_user(TelegramUser(id=995000001, first_name="N", last_name=None, username=None,
                                                      language_code="ru"), int(clock()))
    assert post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, schedule).json()["replayed"] is True
    assert len(jobs(config)) == frozen and {j[3] for j in jobs(config)} == {"cancelled"}
