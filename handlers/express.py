import asyncio
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from db import db_is_reg, db_get, db_lang
from translations import T, tr, SPORTS_LABELS, EXP_LABELS
from claude_client import _create_with_retry
from mostbet import (_mostbet_load_matches, _is_within_week, _is_virtual_match,
                     _is_outright_market, mostbet_get_odds, format_odds_compact)
from handlers.utils import _fmt_dt, cb_guard, cb_release

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
    q = update.callback_query
    uid = q.from_user.id
    # Triggers a Claude (Haiku) call — same limits as text input, plus the
    # per-user in-flight lock; cb_guard answers the query itself on refusal.
    if not await cb_guard(update):
        return
    await q.answer()
    try:
        await _express_run(context, q, uid)
    finally:
        cb_release(uid)


async def _express_run(context, q, uid: int) -> None:
    """Expensive body of express_cb; the caller holds the in-flight slot."""
    n = int(q.data.split("_")[1])
    lang = db_lang(uid)
    await q.edit_message_text("⏳")
    await context.bot.send_chat_action(chat_id=uid, action="typing")

    u = db_get(uid) or {}
    sports = SPORTS_LABELS.get(lang, SPORTS_LABELS["ru"]).get(u.get("sports", "football"), "Football")
    exp    = EXP_LABELS.get(lang, EXP_LABELS["ru"]).get(u.get("experience", "beginner"), "Beginner")

    mb_matches = await _mostbet_load_matches()
    week_matches = [m for m in mb_matches
                    if not _is_virtual_match(m) and not _is_outright_market(m)
                    and (m.get("isLive") or _is_within_week(m.get("matchBeginAt", "")))]

    # Fetch REAL odds for the first candidates (a few spares so a match whose
    # line has no core 1X2 market yet can be skipped). The express never
    # invents odds: only matches with a real price are eligible, and with
    # fewer than two priced matches we answer honestly instead of asking the
    # model to fabricate numbers (CLAUDE.md: real odds are passed as data).
    candidates = week_matches[:n + 4]
    odds_list = await asyncio.gather(
        *(mostbet_get_odds(m["id"]) for m in candidates)) if candidates else []
    priced = [(m, o) for m, o in zip(candidates, odds_list) if o.get("w1")]
    selected = priced[:n]

    if len(selected) < 2:
        await q.edit_message_text(tr(uid, "express_no_odds"))
        return
    k = len(selected)  # may be < n when fewer priced matches exist

    _hdr = {
        "ru": "Актуальные матчи с РЕАЛЬНЫМИ коэффициентами Mostbet:",
        "az": "Mostbet-in REAL əmsalları ilə aktual matçlar:",
        "en": "Current matches with REAL Mostbet odds:",
        "tr": "GERÇEK Mostbet oranlarıyla güncel maçlar:",
        "kz": "Mostbet-тің НАҚТЫ коэффициенттері бар матчтар:",
        "uz": "Mostbet-ning HAQIQIY koeffitsientlari bilan o'yinlar:",
        "ar": "المباريات الحالية مع أرباح Mostbet الحقيقية:",
    }
    _use = {
        "ru": "Используй ТОЛЬКО эти матчи и ТОЛЬКО указанные коэффициенты — они реальные.",
        "az": "YALNIZ bu matçları və YALNIZ göstərilən əmsalları istifadə et — onlar realdır.",
        "en": "Use ONLY these matches and ONLY the listed odds — they are real.",
        "tr": "YALNIZCA bu maçları ve YALNIZCA listelenen oranları kullan — bunlar gerçek.",
        "kz": "ТЕК осы матчтарды және ТЕК көрсетілген коэффициенттерді қолдан — олар нақты.",
        "uz": "FAQAT shu o'yinlarni va FAQAT ko'rsatilgan koeffitsientlarni ishlat — ular haqiqiy.",
        "ar": "استخدم هذه المباريات فقط والأرباح المذكورة فقط — إنها حقيقية.",
    }
    lines_mb = [_hdr.get(lang, _hdr["ru"])]
    for m, o in selected:
        t1 = m.get("team1Title", "?"); t2 = m.get("team2Title", "?")
        league = m.get("lineSubCategory", "")
        dt = _fmt_dt(m.get("matchBeginAt", ""))
        lines_mb.append(f"- {t1} vs {t2} | {league} | {dt}\n  {format_odds_compact(o)}")
    real_matches_str = "\n".join(lines_mb) + "\n\n" + _use.get(lang, _use["ru"]) + "\n"

    express_prompts = {
        "ru": f"""{real_matches_str}Составь экспресс на {k} матчей. Правила:\n- Используй ТОЛЬКО матчи из списка выше\n- Для каждого выбери ставку ТОЛЬКО из указанных рынков и приведи её РЕАЛЬНЫЙ коэффициент из списка\n- НИКОГДА не выдумывай коэффициенты или рынки, которых нет в списке\n- НЕ используй markdown ## ** — только чистый текст и emoji
- В конце посчитай итоговый коэффициент (произведение выбранных)

Формат:
⚽ Матч 1: [Команда А] — [Команда Б]
Ставка: [тип] | Кэф: X.XX
Обоснование: [1 предложение]

⚽ Матч 2: ...

💰 Итог: X.XX × X.XX × X.XX = X.XX

⚠️ Аналитический прогноз.""",
        "az": f"""{real_matches_str}Bu matçlar üçün {k} oyunluq ekspress yarat. Qaydalar:\n- YALNIZ yuxarıdakı siyahıdakı matçları istifadə et\n- Hər matç üçün mərci YALNIZ göstərilən bazarlardan seç və onun siyahıdakı REAL əmsalını yaz\n- Siyahıda olmayan əmsal və ya bazar UYDURMA\n- markdown ## ** işlətmə — yalnız mətn və emoji
- Sonunda ümumi əmsalı hesabla (seçilənlərin hasili)

Format:
⚽ Matç 1: [Komanda A] — [Komanda B]
Mərc: [növ] | Əmsal: X.XX
Səbəb: [1 cümlə]

💰 Nəticə: X.XX × X.XX = X.XX

⚠️ Analitik proqnozdur.""",
        "en": f"""{real_matches_str}Build an express with {k} matches. Rules:\n- Use ONLY matches from the list above\n- For each, pick a bet ONLY from the listed markets and quote its REAL odds from the list\n- NEVER invent odds or markets that are not in the list\n- NO markdown ## ** — plain text and emoji only
- Calculate the total odds at the end (product of the picks)

Format:
⚽ Match 1: [Team A] — [Team B]
Bet: [type] | Odds: X.XX
Reason: [1 sentence]

💰 Total: X.XX × X.XX = X.XX

⚠️ Analytical forecast.""",
        "tr": f"""{real_matches_str}{k} maçlık ekspres oluştur. Kurallar:\n- YALNIZCA yukarıdaki listedeki maçları kullan\n- Her maç için bahsi YALNIZCA listelenen marketlerden seç ve GERÇEK oranını listeden yaz\n- Listede olmayan oran veya market UYDURMA\n- markdown ## ** kullanma — sadece metin ve emoji\n- Sonunda toplam oranı hesapla (seçimlerin çarpımı)

Format:
⚽ Maç 1: [Takım A] — [Takım B]
Bahis: [tür] | Oran: X.XX

💰 Toplam: X.XX × X.XX = X.XX

⚠️ Analitik tahmin.""",
        "kz": f"""{real_matches_str}{k} матчтық экспресс жаса. Ережелер:\n- ТЕК жоғарыдағы тізімдегі матчтарды қолдан\n- Әр матч үшін ставканы ТЕК көрсетілген нарықтардан таңдап, тізімдегі НАҚТЫ коэффициентін жаз\n- Тізімде жоқ коэффициент немесе нарық ОЙДАН ШЫҒАРМА\n- markdown ## ** жоқ — тек мәтін және emoji\n\nFormat:
⚽ Матч 1: [А] — [Б]
Ставка: [түрі] | Коэф: X.XX

💰 Жалпы: X.XX × X.XX = X.XX""",
        "uz": f"""{real_matches_str}{k} ta o'yin uchun ekspress tuzing. Qoidalar:\n- FAQAT yuqoridagi ro'yxatdagi o'yinlarni ishlating\n- Har bir o'yin uchun stavkani FAQAT ko'rsatilgan bozorlardan tanlang va ro'yxatdagi HAQIQIY koeffitsientini yozing\n- Ro'yxatda yo'q koeffitsient yoki bozorni O'YLAB TOPMANG\n- markdown ## ** yo'q — faqat matn va emoji\n\nFormat:
⚽ O'yin 1: [A] — [B]
Stavka: [turi] | Koef: X.XX

💰 Jami: X.XX × X.XX = X.XX""",
        "ar": f"""{real_matches_str}أنشئ رهاناً مركباً من {k} مباريات. القواعد:\n- استخدم فقط المباريات من القائمة أعلاه\n- لكل مباراة اختر الرهان فقط من الأسواق المذكورة واذكر ربحه الحقيقي من القائمة\n- لا تخترع أبداً أرباحاً أو أسواقاً غير موجودة في القائمة\n- بدون markdown ## ** — نص وإيموجي فقط\n\nالصيغة:
⚽ مباراة 1: [أ] — [ب]
الرهان: [النوع] | الربح: X.XX

💰 الإجمالي: X.XX × X.XX = X.XX""",
    }
    prompt = express_prompts.get(lang, express_prompts["ru"])

    try:
        resp = await _create_with_retry(
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
        resp = await _create_with_retry(
            model="claude-haiku-4-5-20251001", max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        reply = resp.content[0].text
    except Exception:
        reply = tr(uid, "api_error")

    await context.bot.send_message(chat_id=uid, text=reply)
    return True
