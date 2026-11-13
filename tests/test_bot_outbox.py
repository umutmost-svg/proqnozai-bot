"""admin -> bot outbox: the bot claims, sends and records outcomes (offline: a fake sender, a temporary file)."""
from __future__ import annotations

import sqlite3

import bot_outbox
from bot_outbox import BotOutbox, Outcome
from miniapp.admin_web import outbox as admin_outbox

NOW = 1_800_000_000


def setup(tmp_path):
    admin = admin_outbox.Outbox(tmp_path / "bot-outbox.db")
    admin.init_schema()
    bot = BotOutbox(tmp_path / "bot-outbox.db")
    bot.init_schema()
    return admin, bot


def statuses(tmp_path) -> dict[str, tuple]:
    with sqlite3.connect(tmp_path / "bot-outbox.db") as conn:
        return {k: (s, c) for k, s, c in conn.execute("SELECT idempotency_key, status, last_error_code FROM jobs")}


def test_schemas_of_both_sides_are_identical():
    assert bot_outbox.SCHEMA == admin_outbox.SCHEMA


async def test_jobs_are_sent_once_with_outcomes_recorded(tmp_path):
    admin, bot = setup(tmp_path)
    jobs = [("msg:1", 11, "Привет", []), ("msg:2", 12, "Hi", [{"text": "Open", "url": "miniapp"}]), ("msg:3", 13, "x", [])]
    assert admin.enqueue(jobs, batch_id=None, not_before=NOW, admin_event_id=None, now=NOW) == 3
    assert admin.enqueue(jobs[:1], batch_id=None, not_before=NOW, admin_event_id=None, now=NOW) == 0  # idempotent
    sent = []

    async def send(job):
        sent.append((job.user_id, job.text, job.buttons))
        if job.user_id == 12:
            return Outcome("failed", "blocked_by_user")
        return Outcome("sent")

    counts = await bot.run_once(send, lambda: NOW, is_banned=lambda uid: uid == 13)
    assert counts == {"sent": 1, "failed": 2}
    assert sent == [(11, "Привет", []), (12, "Hi", [{"text": "Open", "url": "miniapp"}])]  # 13 is banned in the bot
    assert statuses(tmp_path) == {"msg:1": ("sent", None), "msg:2": ("failed", "blocked_by_user"),
                                  "msg:3": ("failed", "bot_ban")}
    assert await bot.run_once(send, lambda: NOW) == {}  # nothing is sent twice
    assert admin.status_by_keys(["msg:1"])["msg:1"]["status"] == "sent"


async def test_retry_after_a_rate_limit_then_give_up(tmp_path):
    admin, bot = setup(tmp_path)
    admin.enqueue([("msg:1", 11, "x", [])], batch_id=None, not_before=NOW, admin_event_id=None, now=NOW)
    t = [NOW]

    async def limited(job):
        return Outcome("retry", "rate_limited", 5)

    assert await bot.run_once(limited, lambda: t[0]) == {"retry": 1}
    assert await bot.run_once(limited, lambda: t[0]) == {}  # waits for retry_at
    for _ in range(2):
        t[0] += 6
        await bot.run_once(limited, lambda: t[0])
    assert statuses(tmp_path)["msg:1"] == ("failed", "rate_limited")  # MAX_ATTEMPTS reached


async def test_scheduled_cancelled_expired_and_stuck_jobs(tmp_path):
    admin, bot = setup(tmp_path)
    admin.enqueue([("bc:1:11", 11, "x", []), ("bc:1:12", 12, "x", [])], batch_id="bc:1", not_before=NOW + 3600,
                  admin_event_id=None, now=NOW)
    admin.enqueue([("bc:2:11", 11, "y", [])], batch_id="bc:2", not_before=NOW, admin_event_id=None, now=NOW)

    async def ok(job):
        return Outcome("sent")

    stale = bot.claim(NOW)
    assert stale and statuses(tmp_path)["bc:2:11"][0] == "sending"  # claimed, then the bot "dies" mid-send
    assert await bot.run_once(ok, lambda: NOW + 60) == {}  # bc:1 not due yet, bc:2 still in flight
    assert await bot.run_once(ok, lambda: NOW + bot_outbox.STUCK_S + 1) == {}
    assert statuses(tmp_path)["bc:2:11"] == ("failed", "interrupted")  # outcome unknown: never sent twice
    assert bot.finish(stale, Outcome("sent"), NOW + 700) is False  # a late result of the old claim is fenced off
    assert admin.cancel_batch("bc:1") == 2
    assert await bot.run_once(ok, lambda: NOW + 3600) == {}
    admin.enqueue([("msg:9", 11, "late", [])], batch_id=None, not_before=NOW, admin_event_id=None, now=NOW)
    assert await bot.run_once(ok, lambda: NOW + admin_outbox.JOB_TTL_S + 1) == {}
    assert statuses(tmp_path)["msg:9"][0] == "expired"


async def test_exceptions_are_classified_by_type_only(tmp_path):
    from telegram.error import Forbidden, RetryAfter

    admin, bot = setup(tmp_path)
    admin.enqueue([("msg:1", 11, "x", []), ("msg:2", 12, "x", [])], batch_id=None, not_before=NOW,
                  admin_event_id=None, now=NOW)

    async def boom(job):
        raise Forbidden("bot was blocked by the user") if job.user_id == 11 else RetryAfter(7)

    assert await bot.run_once(boom, lambda: NOW) == {"failed": 1, "retry": 1}
    assert statuses(tmp_path) == {"msg:1": ("failed", "blocked_by_user"), "msg:2": ("queued", "rate_limited")}


def test_retention_clears_texts_then_rows(tmp_path):
    admin, bot = setup(tmp_path)
    admin.enqueue([("msg:1", 11, "secret", [])], batch_id=None, not_before=NOW, admin_event_id=None, now=NOW)
    admin.cancel_batch("none")
    with sqlite3.connect(tmp_path / "bot-outbox.db") as conn:
        conn.execute("UPDATE jobs SET status = 'sent'")
    bot.purge(NOW + 31 * 86400)
    with sqlite3.connect(tmp_path / "bot-outbox.db") as conn:
        assert conn.execute("SELECT text FROM jobs").fetchone()[0] == ""
    bot.purge(NOW + 401 * 86400)
    assert statuses(tmp_path) == {}


async def test_an_expired_job_is_never_claimed(tmp_path):
    admin, bot = setup(tmp_path)
    admin.enqueue([("msg:1", 11, "x", [])], batch_id=None, not_before=NOW, admin_event_id=None, now=NOW)

    async def ok(job):
        return Outcome("sent")
    assert bot.claim(NOW + admin_outbox.JOB_TTL_S) is None
    assert statuses(tmp_path)["msg:1"][0] == "expired"
    assert await bot.run_once(ok, lambda: NOW + admin_outbox.JOB_TTL_S + 10) == {}
