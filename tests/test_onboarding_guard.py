"""Regression: a registered user must never be frozen by a stale onboarding
step. Before the fix, abandoning the sport picker left reg_step='ob_sports'
while is_registered=1, and the guard silently swallowed every message forever."""
import types

import handlers.forecast as fc
from config import reg_step
from translations import T


class _Msg:
    def __init__(self, text):
        self.text = text
        self.caption = None
        self.photo = None
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


def _ctx():
    return types.SimpleNamespace(user_data={}, bot=None)


async def test_registered_user_with_stale_ob_step_is_not_frozen(temp_db):
    uid = 960001
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    reg_step[uid] = "ob_sports"                       # abandoned sport picker
    try:
        upd = _update(uid, T["ru"]["menu_profile"])  # taps Profile
        await fc.handle_msg(upd, _ctx())
        assert upd.message.replies                   # routed → got a response
    finally:
        reg_step.pop(uid, None)


async def test_unregistered_user_in_onboarding_is_still_silent(temp_db):
    uid = 960002
    temp_db.db_ensure(uid, "u", "ru")                # is_registered=0
    reg_step[uid] = "awaiting_lang"
    try:
        upd = _update(uid, "random text")
        await fc.handle_msg(upd, _ctx())
        assert upd.message.replies == []             # correctly swallowed
    finally:
        reg_step.pop(uid, None)


async def test_start_clears_stale_regstep_for_registered_user(temp_db):
    from handlers.registration import start
    uid = 960003
    temp_db.db_ensure(uid, "u", "ru"); temp_db.db_set(uid, "is_registered", 1)
    reg_step[uid] = "ob_sports"
    upd = _update(uid, "/start")
    await start(upd, _ctx())
    assert uid not in reg_step                       # stale step cleared
    assert upd.message.replies                       # already-registered reply sent
