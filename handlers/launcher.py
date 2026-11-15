"""Launcher mode (BOT_LAUNCHER_MODE=true, owner 15.11): the bot only opens the Mini App.

/start, any message and any old inline button (callback_data formats stay registered forever, CLAUDE.md) get one
answer: a short text and an "Open the app" button (a WebApp button, so the Mini App receives signed initData).
Admin commands, inquiries (group -1), the referral note on the first /start, the odds export, partner links and
the admin's outbox keep working. Texts in all 7 languages (translations: launcher_text / launcher_btn).
"""
from __future__ import annotations

import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes

import bot_crm
from config import MINIAPP_URL
import db
from translations import T

logger = logging.getLogger(__name__)


def launcher_markup(lang: str) -> InlineKeyboardMarkup:
    tl = T.get(lang) or T["ru"]
    return InlineKeyboardMarkup([[InlineKeyboardButton(tl["launcher_btn"], web_app=WebAppInfo(url=MINIAPP_URL))]])


def _lang(uid: int) -> str:
    try:
        lang = db.db_lang(uid)
    except Exception:  # noqa: BLE001 — a launcher answer never fails on the user's row
        lang = "ru"
    return lang if lang in T else "ru"


# Telegram language codes for the launcher text of a NEW user (the bot's own picker maps uz/ar elsewhere, but the
# launcher has all 7 texts).
_TG_LANG = {"az": "az", "ru": "ru", "uk": "ru", "be": "ru", "en": "en", "tr": "tr", "kk": "kz", "uz": "uz", "ar": "ar"}


async def _answer(chat_id: int, uid: int, context: ContextTypes.DEFAULT_TYPE, lang: str | None = None) -> None:
    lang = lang or _lang(uid)
    await context.bot.send_message(chat_id=chat_id, text=(T.get(lang) or T["ru"])["launcher_text"],
                                   reply_markup=launcher_markup(lang))


async def launcher_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.effective_chat is None:
        return
    # The user row (language from Telegram) and the referral of the very first start, as in the full bot.
    created = db.db_ensure(user.id, user.username or "", user.language_code)
    if created and context.args:
        bot_crm.note_start(user.id, context.args, True, int(time.time()))
    await _answer(update.effective_chat.id, user.id, context, _user_lang(user))


def _user_lang(user) -> str | None:
    """The language the user picked in the bot; else (never registered there) their Telegram language."""
    try:
        if db.db_is_reg(user.id):
            return None  # _answer reads the stored choice
    except Exception:  # noqa: BLE001
        pass
    tg = _TG_LANG.get((getattr(user, "language_code", None) or "").lower()[:2])
    return tg if tg in T else None


async def launcher_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.effective_chat is None or update.effective_chat.type != "private":
        return
    await _answer(update.effective_chat.id, user.id, context, _user_lang(user))


async def launcher_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any old inline button (forecast menus, history, watch, promo...): stop the client's spinner, then the app."""
    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
    except Exception:  # noqa: BLE001 — an expired query cannot be answered; the button below still helps
        pass
    if query.message is not None and update.effective_user is not None:
        await _answer(query.message.chat.id, update.effective_user.id, context, _user_lang(update.effective_user))
