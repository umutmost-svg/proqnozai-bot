from datetime import datetime, timezone, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from db import db_lang
from translations import T


SUPPORT_URL = "https://t.me/AIproqnoz_support"

def main_menu(uid):
    lang = db_lang(uid)
    tl = T[lang]
    return ReplyKeyboardMarkup([
        [tl["menu_forecast"],  tl["menu_express"]],
        [tl["menu_history"],   tl["menu_profile"]],
        [tl["menu_support"]],
    ], resize_keyboard=True, is_persistent=True)


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


# All match times are shown in Baku time (UTC+4) for every user.
BAKU_OFFSET = 4
# Mostbet returns its "DD.MM.YYYY HH:MM" times in Moscow time (UTC+3); we shift
# +1h to Baku (UTC+4). Change this if the source zone ever changes.
MOSTBET_SRC_TZ = 3


def _fmt_dt(dt_raw: str, tz_offset: int = BAKU_OFFSET) -> str:
    """Format a match datetime string into Baku time (UTC+4).

    ISO (T/Z) and "YYYY-MM-DD HH:MM" → assumed UTC → shifted to Baku.
    "DD.MM.YYYY HH:MM" (Mostbet) → assumed MOSTBET_SRC_TZ → shifted to Baku.
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
        # Convert from the source zone to Baku.
        dt_baku = dt + timedelta(hours=tz_offset - src_offset)
        return dt_baku.strftime("%d.%m %H:%M") + " (Bakı)"
    except Exception:
        try:
            return dt_raw[8:10] + "." + dt_raw[5:7] + " " + dt_raw[11:16]
        except Exception:
            return ""


def fmt_dt_for_user(dt_raw: str, uid: int) -> str:
    # Always Baku time, regardless of the user's language/region.
    return _fmt_dt(dt_raw, BAKU_OFFSET)
