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
    msg = post(client, f"/api/admin/users/{U1}/messages", csrf, {"body": "  Привет от поддержки  "}).json()
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
    assert post(client, f"/api/admin/users/{U1}/messages", csrf, {"body": "Текст"}).status_code == 500
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
