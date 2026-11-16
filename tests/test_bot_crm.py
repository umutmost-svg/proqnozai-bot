"""bot-crm.db (bot -> admin): referral starts on the first /start only, behind flags; schema shared with the admin."""
from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot_crm
from bot_crm import BotCrm, clamp_retention, referral_code


def test_referral_code_accepts_only_the_ref_prefix_and_safe_codes():
    assert referral_code("ref_insta01") == "insta01"
    assert referral_code("ref_AB") is None and referral_code("ref_ab") is None  # upper case / too short
    assert referral_code("promo") is None and referral_code(None) is None and referral_code("ref_a-b-c") is None


def test_first_start_wins_and_is_recorded_once(tmp_path):
    crm = BotCrm(tmp_path / "bot-crm.db")
    crm.init_schema()
    assert crm.record_start(7, "insta01", 100) is True
    assert crm.record_start(7, "other", 200) is False


def test_schema_matches_the_admin_side():
    admin = (Path(__file__).resolve().parents[1] / "miniapp" / "admin_web" / "bot_crm.py").read_text()
    assert re.search(r'SCHEMA = """(.*?)"""', admin, re.S).group(1) == bot_crm.SCHEMA


def test_clamp_retention():
    assert clamp_retention("30") == 30 and clamp_retention("0") == 1 and clamp_retention("9999") == 365
    assert clamp_retention("x") == 90


def _update(uid: int, args: list[str] | None = None):
    message = SimpleNamespace(reply_text=AsyncMock())
    user = SimpleNamespace(id=uid, username="u", first_name="U", language_code="ru")
    return SimpleNamespace(effective_user=user, message=message), SimpleNamespace(args=args or [])


@pytest.mark.parametrize("enabled", [False, True])
def test_start_records_a_referral_only_with_the_flag_and_only_for_new_users(temp_db, tmp_path, monkeypatch, enabled):
    from handlers import registration
    monkeypatch.setattr(bot_crm, "_instance", None)
    monkeypatch.setattr(bot_crm, "BOT_CRM_DB", str(tmp_path / "bot-crm.db"))
    monkeypatch.setattr(bot_crm, "BOT_REFERRALS_ENABLED", enabled)
    uid = 424242 + int(enabled)  # the bot DB is shared by the session: a fresh user per case
    update, context = _update(uid, ["ref_insta01"])
    asyncio.run(registration.start(update, context))
    update.message.reply_text.assert_awaited()  # /start answered as before
    again, context2 = _update(uid, ["ref_other"])
    asyncio.run(registration.start(again, context2))
    if not enabled:
        assert not (tmp_path / "bot-crm.db").exists()
        return
    crm = BotCrm(tmp_path / "bot-crm.db")
    with crm._connect() as conn:
        assert conn.execute("SELECT user_id, code FROM referral_starts").fetchall() == [(uid, "insta01")]


def test_a_broken_crm_file_never_breaks_start(temp_db, tmp_path, monkeypatch):
    from handlers import registration
    monkeypatch.setattr(bot_crm, "_instance", None)
    monkeypatch.setattr(bot_crm, "BOT_CRM_DB", str(tmp_path))  # a directory: sqlite cannot open it
    monkeypatch.setattr(bot_crm, "BOT_REFERRALS_ENABLED", True)
    update, context = _update(515151, ["ref_insta01"])
    asyncio.run(registration.start(update, context))
    update.message.reply_text.assert_awaited()


def test_note_start_ignores_known_users_and_other_payloads(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_crm, "_instance", None)
    monkeypatch.setattr(bot_crm, "BOT_CRM_DB", str(tmp_path / "bot-crm.db"))
    monkeypatch.setattr(bot_crm, "BOT_REFERRALS_ENABLED", True)
    bot_crm.note_start(1, ["ref_insta01"], False, 100)   # already known to the bot
    bot_crm.note_start(2, ["promo2024"], True, 100)      # not a referral payload
    assert not (tmp_path / "bot-crm.db").exists()
    bot_crm.note_start(3, ["ref_insta01"], True, 100)
    with BotCrm(tmp_path / "bot-crm.db")._connect() as conn:
        assert conn.execute("SELECT user_id FROM referral_starts").fetchall() == [(3,)]


def _msg_update(uid: int, text: str, chat_type: str = "private", date=None):
    from datetime import datetime, timezone
    message = SimpleNamespace(text=text, reply_text=AsyncMock(), date=date or datetime.now(timezone.utc))
    return SimpleNamespace(message=message, effective_chat=SimpleNamespace(type=chat_type),
                           effective_user=SimpleNamespace(id=uid))


def _stored(path):
    with BotCrm(path)._connect() as conn:
        return conn.execute("SELECT user_id, text FROM inquiries ORDER BY id").fetchall()


def test_inquiry_hook_is_inert_with_the_flag_off(tmp_path, monkeypatch):
    from handlers.inquiries import inquiry_hook
    monkeypatch.setattr(bot_crm, "_instance", None)
    monkeypatch.setattr(bot_crm, "BOT_CRM_DB", str(tmp_path / "bot-crm.db"))
    monkeypatch.setattr(bot_crm, "BOT_INQUIRIES_ENABLED", False)
    update = _msg_update(901, "help")
    asyncio.run(inquiry_hook(update, None))
    update.message.reply_text.assert_not_awaited()
    assert not (tmp_path / "bot-crm.db").exists()


def test_inquiry_hook_sends_the_notice_once_before_storing(temp_db, tmp_path, monkeypatch):
    from handlers.inquiries import inquiry_hook
    from translations import T
    monkeypatch.setattr(bot_crm, "_instance", None)
    monkeypatch.setattr(bot_crm, "BOT_CRM_DB", str(tmp_path / "bot-crm.db"))
    monkeypatch.setattr(bot_crm, "BOT_INQUIRIES_ENABLED", True)
    first = _msg_update(902, "Не работает прогноз")
    asyncio.run(inquiry_hook(first, None))
    notice = first.message.reply_text.await_args.args[0]
    assert notice == T["ru"]["inquiry_notice"].format(days=90)
    second = _msg_update(902, "  ещё  ")
    asyncio.run(inquiry_hook(second, None))
    second.message.reply_text.assert_not_awaited()
    for skipped in (_msg_update(902, T["ru"]["menu_profile"]), _msg_update(902, "/start"),
                    _msg_update(902, "group text", "group"), _msg_update(1, "admin text")):  # ADMIN_ID=1 in tests
        asyncio.run(inquiry_hook(skipped, None))
    assert _stored(tmp_path / "bot-crm.db") == [(902, "Не работает прогноз"), (902, "ещё")]


def test_inquiry_is_not_stored_when_the_notice_fails(temp_db, tmp_path, monkeypatch):
    from handlers.inquiries import inquiry_hook
    monkeypatch.setattr(bot_crm, "_instance", None)
    monkeypatch.setattr(bot_crm, "BOT_CRM_DB", str(tmp_path / "bot-crm.db"))
    monkeypatch.setattr(bot_crm, "BOT_INQUIRIES_ENABLED", True)
    update = _msg_update(903, "hello")
    update.message.reply_text.side_effect = RuntimeError("telegram down")
    asyncio.run(inquiry_hook(update, None))  # never raises
    assert _stored(tmp_path / "bot-crm.db") == []


def test_retention_from_the_admin_setting_purges_on_the_bot_side(tmp_path):
    crm = BotCrm(tmp_path / "bot-crm.db")
    crm.init_schema()
    crm.mark_notice(5, 0, 90)
    crm.record_inquiry(5, "old", 0)
    with crm._connect() as conn:
        conn.execute("INSERT INTO config (key, value) VALUES ('retention_days', '1')")
    crm.record_inquiry(5, "new", 2 * 86400)  # triggers the hourly purge
    assert _stored(tmp_path / "bot-crm.db") == [(5, "new")]


def test_the_hook_is_registered_only_with_the_flag(monkeypatch):
    import handlers
    from telegram.ext import Application
    for flag in (False, True):
        monkeypatch.setattr(bot_crm, "BOT_INQUIRIES_ENABLED", flag)
        app = Application.builder().token("0:offline-test-token").build()
        handlers.register_handlers(app)
        assert (-1 in app.handlers) is flag


def test_messages_are_never_kept_longer_than_the_notice_stated(tmp_path):
    crm = BotCrm(tmp_path / "bot-crm.db")
    crm.init_schema()
    crm.mark_notice(6, 0, 1)            # the user was told 1 day
    crm.record_inquiry(6, "a", 0)
    with crm._connect() as conn:        # the owner later raised the retention to 90 days
        conn.execute("INSERT INTO config (key, value) VALUES ('retention_days', '90')")
    crm.record_inquiry(6, "b", 2 * 86400)
    assert _stored(tmp_path / "bot-crm.db") == [(6, "b")]


def test_the_notice_records_the_days_it_stated(temp_db, tmp_path, monkeypatch):
    from handlers.inquiries import inquiry_hook
    monkeypatch.setattr(bot_crm, "_instance", None)
    monkeypatch.setattr(bot_crm, "BOT_CRM_DB", str(tmp_path / "bot-crm.db"))
    monkeypatch.setattr(bot_crm, "BOT_INQUIRIES_ENABLED", True)
    asyncio.run(inquiry_hook(_msg_update(904, "hi"), None))
    with BotCrm(tmp_path / "bot-crm.db")._connect() as conn:
        assert conn.execute("SELECT days FROM inquiry_notices WHERE user_id = 904").fetchone() == (90,)


def test_the_file_is_never_readable_by_other_local_users(tmp_path):
    import os
    old = os.umask(0o022)
    try:
        BotCrm(tmp_path / "new.db").init_schema()
        loose = tmp_path / "loose.db"
        loose.touch(mode=0o666)
        os.chmod(loose, 0o666)
        BotCrm(loose).init_schema()
    finally:
        os.umask(old)
    assert os.stat(tmp_path / "new.db").st_mode & 0o777 == 0o660  # group write kept despite the umask
    assert os.stat(loose).st_mode & 0o777 == 0o660


def test_a_loose_file_owned_by_the_other_process_fails_closed(tmp_path, monkeypatch):
    import os
    loose = tmp_path / "theirs.db"
    loose.touch()
    os.chmod(loose, 0o666)

    def not_owner(*_a, **_k):
        raise PermissionError("not the owner")

    monkeypatch.setattr(bot_crm.os, "chmod", not_owner)
    with pytest.raises(PermissionError):
        BotCrm(loose).init_schema()
    with sqlite3.connect(loose) as conn:  # nothing was written into it
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone() == (0,)


def test_a_file_loosened_after_start_is_tightened_before_the_next_write(tmp_path):
    import os
    crm = BotCrm(tmp_path / "bot-crm.db")
    crm.init_schema()
    crm.mark_notice(8, 0, 90)
    os.chmod(tmp_path / "bot-crm.db", 0o666)
    crm.record_inquiry(8, "x", 1)
    assert os.stat(tmp_path / "bot-crm.db").st_mode & 0o777 == 0o660


def test_messages_sent_before_activation_are_never_collected(temp_db, tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from handlers import inquiries
    monkeypatch.setattr(bot_crm, "_instance", None)
    monkeypatch.setattr(bot_crm, "BOT_CRM_DB", str(tmp_path / "bot-crm.db"))
    monkeypatch.setattr(bot_crm, "BOT_INQUIRIES_ENABLED", True)
    queued = _msg_update(905, "written while the bot was down", date=datetime.now(timezone.utc) - timedelta(hours=1))
    asyncio.run(inquiries.inquiry_hook(queued, None))
    queued.message.reply_text.assert_not_awaited()  # no notice, nothing stored
    assert not (tmp_path / "bot-crm.db").exists()


def test_inquiries_are_capped_per_user(tmp_path):
    # CRM audit P1: one scripted account must not fill the shared volume.
    crm = BotCrm(tmp_path / "bot-crm.db")
    crm.init_schema()
    crm.mark_notice(7, 0, 90)
    crm.mark_notice(8, 0, 90)
    t = 10_000_000
    for i in range(bot_crm.INQUIRIES_PER_HOUR + 5):
        crm.record_inquiry(7, f"m{i}", t + i)
    crm.record_inquiry(8, "other user", t)
    with sqlite3.connect(tmp_path / "bot-crm.db") as conn:
        per_user = dict(conn.execute("SELECT user_id, COUNT(*) FROM inquiries GROUP BY user_id"))
    assert per_user == {7: bot_crm.INQUIRIES_PER_HOUR, 8: 1}
    crm.record_inquiry(7, "an hour later", t + 3601)
    with sqlite3.connect(tmp_path / "bot-crm.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM inquiries WHERE user_id = 7").fetchone()[0] == bot_crm.INQUIRIES_PER_HOUR + 1
