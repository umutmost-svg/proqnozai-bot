from telegram.ext import CommandHandler, MessageHandler, CallbackQueryHandler, filters

from config import ADMIN_ID
from handlers.registration import start, lang_cb, lang_cmd, ob_cb, profile_cmd, tz_cmd, handle_tz_input
from handlers.forecast import (
    forecast_cb, forecast_menu_start,
    fm_sport_cb, fm_league_cb, fm_match_cb, fm_back_cb,
    handle_msg,
)
from handlers.live import watch_cb, matches_cmd
from handlers.history import history_cmd, history_cb
from handlers.express import express_cb, express_cmd, compare_cmd
from handlers.admin import admin_cmd, adm_cb, handle_adm_msg, cancel_cmd, testapi_cmd


def register_handlers(app):
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("lang",    lang_cmd))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("tz",      tz_cmd))
    app.add_handler(CommandHandler("matches", matches_cmd))
    app.add_handler(CommandHandler("admin",   admin_cmd))
    app.add_handler(CommandHandler("cancel",  cancel_cmd))
    app.add_handler(CommandHandler("testapi", testapi_cmd))

    app.add_handler(CallbackQueryHandler(lang_cb,       pattern=r"^lang_"))
    app.add_handler(CallbackQueryHandler(ob_cb,         pattern=r"^ob_"))
    app.add_handler(CallbackQueryHandler(forecast_cb,   pattern=r"^forecast_"))
    app.add_handler(CallbackQueryHandler(fm_sport_cb,   pattern=r"^fm_sp_"))
    app.add_handler(CallbackQueryHandler(fm_league_cb,  pattern=r"^fm_lg_"))
    app.add_handler(CallbackQueryHandler(fm_match_cb,   pattern=r"^fm_mt_"))
    app.add_handler(CallbackQueryHandler(fm_back_cb,    pattern=r"^fm_back_"))
    app.add_handler(CallbackQueryHandler(watch_cb,      pattern=r"^(watch|unwatch)_"))
    app.add_handler(CallbackQueryHandler(history_cb,    pattern=r"^(fb_|repeat_)"))
    app.add_handler(CallbackQueryHandler(express_cb,    pattern=r"^expr_"))
    app.add_handler(CallbackQueryHandler(adm_cb,        pattern=r"^adm_"))

    app.add_handler(MessageHandler(filters.TEXT & filters.User(ADMIN_ID), handle_adm_msg), group=0)
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_msg), group=1)
