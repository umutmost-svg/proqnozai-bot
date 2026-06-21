from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from db import db_lang
from translations import T


def main_menu(uid):
    lang = db_lang(uid)
    tl = T[lang]
    return ReplyKeyboardMarkup([
        [tl["menu_forecast"],  tl["menu_express"]],
        [tl["menu_history"],   tl["menu_favs"]],
        [tl["menu_matches"],   tl["menu_profile"]],
        [tl["menu_lang"]],
    ], resize_keyboard=True)


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
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"ob_{val}")] for label, val in items]
    )


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


def _fmt_dt(dt_raw: str) -> str:
    if len(dt_raw) < 16:
        return ""
    return dt_raw[8:10] + "." + dt_raw[5:7] + " " + dt_raw[11:16]
