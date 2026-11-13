"""Inquiries for the admin panel (BOT_INQUIRIES_ENABLED, off by default): NEW inbound private texts are kept in
bot-crm.db for the support team, after a one-time notice to the user. Registered only with the flag on, in a group
before the normal handlers, and never stops them: the user's message is handled exactly as without the flag."""
from __future__ import annotations

import asyncio
import logging
import time

from telegram import Update
from telegram.ext import ContextTypes

import bot_crm
from config import ADMIN_ID
from db import db_lang
from translations import T

logger = logging.getLogger(__name__)

# Menu buttons are navigation, not inquiries.
_MENU = frozenset(v for tl in T.values() for k, v in tl.items() if k.startswith("menu_"))
# Updates are processed one by one today (no concurrent_updates); the lock keeps "one notice per user" true even if
# that changes: check, send and mark never interleave.
_notice_lock = asyncio.Lock()
# Messages written before this process started with the flag on (e.g. queued by Telegram while the bot was down or
# the feature was off) are never collected: only messages sent after activation are "new".
ACTIVE_SINCE = time.time()


async def inquiry_hook(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    chat = update.effective_chat
    if not bot_crm.BOT_INQUIRIES_ENABLED or msg is None or chat is None or chat.type != "private":
        return
    uid = update.effective_user.id
    text = (msg.text or "").strip()
    if uid == ADMIN_ID or not text or text.startswith("/") or text in _MENU:
        return
    sent_at = msg.date.timestamp() if getattr(msg, "date", None) is not None else 0.0
    if sent_at < ACTIVE_SINCE:
        return
    now = int(time.time())
    try:
        crm = bot_crm.crm()
        if not crm.notice_sent(uid):
            async with _notice_lock:
                if not crm.notice_sent(uid):
                    # The notice comes first; only after it was delivered is anything stored, and never for longer
                    # than the period it states (kept with the notice, enforced by the purge if the setting grows).
                    days = crm.retention_days()
                    await msg.reply_text(T[db_lang(uid)]["inquiry_notice"].format(days=days))
                    crm.mark_notice(uid, now, days)
        crm.record_inquiry(uid, text, now)
    except Exception as exc:  # noqa: BLE001 — storage must never affect the bot's answer; type only, never the text
        logger.warning("event=inquiry_store_failed type=%s", type(exc).__name__)
