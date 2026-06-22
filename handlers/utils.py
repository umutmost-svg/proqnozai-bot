from datetime import datetime, timezone, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from db import db_lang, db_get_tz
from translations import T


def main_menu(uid):
    lang = db_lang(uid)
    tl = T[lang]
    return ReplyKeyboardMarkup([
        [tl["menu_forecast"],  tl["menu_express"]],
        [tl["menu_history"],   tl["menu_profile"]],
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


def _fmt_dt(dt_raw: str, tz_offset: int = 0) -> str:
    """Format match datetime string.

    ISO format (T or Z) → assumed UTC → apply tz_offset (football-data.org, api-sports).
    DD.MM.YYYY format (Mostbet) → shown as-is, no conversion (Mostbet returns local time).
    YYYY-MM-DD HH:MM format → assumed UTC → apply tz_offset.
    """
    if not dt_raw or len(dt_raw) < 16:
        return ""
    try:
        ds = dt_raw.strip()
        if "T" in ds:
            # ISO format from football APIs — always UTC
            dt = datetime.fromisoformat(ds.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
            dt_local = dt + timedelta(hours=tz_offset)
            sign = "+" if tz_offset >= 0 else ""
            return dt_local.strftime("%d.%m %H:%M") + f" (UTC{sign}{tz_offset})"
        elif "." in ds:
            # Mostbet format: "DD.MM.YYYY HH:MM:SS" — already in local time, show as-is
            dt = datetime.strptime(ds[:16], "%d.%m.%Y %H:%M")
            return dt.strftime("%d.%m %H:%M")
        else:
            # "YYYY-MM-DD HH:MM:SS" — assumed UTC, apply user offset
            dt = datetime.strptime(ds[:16], "%Y-%m-%d %H:%M")
            dt = dt.replace(tzinfo=timezone.utc)
            dt_local = dt + timedelta(hours=tz_offset)
            sign = "+" if tz_offset >= 0 else ""
            return dt_local.strftime("%d.%m %H:%M") + f" (UTC{sign}{tz_offset})"
    except Exception:
        try:
            return dt_raw[8:10] + "." + dt_raw[5:7] + " " + dt_raw[11:16]
        except Exception:
            return ""


def fmt_dt_for_user(dt_raw: str, uid: int) -> str:
    return _fmt_dt(dt_raw, db_get_tz(uid))
