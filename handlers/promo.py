"""Channel-gated promo codes.

One shared code with a total-use cap, not a pool of unique codes: every user
who qualifies receives the SAME code, and the campaign stops issuing it once
the cap is reached.

User taps "🎁 Get promo code" → must be registered AND subscribed to
PROMO_CHANNEL → receives the active code (idempotent: repeat taps return the
same code and consume only one use). Admin sets the campaign with
/setpromo CODE MAX_USES and checks /promostats.
The bot must be an admin/member of PROMO_CHANNEL for getChatMember to work.
"""
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import PROMO_CHANNEL, ADMIN_ID
from handlers.utils import channel_url
from db import (db_is_reg, db_claim_promos, db_list_promo_codes,
                db_set_promo_code, db_delete_promo_code)
from translations import tr

logger = logging.getLogger(__name__)

_OK_STATUS = {"member", "administrator", "creator"}


async def _is_subscribed(context, uid) -> bool | None:
    """True / False, or None when it can't be determined (the bot isn't in the
    channel, or can't read it) — the caller then shows 'unavailable' instead of
    falsely refusing a subscribed user.

    An unconfigured channel is NOT None: there is no gate to fail, and the two
    used to be indistinguishable both to the user and in the logs. It returns
    True so the caller can decide, and the decision is made there."""
    if not PROMO_CHANNEL:
        return True
    try:
        m = await context.bot.get_chat_member(PROMO_CHANNEL, uid)
        if m.status in _OK_STATUS:
            return True
        # A restricted member is still IN the channel (muted, typically). PTB
        # exposes is_member for exactly this case; anything else is 'left' or
        # 'kicked', which genuinely is not subscribed.
        return m.status == "restricted" and bool(getattr(m, "is_member", False))
    except Exception as e:
        # The ONLY silent path used to be the one above, so an operator looking
        # for this line in the logs and finding nothing learned nothing.
        logger.warning(f"promo get_chat_member failed channel={PROMO_CHANNEL!r} "
                       f"uid={uid}: {type(e).__name__}: {e}")
        return None


def _subscribe_kb(uid) -> InlineKeyboardMarkup:
    rows = []
    url = channel_url()
    if url:
        rows.append([InlineKeyboardButton(tr(uid, "promo_open_channel"), url=url)])
    rows.append([InlineKeyboardButton(tr(uid, "promo_check_btn"), callback_data="promo_check")])
    return InlineKeyboardMarkup(rows)


async def _run_promo(context, uid, reply) -> None:
    """Shared gate: registration → active campaign → channel subscription →
    hand out THE code. `reply(text, reply_markup=None)` is an async sender."""
    if not db_is_reg(uid):
        await reply(tr(uid, "need_reg")); return
    if not db_list_promo_codes():
        await reply(tr(uid, "promo_unavailable")); return   # nothing configured
    if not PROMO_CHANNEL:
        # main_menu() hides the button in this state, but a reply keyboard is
        # persistent: one already sitting in a chat keeps sending the label.
        # "Can't verify your subscription" was a lie — there is no channel to
        # be subscribed to, and no amount of retrying would have helped.
        logger.warning("promo requested with PROMO_CHANNEL unset — "
                       "the gate cannot be satisfied; set it or hide the button")
        await reply(tr(uid, "promo_unavailable")); return
    sub = await _is_subscribed(context, uid)
    if sub is None:
        # Distinct from "no campaign": the gate itself is broken (channel not
        # configured, or the bot isn't in it). The user gets a message that
        # says to retry; the technical reason is already in the warning log.
        await reply(tr(uid, "promo_check_failed")); return
    if not sub:
        # Name the partners BEFORE asking for a subscription: the gate used to
        # demand a paid action ("subscribe") for an unnamed reward, which is the
        # order that makes people leave.
        names = ", ".join(c["partner"] for c in db_list_promo_codes() if c["partner"])
        text = tr(uid, "promo_subscribe")
        if names:
            text = f"{tr(uid, 'promo_preview', partners=names)}\n\n{text}"
        await reply(text, reply_markup=_subscribe_kb(uid)); return
    granted = db_claim_promos(uid)
    if not granted:
        await reply(tr(uid, "promo_empty")); return         # every cap reached
    lines = [tr(uid, "promo_codes_title")]
    for item in granted:
        name = item["partner"] or tr(uid, "partners_btn")
        lines.append(f"{name} — <code>{item['code']}</code>")
    await reply("\n\n".join(lines), parse_mode="HTML")


async def promo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"🎁 Get promo code" reply-keyboard button / /promo command."""
    uid = update.effective_user.id

    async def reply(text, reply_markup=None, **kw):
        await update.message.reply_text(text, reply_markup=reply_markup, **kw)

    await _run_promo(context, uid, reply)


async def promo_check_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"✅ I subscribed" inline button — re-run the gate and issue the code."""
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()

    async def reply(text, reply_markup=None, **kw):
        await context.bot.send_message(chat_id=uid, text=text,
                                       reply_markup=reply_markup, **kw)

    await _run_promo(context, uid, reply)


# ─── Admin ────────────────────────────────────────────────────────────────────
async def setpromo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setpromo PARTNER CODE MAX_USES — set one partner's code and its own cap,
    e.g. /setpromo Mostbet WELCOME500 500. Each partner is independent: running
    one out does not hide the others. Admin only."""
    if update.effective_user.id != ADMIN_ID:
        return
    parts = (update.message.text or "").split()
    if len(parts) < 4:
        await update.message.reply_text(
            "Usage: /setpromo PARTNER CODE MAX_USES\n"
            "  e.g. /setpromo Mostbet WELCOME500 500\n"
            "  each partner needs its OWN code\n"
            "  /delpromo PARTNER — remove one\n"
            "  /promostats — current state\n"
            "  /promodiag — why the bonus button fails")
        return
    partner, code = parts[1], parts[2]
    try:
        max_uses = int(parts[3])
        if max_uses <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("MAX_USES must be a positive integer.")
        return
    try:
        db_set_promo_code(partner, code, max_uses)
    except ValueError as e:
        # Usually the same code given to two partners: claims are keyed by the
        # code string, so sharing one would merge their caps. Say so plainly
        # instead of letting the error handler show a generic failure. A partner
        # running a code pool lands here too, with its own explanation.
        hint = "\nGive each partner its own code." if "already used by" in str(e) else ""
        await update.message.reply_text(f"⚠️ {e}{hint}")
        return
    await update.message.reply_text(f"✅ {partner}: {code} · cap {max_uses}")
    await promostats_cmd(update, context)


async def delpromo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/delpromo PARTNER — remove one partner's code. Admin only."""
    if update.effective_user.id != ADMIN_ID:
        return
    parts = (update.message.text or "").split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /delpromo PARTNER")
        return
    removed = db_delete_promo_code(parts[1])
    await update.message.reply_text(
        f"🗑 {parts[1]} removed." if removed else f"No code for {parts[1]}.")


async def promodiag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/promodiag — why the bonus button fails, in the chat. Admin only.

    The gate can fail for reasons only Telegram knows (the bot was never added
    to the channel, or is not an admin of it), and reading them meant digging
    through the host's logs. This runs the same getChatMember the gate runs and
    reports the raw answer."""
    if update.effective_user.id != ADMIN_ID:
        return
    lines = ["🩺 Promo gate:"]
    lines.append(f"PROMO_CHANNEL: {PROMO_CHANNEL or '(not set — the bonus button is hidden)'}")
    lines.append(f"channel link:  {channel_url() or '(none — set PROMO_CHANNEL_URL)'}")

    codes = db_list_promo_codes()
    if not codes:
        lines.append("campaigns:     none live — the button stays hidden")
    else:
        for c in codes:
            what = "pool" if c.get("mode") == "pool" else c["code"]
            lines.append(f"campaign:      {c['partner']}: {what} "
                         f"— {c['claimed']}/{c['max_uses']} used")

    if not PROMO_CHANNEL:
        lines.append("\nverdict: no channel configured, so nothing is issued.")
        await update.message.reply_text("\n".join(lines))
        return

    try:
        m = await context.bot.get_chat_member(PROMO_CHANNEL, update.effective_user.id)
        lines.append(f"\ngetChatMember: OK, your status is '{m.status}'")
        lines.append("verdict: the gate works. A user who is subscribed gets a code.")
    except Exception as e:
        lines.append(f"\ngetChatMember: FAILED — {type(e).__name__}: {e}")
        lines.append("verdict: every user sees 'can't verify subscription'.")
        lines.append("Usually: the bot is not an ADMIN of the channel, or "
                     "PROMO_CHANNEL names a chat it was never added to.")
    await update.message.reply_text("\n".join(lines))


async def promostats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/promostats — every partner's code, claimed / cap. Admin only."""
    if update.effective_user.id != ADMIN_ID:
        return
    # include_inactive: this is the admin readout, so a campaign that was
    # switched off in the dashboard must still be visible here.
    codes = db_list_promo_codes(include_inactive=True)
    if not codes:
        await update.message.reply_text(
            "No promo codes. Add one: /setpromo PARTNER CODE MAX_USES")
        return
    lines = ["🎁 Promo codes:"]
    for c in codes:
        name = c["partner"] or "(no partner)"
        state = "" if c["is_active"] and not c["is_archived"] else " · OFF"
        # A pool campaign has no single code — every holder has a different
        # string — so its size stands in for one.
        what = "pool of single-use codes" if c.get("mode") == "pool" else c["code"]
        lines.append(f"{name}: {what} — {c['claimed']}/{c['max_uses']} "
                     f"(available {c['available']}){state}")
    await update.message.reply_text("\n".join(lines))
