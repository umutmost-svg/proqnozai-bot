from datetime import datetime, timezone, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from config import MOSTBET_SRC_TZ, violations, SPAM_AFTER, SPAM_DUR, PROMO_CHANNEL, PARTNERS
from db import db_lang, db_get_tz
from security import sec_blocked, rate_check, nav_rate_check, record_viol
from translations import T, tr


SUPPORT_URL = "https://t.me/AIproqnoz_support"

# ─── Callback rate-limit gates ─────────────────────────────────────────────────
# Users the bot is CURRENTLY generating a forecast/express for. One expensive
# operation per user at a time, so rapid double-clicks can't start duplicate
# Claude/enrichment work. In-memory like the rest of the rate-limit state.
_cb_inflight: set = set()


async def _blocked_gate(update) -> bool:
    """True if the user is currently temp-blocked (spam auto-block). Answers
    the callback query itself with a localized toast on refusal."""
    q = update.callback_query
    uid = q.from_user.id
    blk, secs = sec_blocked(uid)
    if blk:
        await q.answer(tr(uid, "blocked", m=secs // 60, s=secs % 60), show_alert=True)
    return blk


async def _rate_limited(update) -> bool:
    """True if this callback exceeds the SAME rate budget as text messages
    (records a violation and answers the query itself with a toast on
    refusal); resets the violation counter and returns False otherwise."""
    q = update.callback_query
    uid = q.from_user.id
    exceeded, wait = rate_check(uid)
    if not exceeded:
        violations[uid] = 0
        return False
    info = f"CB | id={uid} @{getattr(q.from_user, 'username', None) or '-'}"
    if record_viol(uid, info):
        await q.answer(tr(uid, "auto_blocked", min=SPAM_DUR // 60), show_alert=True)
    else:
        await q.answer(tr(uid, "rate_limit", w=wait, v=violations[uid], max=SPAM_AFTER))
    return True


async def cb_guard(update) -> bool:
    """Gate for callbacks that trigger Claude / external-API work.

    Applies the SAME budget as text messages (sec_blocked + rate_check +
    violation accounting) plus a per-user in-flight lock. On refusal it answers
    the callback query itself with a short localized toast, so the client
    spinner never hangs. On True the caller OWNS the in-flight slot and MUST
    call cb_release(uid) in a finally block."""
    q = update.callback_query
    uid = q.from_user.id
    if await _blocked_gate(update):
        return False
    if uid in _cb_inflight:
        await q.answer("⏳")  # previous request still generating
        return False
    if await _rate_limited(update):
        return False
    _cb_inflight.add(uid)
    return True


def cb_release(uid) -> None:
    _cb_inflight.discard(uid)


async def nav_guard(update) -> bool:
    """Gate for pure menu-navigation callbacks (sport/day/country/tournament
    selection, "show more" pagination, "back") that never call Claude or any
    external API. Uses a SEPARATE, far more generous budget than text/expensive
    callbacks (nav_rate_check: NAV_RATE_MAX clicks per NAV_RATE_WINDOW s), so
    normal quick browsing — including tapping "back" a few times — is never
    throttled, and a burst of navigation can never drain the strict text budget
    or accrue violations toward the SPAM_DUR auto-block. A temp-blocked user is
    still blocked (sec_blocked). On refusal it soft-throttles: answers the query
    with a short toast so the spinner never hangs; nothing to release."""
    if await _blocked_gate(update):
        return False
    q = update.callback_query
    uid = q.from_user.id
    exceeded, wait = nav_rate_check(uid)
    if exceeded:
        await q.answer(tr(uid, "nav_slow", w=wait))
        return False
    return True

# Universal language button — same label in every language so one handler matches.
LANG_BTN = "🌐 Dil · Язык · Lang"


def main_menu(uid):
    lang = db_lang(uid)
    tl = T[lang]
    rows = [
        [tl["menu_forecast"]],
        [tl["menu_history"],   tl["menu_profile"]],
        [tl["menu_support"],   LANG_BTN],
    ]
    # The promo button only appears once a gate channel is configured.
    if PROMO_CHANNEL:
        rows.insert(1, [tl["menu_get_promo"]])
    # "Our partners" only appears once at least one partner is configured.
    if PARTNERS:
        rows.append([tl["menu_partners"]])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def lang_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Azərbaycan", callback_data="lang_az"),
            InlineKeyboardButton("Русский",    callback_data="lang_ru"),
            InlineKeyboardButton("English",    callback_data="lang_en"),
        ],
        [
            InlineKeyboardButton("Türkçe",     callback_data="lang_tr"),
            InlineKeyboardButton("Қазақша",    callback_data="lang_kz"),
            InlineKeyboardButton("O'zbek",     callback_data="lang_uz"),
        ],
        [
            InlineKeyboardButton("العربية",    callback_data="lang_ar"),
        ],
    ])


def ob_kb(items):
    rows = []
    for i in range(0, len(items), 2):
        row = [InlineKeyboardButton(items[i][0], callback_data=f"ob_{items[i][1]}")]
        if i + 1 < len(items):
            row.append(InlineKeyboardButton(items[i+1][0], callback_data=f"ob_{items[i+1][1]}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


SPORT_EMOJI = {
    "football": "⚽", "soccer": "⚽", "futbol": "⚽", "футбол": "⚽",
    "basketball": "🏀", "баскетбол": "🏀", "basketbol": "🏀",
    "tennis": "🎾", "теннис": "🎾",
    "hockey": "🏒", "хоккей": "🏒", "hokey": "🏒",
    "ufc": "🥊", "mma": "🥊", "boxing": "🥊", "бокс": "🥊",
    "volleyball": "🏐", "handball": "🤾",
}


def _sport_emoji(cat: str) -> str:
    cl = cat.lower()
    return next((v for k, v in SPORT_EMOJI.items() if k in cl), "🏆")


# Fallback display offset when a user has no stored timezone. Baku (UTC+4)
# matches the bot's primary audience; fmt_dt_for_user overrides it per user.
BAKU_OFFSET = 4


def _fmt_dt(dt_raw: str, tz_offset: int = BAKU_OFFSET) -> str:
    """Format a raw match datetime string into the given UTC offset, with a
    matching "(UTC±N)" suffix (same convention as the menu's _fmt_kickoff).

    ISO (T/Z) and "YYYY-MM-DD HH:MM" → assumed UTC.
    "DD.MM.YYYY HH:MM" (Mostbet) → assumed MOSTBET_SRC_TZ.
    """
    if not dt_raw or len(dt_raw) < 16:
        return ""
    try:
        ds = dt_raw.strip()
        if "T" in ds:
            dt = datetime.fromisoformat(ds.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
            src_offset = 0  # UTC
        elif "." in ds:
            dt = datetime.strptime(ds[:16], "%d.%m.%Y %H:%M").replace(tzinfo=timezone.utc)
            src_offset = MOSTBET_SRC_TZ
        else:
            dt = datetime.strptime(ds[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            src_offset = 0  # UTC
        # Convert from the source zone to the target display offset.
        dt_local = dt + timedelta(hours=tz_offset - src_offset)
        sign = "+" if tz_offset >= 0 else "-"
        return dt_local.strftime("%d.%m %H:%M") + f" (UTC{sign}{abs(tz_offset)})"
    except Exception:
        try:
            return dt_raw[8:10] + "." + dt_raw[5:7] + " " + dt_raw[11:16]
        except Exception:
            return ""


def fmt_dt_for_user(dt_raw: str, uid: int) -> str:
    """Match time in the USER's stored timezone — same convention as the
    forecast menu's _fmt_kickoff, so times are consistent across the menu and
    the express/compare flows. Falls back to Baku when no tz is stored."""
    return _fmt_dt(dt_raw, db_get_tz(uid) or BAKU_OFFSET)
