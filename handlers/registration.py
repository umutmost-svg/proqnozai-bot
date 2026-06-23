import asyncio
import logging

from telegram import Update, ReplyKeyboardRemove
from telegram.ext import ContextTypes

from config import reg_step, UNIVERSAL_WELCOME
from db import db_ensure, db_get, db_set, db_lang, db_is_reg, db_get_tz, db_user_stats, con
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
    tz = db_get_tz(uid)
    tz_str = f"UTC+{tz}" if tz >= 0 else f"UTC{tz}"
    stats = db_user_stats(uid)
    lang = db_lang(uid)

    # Build winrate line
    if stats["fb_pct"] is not None:
        winrate_line = tr(uid, "stats_winrate",
            pct=stats["fb_pct"], wins=stats["fb_wins"], total=stats["fb_total"])
    else:
        winrate_line = tr(uid, "stats_no_feedback")

    streak_line = tr(uid, "stats_streak", n=stats["streak"]) if stats["streak"] >= 2 else ""

    text = tr(uid, "profile_text",
        name=u["display_name"] or "-",
        lang=LANG_NAMES.get(u["lang"], u["lang"]),
        total_forecasts=stats["total_forecasts"],
        winrate=winrate_line,
        streak=("\n" + streak_line) if streak_line else "",
        joined=stats["joined"],
        tz=tz_str)
    await update.message.reply_text(text)


async def tz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db_is_reg(uid):
        await update.message.reply_text(tr(uid, "need_reg")); return
    lang = db_lang(uid)
    tz = db_get_tz(uid)
    tz_str = f"UTC+{tz}" if tz >= 0 else f"UTC{tz}"
    msgs = {
        "ru": f"🕐 Ваш часовой пояс: {tz_str}\n\nЧтобы изменить, отправьте смещение от UTC.\nПримеры: +4, +3, 0, -5",
        "az": f"🕐 Saat qurşağınız: {tz_str}\n\nDəyişmək üçün UTC fərqini göndərin.\nMəsələn: +4, +3, 0, -5",
        "en": f"🕐 Your timezone: {tz_str}\n\nTo change, send your UTC offset.\nExamples: +4, +3, 0, -5",
        "tr": f"🕐 Saat diliminiz: {tz_str}\n\nDeğiştirmek için UTC farkını gönderin.\nÖrnek: +4, +3, 0, -5",
        "kz": f"🕐 Уақыт белдеуіңіз: {tz_str}\n\nӨзгерту үшін UTC ауытқуын жіберіңіз.\nМысалы: +4, +3, 0, -5",
        "uz": f"🕐 Vaqt mintaqangiz: {tz_str}\n\nO'zgartirish uchun UTC farqini yuboring.\nMasalan: +4, +3, 0, -5",
        "ar": f"🕐 منطقتك الزمنية: {tz_str}\n\nلتغييرها أرسل الفرق عن UTC.\nمثلاً: +4، +3، 0، -5",
    }
    context.user_data["awaiting_tz"] = True
    await update.message.reply_text(msgs.get(lang, msgs["ru"]))


async def handle_tz_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    if not context.user_data.get("awaiting_tz"):
        return False
    text = (update.message.text or "").strip()
    try:
        offset = int(text.replace(" ", ""))
        if not (-12 <= offset <= 14):
            raise ValueError
    except ValueError:
        lang = db_lang(uid)
        err = {"ru": "Неверный формат. Введите число от -12 до +14, например: +4",
               "az": "Səhv format. -12 ilə +14 arasında rəqəm daxil edin, məsələn: +4",
               "en": "Invalid format. Enter a number from -12 to +14, e.g.: +4"}
        await update.message.reply_text(err.get(lang, err["ru"]))
        return True
    db_set(uid, "tz_offset", offset)
    context.user_data.pop("awaiting_tz")
    tz_str = f"UTC+{offset}" if offset >= 0 else f"UTC{offset}"
    lang = db_lang(uid)
    ok = {"ru": f"✅ Часовой пояс установлен: {tz_str}",
          "az": f"✅ Saat qurşağı təyin edildi: {tz_str}",
          "en": f"✅ Timezone set: {tz_str}",
          "tr": f"✅ Saat dilimi ayarlandı: {tz_str}",
          "kz": f"✅ Уақыт белдеуі орнатылды: {tz_str}",
          "uz": f"✅ Vaqt mintaqasi o'rnatildi: {tz_str}",
          "ar": f"✅ تم تعيين المنطقة الزمنية: {tz_str}"}
    await update.message.reply_text(ok.get(lang, ok["ru"]))
    return True
