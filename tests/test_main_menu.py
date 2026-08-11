"""Main reply keyboard: History/Profile were dropped from the menu, but their
labels must keep routing — keyboards already sitting in users' chats go on
sending them until the menu is re-rendered. Offline."""
import types

import handlers.utils as hu
import handlers.forecast as fc
from translations import T


def _labels(uid):
    return [b.text for row in hu.main_menu(uid).keyboard for b in row]


def test_history_and_profile_are_not_in_the_keyboard(temp_db):
    uid = 971001
    temp_db.db_ensure(uid, "u", "ru")
    labels = _labels(uid)
    assert T["ru"]["menu_history"] not in labels
    assert T["ru"]["menu_profile"] not in labels


def test_forecast_support_and_lang_remain(temp_db):
    uid = 971002
    temp_db.db_ensure(uid, "u", "ru")
    labels = _labels(uid)
    assert T["ru"]["menu_forecast"] in labels
    assert T["ru"]["menu_support"] in labels
    assert hu.LANG_BTN in labels


def test_history_and_profile_are_reachable_as_commands():
    """The menu buttons are gone, so the commands are the only entry points —
    /history in particular is where the ✅/❌ feedback buttons live."""
    from telegram.ext import CommandHandler
    from handlers import register_handlers

    class _App:
        def __init__(self):
            self.handlers = []

        def add_handler(self, h, group=0):
            self.handlers.append(h)

    app = _App()
    register_handlers(app)
    commands = set()
    for h in app.handlers:
        if isinstance(h, CommandHandler):
            commands |= set(h.commands)
    assert {"history", "profile"} <= commands


class _Msg:
    def __init__(self, text):
        self.text = text; self.caption = None; self.photo = None
        self.replies = []
        self.chat = types.SimpleNamespace(send_action=self._noop)

    async def reply_text(self, text, **kw):
        self.replies.append(text)

    async def _noop(self, *a, **k):
        pass


def _update(uid, text):
    return types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=uid, username="u", full_name="U",
                                             first_name="U", language_code="ru"),
        message=_Msg(text))


async def test_stale_keyboard_history_label_still_routes(temp_db):
    uid = 971003
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    upd = _update(uid, T["ru"]["menu_history"])
    await fc.handle_msg(upd, types.SimpleNamespace(user_data={}, bot=None))
    assert upd.message.replies


async def test_stale_keyboard_profile_label_still_routes(temp_db):
    uid = 971004
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    upd = _update(uid, T["ru"]["menu_profile"])
    await fc.handle_msg(upd, types.SimpleNamespace(user_data={}, bot=None))
    assert upd.message.replies
