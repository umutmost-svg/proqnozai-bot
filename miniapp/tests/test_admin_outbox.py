# ruff: noqa: F811 — pytest fixtures imported from test_admin_web are used as parameters
"""Messages and broadcasts go to the existing bot through bot-outbox.db; the admin never calls Telegram."""
from __future__ import annotations

import sqlite3

from miniapp.admin_web.app import create_admin_app
from miniapp.tests.test_admin_crm import U1, dangerous
from miniapp.tests.test_admin_web import digest_of, reviewed, fresh, env, login, post  # noqa: F401 — env is a fixture


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
    r = post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**dangerous(secrets_, clock), **reviewed(client, bid), "at": at}).json()
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
    schedule = {**dangerous(secrets_, clock), "at": at, **reviewed(client, bid)}
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


def test_the_user_card_shows_the_bots_outcome(env):
    config, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    msg = post(client, f"/api/admin/users/{U1}/messages", csrf, {"body": "Статус", "idempotencyKey": "msg-key-st"}).json()
    with sqlite3.connect(config.outbox_db) as conn:
        conn.execute("UPDATE jobs SET status = 'sent' WHERE idempotency_key = ?", (f"msg:{msg['id']}",))
    card = client.get(f"/api/admin/users/{U1}").json()
    assert next(m for m in card["messages"] if m["id"] == msg["id"])["status"] == "sent"


def test_a_cancel_committed_between_the_status_check_and_the_enqueue_still_wins(env, monkeypatch):
    """Cancel vs enqueue across admin.db and bot-outbox.db: the jobs inserted after a concurrent cancel are
    cancelled right after the insert (the status is read again), so none stays queued."""
    from miniapp.admin_web.outbox import Outbox
    config, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    bid = post(client, "/api/admin/broadcasts", csrf, {"name": "Гонка", "body": "Текст", "rules": {"lang_in": ["ru"]}}).json()["id"]
    original = Outbox.enqueue

    def cancel_first(self, *args, **kwargs):  # another process cancels just before our insert lands
        with sqlite3.connect(config.admin_db) as conn:
            conn.execute("UPDATE crm_broadcasts SET status = 'cancelled' WHERE id = ?", (bid,))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Outbox, "enqueue", cancel_first)
    post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**dangerous(secrets_, clock), **reviewed(client, bid), "at": int(clock()) + 3600})
    states = {j[3] for j in jobs(config) if j[5] == f"bc:{bid}"}
    assert states == {"cancelled"}


def _bot_outbox(config):
    """The bot's real claim code (bot_outbox.py at the repo root; the bot package is not importable here)."""
    import importlib.util
    import sys
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "bot_outbox.py"
    spec = importlib.util.spec_from_file_location("bot_outbox_under_test", path)
    module = sys.modules.get("bot_outbox_under_test") or importlib.util.module_from_spec(spec)
    sys.modules["bot_outbox_under_test"] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module.BotOutbox(Path(config.outbox_db))


def test_the_bot_cannot_claim_a_broadcast_job_before_the_status_is_rechecked(env, monkeypatch):
    """A cancel committed just before the insert: a due job inserted after it is invisible to the bot's claim
    until the status re-check, which then cancels it — nothing of a cancelled broadcast is ever sent."""
    from miniapp.admin_web.outbox import Outbox
    config, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    bid = post(client, "/api/admin/broadcasts", csrf, {"name": "Гонка", "body": "Текст", "rules": {"lang_in": ["ru"]}}).json()["id"]
    bot = _bot_outbox(config)
    claimed = []
    original = Outbox.enqueue

    def cancel_then_insert_then_bot_claims(self, *args, **kwargs):
        with sqlite3.connect(config.admin_db) as conn:
            conn.execute("UPDATE crm_broadcasts SET status = 'cancelled' WHERE id = ?", (bid,))
        added = original(self, *args, **kwargs)
        claimed.append(bot.claim(int(clock()) + 7200))  # the bot runs in the gap, the job is already due
        return added

    monkeypatch.setattr(Outbox, "enqueue", cancel_then_insert_then_bot_claims)
    post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**dangerous(secrets_, clock), **reviewed(client, bid), "at": int(clock()) + 3600})
    assert claimed == [None]
    assert {j[3] for j in jobs(config) if j[5] == f"bc:{bid}"} == {"cancelled"}


def test_a_scheduled_broadcast_is_released_and_a_crash_while_held_is_released_at_startup(env, monkeypatch):
    from miniapp.admin_web.outbox import Outbox
    config, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    bid = post(client, "/api/admin/broadcasts", csrf, {"name": "Норма", "body": "Текст", "rules": {"lang_in": ["ru"]}}).json()["id"]
    monkeypatch.setattr(Outbox, "release_batch", lambda self, batch: 0)  # crash before the release
    post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**dangerous(secrets_, clock), **reviewed(client, bid), "at": int(clock()) + 60})
    bot = _bot_outbox(config)
    assert bot.claim(int(clock()) + 120) is None  # still held
    monkeypatch.undo()
    create_admin_app(config, clock=clock)  # restart: the still-scheduled broadcast's jobs are released
    job = bot.claim(int(clock()) + 120)
    assert job is not None and job.text == "Текст"


def test_a_crash_while_held_after_a_cancel_cancels_the_jobs_at_startup(env, monkeypatch):
    from miniapp.admin_web.outbox import Outbox
    config, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    bid = post(client, "/api/admin/broadcasts", csrf, {"name": "Сбой", "body": "Текст", "rules": {"lang_in": ["ru"]}}).json()["id"]
    monkeypatch.setattr(Outbox, "release_batch", lambda self, batch: 0)
    post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**dangerous(secrets_, clock), **reviewed(client, bid), "at": int(clock()) + 60})
    monkeypatch.undo()
    with sqlite3.connect(config.admin_db) as conn:  # cancelled, but its cancel_batch never ran (crash)
        conn.execute("UPDATE crm_broadcasts SET status = 'cancelled' WHERE id = ?", (bid,))
    create_admin_app(config, clock=clock)
    assert {j[3] for j in jobs(config) if j[5] == f"bc:{bid}"} == {"cancelled"}


def test_a_cancel_between_the_recheck_and_the_release_is_never_released(env, monkeypatch):
    """Codex round 2: the status re-check passed, then a cancel ran its cancel_batch before the release —
    the release sees the batch cancelled in bot-outbox.db and the bot can claim nothing."""
    from miniapp.admin_web.outbox import Outbox
    config, clock, client, _, secrets_ = env
    csrf = login(client, clock, secrets_, "admin@x.test")
    bid = post(client, "/api/admin/broadcasts", csrf, {"name": "Окно", "body": "Текст", "rules": {"lang_in": ["ru"]}}).json()["id"]
    original = Outbox.release_batch

    def cancel_lands_first(self, batch):
        with sqlite3.connect(config.admin_db) as conn:
            conn.execute("UPDATE crm_broadcasts SET status = 'cancelled' WHERE id = ?", (bid,))
        self.cancel_batch(batch, int(clock()))
        return original(self, batch)

    monkeypatch.setattr(Outbox, "release_batch", cancel_lands_first)
    post(client, f"/api/admin/broadcasts/{bid}/schedule", csrf, {**dangerous(secrets_, clock), **reviewed(client, bid), "at": int(clock()) + 60})
    assert _bot_outbox(config).claim(int(clock()) + 120) is None
    assert {j[3] for j in jobs(config) if j[5] == f"bc:{bid}"} == {"cancelled"}


def test_a_cancelled_batch_never_gets_new_jobs(tmp_path):
    from miniapp.admin_web.outbox import Outbox
    box = Outbox(tmp_path / "outbox.db")
    box.init_schema()
    box.cancel_batch("bc:9", 1)
    assert box.enqueue([("bc:9:1", 1, "x", [])], batch_id="bc:9", not_before=1, admin_event_id=None, now=1) == 0
    assert box.batch_counts("bc:9") == {}
