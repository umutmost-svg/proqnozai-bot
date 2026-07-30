"""Channel-gated promo codes.

User taps "🎁 Get promo code" → must be registered AND subscribed to
PROMO_CHANNEL → gets ONE unique code from the pool (idempotent: the same code
on repeat taps). Admin loads codes with /addpromo and checks /promostats.
The bot must be an admin/member of PROMO_CHANNEL for getChatMember to work.
"""
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import PROMO_CHANNEL, PROMO_CHANNEL_URL, ADMIN_ID
from db import (db_is_reg, db_claim_promo, db_get_promo_campaign,
                db_set_promo_campaign, db_promo_stats)
from translations import tr

logger = logging.getLogger(__name__)

_OK_STATUS = {"member", "administrator", "creator"}


def _channel_url() -> str:
    if PROMO_CHANNEL_URL:
        return PROMO_CHANNEL_URL
    if PROMO_CHANNEL.startswith("@"):
        return f"https://t.me/{PROMO_CHANNEL[1:]}"
    return ""


async def _is_subscribed(context, uid) -> bool | None:
    """True / False, or None when it can't be determined (no channel configured,
    or the bot isn't an admin of it) — the caller then shows 'unavailable'
    instead of falsely refusing a subscribed user."""
    if not PROMO_CHANNEL:
        return None
    try:
        m = await context.bot.get_chat_member(PROMO_CHANNEL, uid)
        return m.status in _OK_STATUS
    except Exception as e:
        logger.warning(f"promo get_chat_member failed uid={uid}: {e}")
        return None


def _subscribe_kb(uid) -> InlineKeyboardMarkup:
    rows = []
    url = _channel_url()
    if url:
        rows.append([InlineKeyboardButton(tr(uid, "promo_open_channel"), url=url)])
    rows.append([InlineKeyboardButton(tr(uid, "promo_check_btn"), callback_data="promo_check")])
    return InlineKeyboardMarkup(rows)


async def _run_promo(context, uid, reply) -> None:
    """Shared gate: registration → active campaign → channel subscription →
    hand out THE code. `reply(text, reply_markup=None)` is an async sender."""
    if not db_is_reg(uid):
        await reply(tr(uid, "need_reg")); return
    if db_get_promo_campaign() is None:
        await reply(tr(uid, "promo_unavailable")); return   # no active promo
    sub = await _is_subscribed(context, uid)
    if sub is None:
        await reply(tr(uid, "promo_unavailable")); return   # can't verify channel
    if not sub:
        await reply(tr(uid, "promo_subscribe"), reply_markup=_subscribe_kb(uid)); return
    code = db_claim_promo(uid)
    if code is None:
        await reply(tr(uid, "promo_empty")); return         # use cap reached
    await reply(tr(uid, "promo_code", code=code))


async def promo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"🎁 Get promo code" reply-keyboard button / /promo command."""
    uid = update.effective_user.id

    async def reply(text, reply_markup=None):
        await update.message.reply_text(text, reply_markup=reply_markup)

    await _run_promo(context, uid, reply)


async def promo_check_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"✅ I subscribed" inline button — re-run the gate and issue the code."""
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()

    async def reply(text, reply_markup=None):
        await context.bot.send_message(chat_id=uid, text=text, reply_markup=reply_markup)

    await _run_promo(context, uid, reply)


# ─── Admin ────────────────────────────────────────────────────────────────────
async def setpromo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setpromo CODE MAX_USES — set THE promo code and its total-use cap
    (e.g. /setpromo WELCOME500 500). Setting a new code resets the count.
    Admin only."""
    if update.effective_user.id != ADMIN_ID:
        return
    parts = (update.message.text or "").split()
    if len(parts) < 3:
        await update.message.reply_text(
            "Usage: /setpromo CODE MAX_USES   e.g. /setpromo WELCOME500 500")
        return
    code = parts[1]
    try:
        max_uses = int(parts[2])
        if max_uses <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("MAX_USES must be a positive integer.")
        return
    db_set_promo_campaign(code, max_uses)
    st = db_promo_stats()
    await update.message.reply_text(
        f"✅ Promo set: {st['code']} · cap {st['max_uses']} uses.\n"
        f"Claimed: {st['claimed']} · available: {st['available']}.")


async def promostats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/promostats — active code, claimed / cap. Admin only."""
    if update.effective_user.id != ADMIN_ID:
        return
    st = db_promo_stats()
    if not st["code"]:
        await update.message.reply_text("No active promo. Set one: /setpromo CODE MAX_USES")
        return
    await update.message.reply_text(
        f"🎁 Promo: {st['code']}\nClaimed: {st['claimed']} / {st['max_uses']}\n"
        f"Available: {st['available']}")
