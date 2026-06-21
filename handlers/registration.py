import asyncio
import logging

from telegram import Update, ReplyKeyboardRemove
from telegram.ext import ContextTypes

from config import reg_step, UNIVERSAL_WELCOME
from db import db_ensure, db_get, db_set, db_lang, db_is_reg, con
from translations import T, tr, LANG_NAMES, OB_SPORTS, sport_label, exp_label
from handlers.utils import main_menu, lang_kb, ob_kb

logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id
    db_ensure(uid, user.username or "", user.language_code)
    if db_is_reg(uid):
        u = db_get(uid)
        await update.message.reply_text(
            tr(uid, "already_reg", name=u["display_name"] or user.first_name),
            reply_markup=main_menu(uid))
        return
    reg_step[uid] = "awaiting_lang"
    await update.message.reply_text(UNIVERSAL_WELCOME, reply_markup=lang_kb())


async def lang_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; lang = q.data.split("_")[1]
    db_ensure(uid, q.from_user.username or "", q.from_user.language_code)
    db_set(uid, "lang", lang)

    if db_is_reg(uid):
        await q.edit_message_text(T[lang]["lang_set"])
        await context.bot.send_message(chat_id=uid, text=T[lang]["lang_set"],
            reply_markup=main_menu(uid))
        return

    # Auto-register with Telegram name
    name = (q.from_user.first_name or q.from_user.username or "User")[:64]
    db_set(uid, "display_name", name)
    with con() as c:
        c.execute("UPDATE users SET is_registered=1 WHERE user_id=?", (uid,))
    reg_step[uid] = "ob_sports"
    await q.edit_message_text(T[lang]["welcome_intro"])
    await asyncio.sleep(0.4)
    ob_lang = lang if lang in OB_SPORTS else "ru"
    await context.bot.send_message(chat_id=uid, text=T[lang]["ob_sports"],
        reply_markup=ob_kb(OB_SPORTS[ob_lang]))


async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        tr(update.effective_user.id, "choose_lang"), reply_markup=lang_kb())


async def handle_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    if reg_step.get(uid) != "awaiting_name":
        return False
    name = update.message.text.strip()
    if len(name) < 2 or len(name) > 64:
        await update.message.reply_text("2-64 simvol / символа / characters")
        return True
    db_set(uid, "display_name", name)
    with con() as c:
        c.execute("UPDATE users SET is_registered=1 WHERE user_id=?", (uid,))
    reg_step[uid] = "ob_sports"
    await update.message.reply_text(tr(uid, "reg_done", name=name),
        reply_markup=ReplyKeyboardRemove())
    await asyncio.sleep(0.3)
    lang = db_lang(uid)
    await update.message.reply_text(T[lang]["welcome_intro"])
    await asyncio.sleep(0.5)
    await update.message.reply_text(T[lang]["ob_sports"], reply_markup=ob_kb(OB_SPORTS[lang]))
    return True


async def ob_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; val = q.data[3:]  # strip "ob_"
    lang = db_lang(uid); step = reg_step.get(uid, "")

    if step == "ob_sports":
        db_set(uid, "sports", val)
        db_set(uid, "onboarding_done", 1)
        reg_step[uid] = "done"
        u = db_get(uid)
        done_msg = T[lang]["ob_done"].format(sports=sport_label(uid, u["sports"]))
        await q.edit_message_text(done_msg)
        await asyncio.sleep(0.3)
        await context.bot.send_message(
            chat_id=uid, text=T[lang]["post_onboarding"], reply_markup=main_menu(uid))


async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db_is_reg(uid):
        await update.message.reply_text(tr(uid, "need_reg")); return
    u = db_get(uid)
    await update.message.reply_text(tr(uid, "profile_text",
        name=u["display_name"] or "-",
        lang=LANG_NAMES.get(u["lang"], u["lang"]),
        total_req=u["total_requests"],
        sports=sport_label(uid, u["sports"]) if u["sports"] else "-",
        exp=exp_label(uid, u["experience"]) if u["experience"] else "-"))
