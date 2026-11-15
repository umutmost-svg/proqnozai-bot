"""Launcher mode (owner 15.11): the bot only opens the Mini App — /start, messages and old inline buttons get one
WebApp button; admin tools and inquiries stay; the first /start still records its referral."""
import asyncio
from types import SimpleNamespace

import pytest


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup})
        return SimpleNamespace(message_id=1)


def update_for(uid: int, lang: str = "ru", chat_type: str = "private"):
    user = SimpleNamespace(id=uid, username="u", language_code=lang)
    return SimpleNamespace(effective_user=user, effective_chat=SimpleNamespace(id=uid, type=chat_type),
                           callback_query=None, message=None)


def web_app_url(markup) -> str:
    button = markup.inline_keyboard[0][0]
    assert button.web_app is not None
    return button.web_app.url


def test_start_answers_with_the_app_button_in_the_users_language(temp_db, monkeypatch):
    from handlers import launcher
    bot = FakeBot()
    asyncio.run(launcher.launcher_start(update_for(987701, "en"), SimpleNamespace(bot=bot, args=[])))
    assert len(bot.sent) == 1
    from translations import T
    assert bot.sent[0]["text"] == T["en"]["launcher_text"]
    assert web_app_url(bot.sent[0]["markup"]).startswith("https://")


def test_the_first_start_still_records_its_referral(temp_db, monkeypatch):
    from handlers import launcher
    seen = []
    monkeypatch.setattr(launcher.bot_crm, "note_start", lambda uid, args, new, now: seen.append((uid, args, new)))
    asyncio.run(launcher.launcher_start(update_for(987702), SimpleNamespace(bot=FakeBot(), args=["ref_abc"])))
    asyncio.run(launcher.launcher_start(update_for(987702), SimpleNamespace(bot=FakeBot(), args=["ref_abc"])))
    assert seen == [(987702, ["ref_abc"], True)]                  # once: only the start that created the user


def test_an_old_button_is_answered_and_points_to_the_app(temp_db):
    from handlers import launcher
    answered = []

    async def answer():
        answered.append(True)
    bot = FakeBot()
    up = update_for(987703)
    up.callback_query = SimpleNamespace(answer=answer, message=SimpleNamespace(chat=SimpleNamespace(id=987703)))
    asyncio.run(launcher.launcher_callback(up, SimpleNamespace(bot=bot)))
    assert answered == [True] and len(bot.sent) == 1


def test_messages_outside_private_chats_are_ignored(temp_db):
    from handlers import launcher
    bot = FakeBot()
    asyncio.run(launcher.launcher_message(update_for(987704, chat_type="group"), SimpleNamespace(bot=bot)))
    assert bot.sent == []


def test_launcher_registration_keeps_admin_tools_and_answers_everything_else(monkeypatch):
    import config
    import handlers
    from telegram.ext import CallbackQueryHandler, CommandHandler

    class App:
        def __init__(self):
            self.handlers = []

        def add_handler(self, h, group=0):
            self.handlers.append((group, h))
    monkeypatch.setattr(config, "BOT_LAUNCHER_MODE", True)
    app = App()
    handlers.register_handlers(app)
    commands = {c for _, h in app.handlers if isinstance(h, CommandHandler) for c in h.commands}
    assert {"start", "admin"} <= commands and "history" not in commands and "matches" not in commands
    patterns = [h.pattern.pattern if h.pattern else None for _, h in app.handlers if isinstance(h, CallbackQueryHandler)]
    assert "^adm_" in patterns and None in patterns              # admin callbacks, then every other button


def test_outbox_buttons_open_the_app_or_a_https_link(temp_db, tmp_path, monkeypatch):
    """The runner turns the admin's buttons into Telegram buttons: 'miniapp' -> WebApp, https -> URL, others dropped."""
    import bot_outbox
    import main
    sent = []

    class Bot:
        async def send_message(self, chat_id, text, reply_markup=None):
            sent.append((chat_id, text, reply_markup))
            return SimpleNamespace(message_id=7)

    db_file = tmp_path / "bot-outbox.db"
    monkeypatch.setattr("config.BOT_OUTBOX_DB", str(db_file))
    sqlite3_mod = __import__("sqlite3")
    sqlite3_mod.connect(db_file).close()                          # the admin creates the file; the bot never does
    box = bot_outbox.BotOutbox(db_file)
    box.init_schema()
    import sqlite3
    import time as t
    import json
    with sqlite3.connect(db_file) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)")]
        row = {"id": "j1", "idempotency_key": "k1", "user_id": 987705, "text": "Hi",
               "buttons_json": json.dumps([{"text": "App", "url": "miniapp"}, {"text": "Site", "url": "https://x.example"},
                                           {"text": "Bad", "url": "http://x"}]),
               "status": "queued", "not_before": 0, "expires_at": int(t.time()) + 3600, "created_at": int(t.time())}
        keys = [k for k in row if k in cols]
        conn.execute(f"INSERT INTO jobs ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})", [row[k] for k in keys])

    class Stop(Exception):
        pass

    async def stop_sleep(_):
        raise Stop()
    monkeypatch.setattr(main.asyncio, "sleep", stop_sleep)
    with pytest.raises(Stop):
        asyncio.run(main._run_outbox(SimpleNamespace(bot=Bot())))
    assert len(sent) == 1
    rows = sent[0][2].inline_keyboard
    assert rows[0][0].web_app is not None and rows[1][0].url == "https://x.example" and len(rows) == 2


def test_shared_sidecars_are_handed_to_the_database_owner(tmp_path, monkeypatch):
    import os
    import sqlite3
    import bot_outbox
    db = tmp_path / "x.db"
    sqlite3.connect(db).close()
    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda p, u, g: calls.append(str(p)))
    real_stat = os.stat

    class St:
        def __init__(self, s, uid):
            self.st_uid, self.st_gid, self.st_mode, self.st_size = uid, uid, s.st_mode, s.st_size
    monkeypatch.setattr(os, "stat", lambda p, *a, **k: St(real_stat(p, *a, **k), 10001 if str(p) == str(db) else 0))
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    bot_outbox.match_owner(db, conn)
    conn.close()
    assert any(c.endswith("-shm") for c in calls)



def test_the_bot_never_creates_the_outbox_file(tmp_path):
    import sqlite3
    import bot_outbox
    with pytest.raises(sqlite3.OperationalError):
        bot_outbox.BotOutbox(tmp_path / "missing.db").init_schema()
    assert not (tmp_path / "missing.db").exists()


def test_launcher_language_follows_telegram_until_the_user_picks_one(temp_db):
    from handlers import launcher
    from translations import T
    bot = FakeBot()
    asyncio.run(launcher.launcher_message(update_for(987711, "uz"), SimpleNamespace(bot=bot)))
    assert bot.sent[0]["text"] == T["uz"]["launcher_text"]



def test_the_bots_own_admin_broadcast_points_to_the_admin_panel(monkeypatch):
    import config
    from handlers import admin
    monkeypatch.setattr(config, "BOT_LAUNCHER_MODE", True)
    edited = []

    async def answer(*a, **k):
        return None

    async def edit(text, reply_markup=None):
        edited.append(text)
    q = SimpleNamespace(from_user=SimpleNamespace(id=admin.ADMIN_ID), data="adm_bcast_confirm_all", answer=answer,
                        edit_message_text=edit)
    ctx = SimpleNamespace(user_data={"adm_act": "broadcast"}, bot=FakeBot())
    asyncio.run(admin.adm_cb(SimpleNamespace(callback_query=q), ctx))
    assert edited and "admin.proqnozai.com" in edited[0] and ctx.bot.sent == [] and "adm_act" not in ctx.user_data
