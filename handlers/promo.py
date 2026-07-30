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
from db import db_is_reg, db_claim_promo, db_add_promo_codes, db_promo_stats
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
    """Shared gate: registration → channel subscription → hand out a code.
    `reply(text, reply_markup=None)` is an async sender."""
    if not db_is_reg(uid):
        await reply(tr(uid, "need_reg")); return
    sub = await _is_subscribed(context, uid)
    if sub is None:
        await reply(tr(uid, "promo_unavailable")); return
    if not sub:
        await reply(tr(uid, "promo_subscribe"), reply_markup=_subscribe_kb(uid)); return
    code = db_claim_promo(uid)
    if code is None:
        await reply(tr(uid, "promo_empty")); return
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
async def addpromo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/addpromo CODE1 CODE2 …  (also newline-separated). Admin only."""
    if update.effective_user.id != ADMIN_ID:
        return
    parts = (update.message.text or "").split(None, 1)
    codes = parts[1].split() if len(parts) > 1 else []
    if not codes:
        await update.message.reply_text(
            "Usage: /addpromo CODE1 CODE2 …  (space- or newline-separated)")
        return
    added = db_add_promo_codes(codes)
    st = db_promo_stats()
    await update.message.reply_text(
        f"✅ Added {added} new code(s) ({len(codes) - added} dup/empty skipped).\n"
        f"Pool: {st['available']} available / {st['total']} total.")


async def promostats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/promostats — pool size / claimed / available. Admin only."""
    if update.effective_user.id != ADMIN_ID:
        return
    st = db_promo_stats()
    await update.message.reply_text(
        f"🎁 Promo pool\nTotal: {st['total']}\nClaimed: {st['claimed']}\n"
        f"Available: {st['available']}")
