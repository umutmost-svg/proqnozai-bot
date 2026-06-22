import asyncio
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from db import db_is_reg, db_get, db_lang
from translations import T, tr, SPORTS_LABELS, EXP_LABELS
from claude_client import client, request_semaphore
from mostbet import _mostbet_load_matches, _is_within_week

logger = logging.getLogger(__name__)


async def express_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db_is_reg(uid):
        await update.message.reply_text(tr(uid, "need_reg")); return
    lang = db_lang(uid)
    btns = InlineKeyboardMarkup([
        [InlineKeyboardButton("2", callback_data="expr_2"),
         InlineKeyboardButton("3", callback_data="expr_3"),
         InlineKeyboardButton("4", callback_data="expr_4"),
         InlineKeyboardButton("5", callback_data="expr_5")],
    ])
    await update.message.reply_text(T[lang]["express_ask"], reply_markup=btns)


async def express_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; n = int(q.data.split("_")[1])
    lang = db_lang(uid)
    await q.edit_message_text("⏳")
    await context.bot.send_chat_action(chat_id=uid, action="typing")

    u = db_get(uid) or {}
    sports = SPORTS_LABELS.get(lang, SPORTS_LABELS["ru"]).get(u.get("sports", "football"), "Football")
    exp    = EXP_LABELS.get(lang, EXP_LABELS["ru"]).get(u.get("experience", "beginner"), "Beginner")

    mb_matches = await _mostbet_load_matches()
    real_matches_str = ""
    if mb_matches:
        week_matches = [m for m in mb_matches
                        if m.get("isLive") or _is_within_week(m.get("matchBeginAt", ""))]
        selected = week_matches[:n]
        if selected:
            _hdr = {
                "ru": "Реальные матчи из Mostbet:", "az": "Mostbet matçları:",
                "en": "Real matches from Mostbet:", "tr": "Mostbet maçları:",
                "kz": "Mostbet матчтары:", "uz": "Mostbet o'yinlari:", "ar": "مباريات Mostbet:",
            }
            _use = {
                "ru": "Используй ИМЕННО эти матчи для экспресса.",
                "az": "Ekspresdə MƏHZbu matçları istifadə et.",
                "en": "Use ONLY these matches for the express.",
                "tr": "Ekspres için YALNIZCA bu maçları kullan.",
                "kz": "Экспресс үшін ТЕК осы матчтарды қолдан.",
                "uz": "Ekspress uchun FAQAT shu o'yinlarni ishlat.",
                "ar": "استخدم هذه المباريات فقط للرهان المركب.",
            }
            lines_mb = [_hdr.get(lang, _hdr["ru"])]
            for m in selected:
                t1 = m.get("team1Title", "?"); t2 = m.get("team2Title", "?")
                league = m.get("lineSubCategory", "")
                dt = m.get("matchBeginAt", "")[:16]
                lines_mb.append(f"- {t1} vs {t2} | {league} | {dt}")
            real_matches_str = "\n".join(lines_mb) + "\n\n" + _use.get(lang, _use["ru"]) + "\n"

    express_prompts = {
        "ru": f"""{real_matches_str}Составь экспресс на {n} матчей. Правила:\n- Используй только матчи из списка выше (если есть)\n- Для каждого: команды, лучший тип ставки, реалистичный коэффициент\n- Коэффициенты: фаворит 1.20-1.60, равные 2.00-2.80, тотал 1.70-2.10\n- НЕ используй markdown ## ** — только чистый текст и emoji
- В конце посчитай итоговый коэффициент

Формат:
⚽ Матч 1: [Команда А] — [Команда Б]
Ставка: [тип] | Кэф: X.XX
Обоснование: [1 предложение]

⚽ Матч 2: ...

💰 Итог: X.XX × X.XX × X.XX = X.XX

⚠️ Аналитический прогноз.""",
        "az": f"""{real_matches_str}Bu matçlar üçün {n} oyunluq ekspress yarat. Qaydalar:\n- Yuxarıdakı matçları istifadə et (əgər varsa)\n- Hər matç üçün: komandalar, ən yaxşı mərc növü, real kef\n- Keflər: favorit 1.20-1.60, bərabər 2.00-2.80\n- markdown ## ** işlətmə — yalnız mətn və emoji
- Sonunda ümumi kef hesabla

Format:
⚽ Matç 1: [Komanda A] — [Komanda B]
Mərc: [növ] | Kef: X.XX
Səbəb: [1 cümlə]

💰 Nəticə: X.XX × X.XX = X.XX

⚠️ Analitik proqnozdur.""",
        "en": f"""{real_matches_str}Build an express bet with {n} matches. Rules:\n- Use only matches from the list above (if available)\n- For each: teams, best bet type, realistic odds\n- Odds: favorite 1.20-1.60, even 2.00-2.80, total 1.70-2.10\n- NO markdown ## ** — plain text and emoji only
- Calculate total express odds at the end

Format:
⚽ Match 1: [Team A] — [Team B]
Bet: [type] | Odds: X.XX
Reason: [1 sentence]

💰 Total: X.XX × X.XX = X.XX

⚠️ Analytical forecast.""",
        "tr": f"""{real_matches_str}{n} maçlık ekspres oluştur. Kurallar:\n- Yukarıdaki maçları kullan (varsa)\n- Her biri: takımlar, en iyi bahis türü, gerçekçi oran\n- markdown ## ** kullanma — sadece metin ve emoji\n- Sonunda toplam oranı hesapla

Format:
⚽ Maç 1: [Takım A] — [Takım B]
Bahis: [tür] | Oran: X.XX

💰 Toplam: X.XX × X.XX = X.XX

⚠️ Analitik tahmin.""",
        "kz": f"""{real_matches_str}{n} матчтық экспресс жаса. Ережелер:\n- Жоғарыдағы матчтарды қолдан (болса)\n- markdown ## ** жоқ — тек мәтін және emoji\n\nFormat:
⚽ Матч 1: [А] — [Б]
Ставка: [түрі] | Коэф: X.XX

💰 Жалпы: X.XX × X.XX = X.XX""",
        "uz": f"""{real_matches_str}{n} ta o'yin uchun ekspress tuzing. Qoidalar:\n- Yuqoridagi o'yinlarni ishlating (agar bor bo'lsa)\n- markdown ## ** yo'q — faqat matn va emoji\n\nFormat:
⚽ O'yin 1: [A] — [B]
Stavka: [turi] | Koef: X.XX

💰 Jami: X.XX × X.XX = X.XX""",
        "ar": f"""{real_matches_str}أنشئ رهاناً مركباً من {n} مباريات. القواعد:\n- استخدم المباريات من القائمة أعلاه (إن وُجدت)\n- بدون markdown ## ** — نص وإيموجي فقط\n\nالصيغة:
⚽ مباراة 1: [أ] — [ب]
الرهان: [النوع] | الربح: X.XX

💰 الإجمالي: X.XX × X.XX = X.XX""",
    }
    prompt = express_prompts.get(lang, express_prompts["ru"])

    try:
        async with request_semaphore:
            resp = await asyncio.to_thread(
                client.messages.create,
                model="claude-haiku-4-5-20251001", max_tokens=800,
                messages=[{"role": "user", "content": prompt}]
            )
        reply = resp.content[0].text
    except Exception:
        reply = tr(uid, "api_error")

    await context.bot.send_message(chat_id=uid, text=T[lang]["express_title"] + "\n\n" + reply)


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
        async with request_semaphore:
            resp = await asyncio.to_thread(
                client.messages.create,
                model="claude-haiku-4-5-20251001", max_tokens=800,
                messages=[{"role": "user", "content": prompt}]
            )
        reply = resp.content[0].text
    except Exception:
        reply = tr(uid, "api_error")

    await context.bot.send_message(chat_id=uid, text=reply)
    return True
