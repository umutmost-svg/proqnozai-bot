"""bot-crm.db (bot -> admin): referral starts on the first /start only, behind flags; schema shared with the admin."""
from __future__ import annotations

import asyncio
import re
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
