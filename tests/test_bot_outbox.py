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


async def test_a_flood_limit_pauses_the_whole_outbox_and_never_uses_up_attempts(tmp_path):
    admin, bot = setup(tmp_path)
    admin.enqueue([("msg:1", 11, "x", []), ("msg:2", 12, "y", [])], batch_id=None, not_before=NOW,
                  admin_event_id=None, now=NOW)
    t = [NOW]
    floods = [4]  # more flood windows than MAX_ATTEMPTS
    sent = []

    async def send(job):
        if floods[0]:
            floods[0] -= 1
            return Outcome("retry", "rate_limited", 5)
        sent.append(job.user_id)
        return Outcome("sent")

    assert await bot.run_once(send, lambda: t[0]) == {"retry": 1}  # the batch stops: msg:2 is not tried
    assert await bot.run_once(send, lambda: t[0] + 4) == {}  # paused for the time Telegram named
    for _ in range(3):
        t[0] += 6
        assert await bot.run_once(send, lambda: t[0]) == {"retry": 1}
    t[0] += 6
    assert await bot.run_once(send, lambda: t[0]) == {"sent": 2}
    assert sorted(sent) == [11, 12] and statuses(tmp_path)["msg:1"] == ("sent", None)


async def test_the_pause_survives_a_restart(tmp_path):
    admin, bot = setup(tmp_path)
    admin.enqueue([("msg:1", 11, "x", []), ("msg:2", 12, "y", [])], batch_id=None, not_before=NOW,
                  admin_event_id=None, now=NOW)

    async def flooded(job):
        return Outcome("retry", "rate_limited", 60)

    async def ok(job):
        return Outcome("sent")

    assert await bot.run_once(flooded, lambda: NOW) == {"retry": 1}
    restarted = BotOutbox(tmp_path / "bot-outbox.db")  # a new process: no pause in memory
    # A job enqueued after the pause began waits for it too.
    admin.enqueue([("msg:3", 13, "z", [])], batch_id=None, not_before=NOW + 10, admin_event_id=None, now=NOW + 10)
    assert await restarted.run_once(ok, lambda: NOW + 30) == {}  # Telegram's wait still holds for every job
    assert await restarted.run_once(ok, lambda: NOW + 60) == {"sent": 3}


async def test_a_pause_by_another_bot_process_stops_this_one_mid_batch_and_is_never_shortened(tmp_path):
    admin, bot = setup(tmp_path)
    other = BotOutbox(tmp_path / "bot-outbox.db")
    admin.enqueue([(f"msg:{i}", 10 + i, "x", []) for i in range(4)], batch_id=None, not_before=NOW,
                  admin_event_id=None, now=NOW)
    sent = []

    async def send(job):
        sent.append(job.user_id)
        if len(sent) == 1:
            other._pause(NOW + 100)  # the other process meets a flood limit meanwhile
        return Outcome("sent")

    assert await bot.run_once(send, lambda: NOW) == {"sent": 1}
    other._pause(NOW + 10)  # an older, shorter deadline never replaces the longer one
    assert BotOutbox(tmp_path / "bot-outbox.db").paused_until == NOW + 100


async def test_an_unusable_pause_file_keeps_the_pause_in_the_queue(tmp_path):
    admin, bot = setup(tmp_path)
    admin.enqueue([("msg:1", 11, "x", []), ("msg:2", 12, "y", [])], batch_id=None, not_before=NOW,
                  admin_event_id=None, now=NOW)
    (tmp_path / "bot-outbox.db.pause.lock").mkdir()  # the lock path cannot be opened as a file

    async def flooded(job):
        return Outcome("retry", "rate_limited", 60)

    async def ok(job):
        return Outcome("sent")

    assert await bot.run_once(flooded, lambda: NOW) == {"retry": 1}
    restarted = BotOutbox(tmp_path / "bot-outbox.db")
    assert restarted.paused_until == 0  # nothing on disk...
    assert await restarted.run_once(ok, lambda: NOW + 30) == {}  # ...yet every queued job still waits
    assert await restarted.run_once(ok, lambda: NOW + 60) == {"sent": 2}
    assert not list(tmp_path.glob("*.tmp"))


async def test_a_network_error_costs_one_job_never_the_batch(tmp_path):
    admin, bot = setup(tmp_path)
    admin.enqueue([(f"msg:{i}", 10 + i, "x", []) for i in range(5)], batch_id=None, not_before=NOW,
                  admin_event_id=None, now=NOW)
    t = [NOW]
    down = [True]

    async def send(job):
        return Outcome("failed", "unknown_outcome") if down[0] else Outcome("sent")

    assert await bot.run_once(send, lambda: t[0]) == {"failed": 1}  # at most once: that one is not re-sent
    assert await bot.run_once(send, lambda: t[0] + bot_outbox.NETWORK_PAUSE_S - 1) == {}
    t[0] += bot_outbox.NETWORK_PAUSE_S
    assert await bot.run_once(send, lambda: t[0]) == {"failed": 1}  # still down: the pause doubles
    assert bot.paused_until == t[0] + 2 * bot_outbox.NETWORK_PAUSE_S
    down[0] = False
    t[0] += 2 * bot_outbox.NETWORK_PAUSE_S
    assert await bot.run_once(send, lambda: t[0]) == {"sent": 3}
    assert bot._network_pause == 0


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
    assert admin.cancel_batch("bc:1", 1) == 2
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
    admin.cancel_batch("none", 1)
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


async def test_a_timeout_is_never_retried(tmp_path):
    from telegram.error import NetworkError, TimedOut

    admin, bot = setup(tmp_path)
    admin.enqueue([("msg:1", 11, "x", []), ("msg:2", 12, "x", [])], batch_id=None, not_before=NOW,
                  admin_event_id=None, now=NOW)

    async def lost(job):
        raise TimedOut() if job.user_id == 11 else NetworkError("connection reset")

    assert await bot.run_once(lost, lambda: NOW) == {"failed": 1}  # the outage pauses the outbox
    later = NOW + bot_outbox.NETWORK_PAUSE_S
    assert await bot.run_once(lost, lambda: later) == {"failed": 1}
    assert statuses(tmp_path) == {"msg:1": ("failed", "unknown_outcome"), "msg:2": ("failed", "unknown_outcome")}


def test_a_retry_after_the_cancel_is_never_sent(tmp_path):
    """Codex round 3: a job being sent when its batch is cancelled, then Telegram answers RetryAfter — it ends
    cancelled, and the bot never claims it again."""
    admin, bot = setup(tmp_path)
    admin.enqueue([("bc:5:11", 11, "x", [])], batch_id="bc:5", not_before=NOW, admin_event_id=None, now=NOW)
    job = bot.claim(NOW)
    assert job is not None
    assert admin.cancel_batch("bc:5", NOW + 1) == 0  # in flight: not cancellable as queued
    assert bot.finish(job, Outcome("retry", "rate_limited", 5), NOW + 2) is True
    assert statuses(tmp_path)["bc:5:11"] == ("cancelled", "rate_limited")
    assert bot.claim(NOW + 3600) is None


def test_a_queued_job_of_a_marked_batch_is_never_claimed(tmp_path):
    admin, bot = setup(tmp_path)
    admin.enqueue([("bc:6:11", 11, "x", [])], batch_id="bc:6", not_before=NOW, admin_event_id=None, now=NOW)
    with sqlite3.connect(tmp_path / "bot-outbox.db") as conn:  # marked, its queued rows not yet updated
        conn.execute("INSERT INTO cancelled_batches VALUES ('bc:6', ?)", (NOW,))
    assert bot.claim(NOW + 10) is None


# --- auto-delete: the bot deletes a broadcast's messages after the admin's chosen time (within Telegram's 48 h) ---

async def test_sent_messages_are_deleted_once_after_their_time(tmp_path):
    admin, bot = setup(tmp_path)
    admin.enqueue([("bc:1:21", 21, "a", []), ("bc:1:22", 22, "b", []), ("msg:9", 23, "c", [])], batch_id=None,
                  not_before=NOW, admin_event_id=None, now=NOW, delete_after_s=3600)
    admin.enqueue([("msg:10", 24, "keep", [])], batch_id=None, not_before=NOW, admin_event_id=None, now=NOW)
    ids = iter(range(500, 600))

    async def send(job):
        return Outcome("sent", message_id=next(ids))
    await bot.run_once(send, lambda: NOW)
    deleted = []

    async def delete(chat, message):
        if chat == 22:
            raise RuntimeError("chat gone")
        deleted.append((chat, message))
    assert await bot.run_deletes(delete, lambda: NOW + 1800) == {}           # not yet
    assert await bot.run_deletes(delete, lambda: NOW + 3600) == {"deleted": 2, "failed": 1}
    assert sorted(c for c, _ in deleted) == [21, 23]                          # 24 has no auto-delete
    assert await bot.run_deletes(delete, lambda: NOW + 7200) == {}           # each one tried once


async def test_a_message_older_than_telegrams_limit_is_never_tried(tmp_path):
    admin, bot = setup(tmp_path)
    admin.enqueue([("bc:2:31", 31, "a", [])], batch_id=None, not_before=NOW, admin_event_id=None, now=NOW,
                  delete_after_s=3600)

    async def send(job):
        return Outcome("sent", message_id=7)
    await bot.run_once(send, lambda: NOW)
    tried = []

    async def delete(chat, message):
        tried.append(chat)
    assert await bot.run_deletes(delete, lambda: NOW + 49 * 3600) == {"too_old": 1} and tried == []


def test_the_new_columns_are_added_to_an_existing_file(tmp_path):
    path = tmp_path / "bot-outbox.db"
    with sqlite3.connect(path) as conn:                                       # a file from before the feature
        conn.executescript(bot_outbox.SCHEMA.replace(",\n    delete_after_s  INTEGER,\n    message_id      INTEGER,\n"
                                                     "    delete_status   TEXT,\n    deleted_at      INTEGER", ""))
    admin_outbox.Outbox(path).init_schema()
    with sqlite3.connect(path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {"delete_after_s", "message_id", "delete_status", "deleted_at"} <= cols



async def test_a_claimed_deletion_is_never_tried_again_and_old_jobs_are_marked(tmp_path):
    admin, bot = setup(tmp_path)
    admin.enqueue([("bc:3:41", 41, "a", []), ("bc:3:42", 42, "b", [])], batch_id=None, not_before=NOW,
                  admin_event_id=None, now=NOW, delete_after_s=60)

    async def send(job):
        return Outcome("sent", message_id=None if job.user_id == 42 else 9)  # 42: sent by an older bot
    await bot.run_once(send, lambda: NOW)
    with sqlite3.connect(tmp_path / "bot-outbox.db") as conn:              # a crash right after claiming 41
        conn.execute("UPDATE jobs SET delete_status = 'deleting' WHERE user_id = 41")
    tried = []

    async def delete(chat, message):
        tried.append(chat)
    await bot.run_deletes(delete, lambda: NOW + 120)
    with sqlite3.connect(tmp_path / "bot-outbox.db") as conn:
        states = dict(conn.execute("SELECT user_id, delete_status FROM jobs"))
    assert tried == [] and states == {41: "deleting", 42: "no_message_id"}


def test_both_sides_may_add_the_columns_at_once(tmp_path):
    path = tmp_path / "bot-outbox.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(bot_outbox.SCHEMA.replace(",\n    delete_after_s  INTEGER,\n    message_id      INTEGER,\n"
                                                     "    delete_status   TEXT,\n    deleted_at      INTEGER", ""))
    with sqlite3.connect(path, isolation_level=None) as a, sqlite3.connect(path, isolation_level=None) as b:
        bot_outbox.add_columns(a)
        admin_outbox.add_columns(b)                                           # already there: no error
    assert bot_outbox.ADDED_COLUMNS == admin_outbox.ADDED_COLUMNS


async def test_a_flood_limit_or_outage_while_deleting_is_retried_after_the_pause(tmp_path):
    # CRM audit P1: a transient error used to mark the delete failed for good, leaving the promo in the chat.
    from telegram.error import NetworkError, RetryAfter

    admin, bot = setup(tmp_path)
    admin.enqueue([("bc:3:41", 41, "a", []), ("bc:3:42", 42, "b", [])], batch_id=None, not_before=NOW,
                  admin_event_id=None, now=NOW, delete_after_s=3600)

    async def send(job):
        return Outcome("sent", message_id=job.user_id + 100)
    await bot.run_once(send, lambda: NOW)
    errors = [RetryAfter(30), NetworkError("down")]
    deleted = []

    async def delete(chat, message):
        if errors:
            raise errors.pop(0)
        deleted.append(chat)
    t = NOW + 3600
    assert await bot.run_deletes(delete, lambda: t) == {"retry": 1}           # flood: stop, pause 30 s
    assert await bot.run_deletes(delete, lambda: t + 10) == {}                 # paused: nothing tried
    assert await bot.run_deletes(delete, lambda: t + 31) == {"retry": 1}       # network: stop, pause again
    assert await bot.run_deletes(delete, lambda: t + 31 + bot_outbox.NETWORK_PAUSE_S) == {"deleted": 2}
    assert sorted(deleted) == [41, 42]


async def test_a_delete_left_by_a_crash_is_tried_again(tmp_path):
    admin, bot = setup(tmp_path)
    admin.enqueue([("bc:4:51", 51, "a", [])], batch_id=None, not_before=NOW, admin_event_id=None, now=NOW,
                  delete_after_s=3600)

    async def send(job):
        return Outcome("sent", message_id=9)
    await bot.run_once(send, lambda: NOW)
    with sqlite3.connect(tmp_path / "bot-outbox.db") as conn:              # claimed, then the process died
        conn.execute("UPDATE jobs SET delete_status = 'deleting', deleted_at = ?", (NOW + 3600,))
    done = []

    async def delete(chat, message):
        done.append(chat)
    assert await bot.run_deletes(delete, lambda: NOW + 3700) == {}
    assert await bot.run_deletes(delete, lambda: NOW + 3600 + bot_outbox.STUCK_S) == {"deleted": 1} and done == [51]


async def test_a_broadcast_is_claimed_with_its_parse_mode(tmp_path):
    admin, bot = setup(tmp_path)
    admin.enqueue([("bc:5:61", 61, "<b>Hi</b>", [])], batch_id="bc:5", not_before=NOW, admin_event_id=None, now=NOW,
                  parse_mode="HTML")
    admin.enqueue([("msg:1", 62, "<plain>", [])], batch_id=None, not_before=NOW, admin_event_id=None, now=NOW)
    modes = {}

    async def send(job):
        modes[job.user_id] = job.parse_mode
        return Outcome("sent", message_id=1)
    await bot.run_once(send, lambda: NOW)
    assert modes == {61: "HTML", 62: None}
