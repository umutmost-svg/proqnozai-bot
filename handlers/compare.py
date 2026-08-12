"""Two-team comparison (/compare).

Split out of the former express module when the express (accumulator) feature
was removed — the comparison flow is independent of it and stays.
"""
import logging

from telegram import Update
from telegram.ext import ContextTypes

from db import db_is_reg, db_lang
from translations import T, tr
from claude_client import _create_with_retry

logger = logging.getLogger(__name__)


async def compare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db_is_reg(uid):
        await update.message.reply_text(tr(uid, "need_reg")); return
    lang = db_lang(uid)
    await update.message.reply_text(T[lang]["compare_ask"])
    context.user_data["awaiting_compare"] = True


async def handle_compare(uid: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not context.user_data.get("awaiting_compare"):
        return False
    context.user_data.pop("awaiting_compare")
    words = text.strip().split()
    if len(words) < 2:
        await context.bot.send_message(chat_id=uid, text=tr(uid, "compare_ask"))
        return True

    lang = db_lang(uid)
    await context.bot.send_chat_action(chat_id=uid, action="typing")

    compare_prompts = {
        "az": f"İki komandanı müqayisə et: {text}. Forma (son 5 matç), baş-başa görüşlər (son 5), güclü/zəif tərəflər, xG statistikası, hücum/müdafiə. Emoji istifadə et, markdown ** yox. Qısa və konkret.",
        "ru": f"Сравни две команды: {text}. Форма (последние 5 матчей), очные встречи (последние 5), сильные/слабые стороны, xG статистика, атака/защита. Используй emoji, markdown ** не используй. Кратко и по делу.",
        "en": f"Compare two teams: {text}. Form (last 5 matches), head-to-head (last 5), strengths/weaknesses, xG stats, attack/defense. Use emoji, no markdown **. Brief and factual.",
        "tr": f"İki takımı karşılaştır: {text}. Form (son 5 maç), karşılıklı maçlar (son 5), güçlü/zayıf yönler, xG istatistikleri. Emoji kullan, markdown ** kullanma. Kısa ve öz.",
        "kz": f"Екі команданы салыстыр: {text}. Форма (соңғы 5 матч), бетпе-бет кездесулер (соңғы 5), күшті/әлсіз жақтар, xG статистикасы. Emoji қолдан, markdown ** жоқ. Қысқа.",
        "uz": f"Ikkita jamoani solishtirish: {text}. Shakl (oxirgi 5 o'yin), to'g'ridan-to'g'ri uchrashuvlar (oxirgi 5), kuchli/zaif tomonlar, xG statistikasi. Emoji ishlatish, markdown ** yo'q. Qisqa.",
        "ar": f"قارن بين فريقين: {text}. الشكل (آخر 5 مباريات)، المواجهات المباشرة (آخر 5)، نقاط القوة والضعف، إحصاءات xG. استخدم emoji، بدون markdown **. موجز.",
    }
    prompt = compare_prompts.get(lang, compare_prompts["ru"])

    try:
        resp = await _create_with_retry(
            model="claude-haiku-4-5-20251001", max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        reply = resp.content[0].text
    except Exception:
        reply = tr(uid, "api_error")

    await context.bot.send_message(chat_id=uid, text=reply)
    return True
