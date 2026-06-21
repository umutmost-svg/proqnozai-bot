from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from db import db_is_reg, db_get_favs, db_add_fav, db_del_fav
from translations import tr


async def favs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db_is_reg(uid):
        await update.message.reply_text(tr(uid, "need_reg")); return
    favs = db_get_favs(uid)
    if not favs:
        await update.message.reply_text(tr(uid, "fav_empty")); return
    lines = [tr(uid, "fav_list")]
    btns = []
    for team in favs:
        lines.append(f"- {team}")
        btns.append([InlineKeyboardButton(f"X {team}", callback_data=f"delfav_{team[:30]}")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(btns))


async def fav_toggle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    if q.data.startswith("addfav_"):
        team = q.data[7:]
        db_add_fav(uid, team)
        await q.edit_message_text(q.message.text + "\n\n" + tr(uid, "fav_added", team=team))
    elif q.data.startswith("delfav_"):
        team = q.data[7:]
        db_del_fav(uid, team)
        await q.edit_message_text(tr(uid, "fav_removed", team=team))
