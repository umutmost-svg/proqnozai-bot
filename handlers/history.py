from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from db import db_is_reg, db_get_history, db_set_feedback, db_feedback_stats
from translations import tr


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db_is_reg(uid):
        await update.message.reply_text(tr(uid, "need_reg")); return
    history = db_get_history(uid)
    if not history:
        await update.message.reply_text(tr(uid, "history_empty")); return

    stats = db_feedback_stats(uid)
    lines = [tr(uid, "history_title")]
    if stats["total"] > 0:
        lines.append(tr(uid, "winrate", pct=stats["pct"], wins=stats["wins"], total=stats["total"]))
    lines.append("")
    btns = []
    for i, h in enumerate(history, 1):
        d = h["created_at"][:10]
        q_short = h["query"][:40]
        fb = " ✅" if h["feedback"] == 1 else (" ❌" if h["feedback"] == 0 else "")
        lines.append(f"{i}. {q_short} ({d}){fb}")
        if h["feedback"] is None:
            btns.append([
                InlineKeyboardButton(f"✅ #{i}", callback_data=f"fb_1_{h['id']}"),
                InlineKeyboardButton(f"❌ #{i}", callback_data=f"fb_0_{h['id']}"),
                InlineKeyboardButton(f"🔄 #{i}", callback_data=f"repeat_{h['id']}"),
            ])
        else:
            btns.append([InlineKeyboardButton(f"🔄 Повторить #{i}", callback_data=f"repeat_{h['id']}")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(btns))


async def history_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; data = q.data

    if data.startswith("fb_"):
        parts = data.split("_")
        feedback = int(parts[1]); hist_id = int(parts[2])
        db_set_feedback(hist_id, feedback)
        await q.edit_message_text(tr(uid, "feedback_done"))

    elif data.startswith("repeat_"):
        hist_id = int(data.split("_")[1])
        history = db_get_history(uid)
        item = next((h for h in history if h["id"] == hist_id), None)
        if not item:
            await q.edit_message_text(tr(uid, "api_error")); return
        await q.edit_message_text(item["forecast"][:4000])
