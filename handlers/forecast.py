import asyncio
import base64
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import reg_step
from db import db_ensure, db_get, db_lang, db_is_reg, db_is_blocked, db_log_req, db_save_history
from translations import T, tr
from security import uinfo, sec_blocked, rate_check, record_viol
from claude_client import claude_forecast
from football_api import search_match, fetch_real_data
from mostbet import (
    _mostbet_load_matches, _is_within_week,
    mostbet_find_match, mostbet_get_odds, format_mostbet_odds,
)
from handlers.utils import main_menu, _sport_emoji, _fmt_dt, fmt_dt_for_user
from handlers.registration import handle_name

logger = logging.getLogger(__name__)


async def _generate_forecast(uid: int, context: ContextTypes.DEFAULT_TYPE, status_msg):
    """Build prompt, call Claude, send reply. status_msg is the '⏳' message to edit."""
    lang = db_lang(uid)
    msg_content = list(context.user_data.get("pending_content") or [])
    text = context.user_data.get("pending_text", "")
    if not msg_content:
        await status_msg.edit_text(tr(uid, "no_input")); return

    u = db_get(uid) or {}
    exp = u.get("experience", "beginner")
    extra_hints = {
        "ru": {"expert": " Profil: ekspert — xG, aziatskie linii.", "mid": " Profil: sredniy — kratko.", "beginner": " Profil: novichok — prosto."},
        "en": {"expert": " Profile: expert — xG, Asian lines.", "mid": " Profile: intermediate — brief.", "beginner": " Profile: beginner — simple."},
        "az": {"expert": " Profil: tecrubell — xG, Asiya xetleri.", "mid": " Profil: orta — qisa.", "beginner": " Profil: yeni — sade."},
    }
    hint = extra_hints.get(lang, extra_hints["ru"]).get(exp, "")
    sys_prompt = tr(uid, "system_prompt") + hint

    if context.user_data.get("has_real_data"):
        data_note = {
            "ru": "\n\nВАЖНО: В запросе есть РЕАЛЬНЫЕ ДАННЫЕ матчей. Используй ТОЛЬКО их для анализа формы и H2H. Не придумывай результаты.",
            "az": "\n\nVACİB: Sorğuda REAL MATÇ VERİLƏRİ var. Formanı YALNIZ bu verilerə əsasən analiz et. Olmayan nəticələri UYDURMA.",
            "en": "\n\nIMPORTANT: REAL MATCH DATA is provided. Use ONLY it for form and H2H. Do not invent results.",
            "tr": "\n\nÖNEMLİ: Gerçek maç verileri sağlandı. Form ve H2H için YALNIZCA bunları kullan. Sonuçları uydurma.",
            "kz": "\n\nМАНЫЗДЫ: Нақты матч деректері бар. Форма мен H2H үшін тек осыларды қолдан. Нәтижелерді ойдан шығарма.",
            "uz": "\n\nMUHIM: Haqiqiy o'yin ma'lumotlari mavjud. Faqat shular asosida forma va H2H tahlili. Natijalarni o'ylab topma.",
            "ar": "\n\nمهم: بيانات المباريات الحقيقية متوفرة. استخدمها فقط لتحليل الشكل والمواجهات. لا تخترع نتائج.",
        }
        sys_prompt += data_note.get(lang, data_note["ru"])
    else:
        no_data_note = {
            "ru": "\n\nФОРМА: Реальные данные НЕ предоставлены. Напиши 'данные о форме недоступны' вместо вымышленных результатов.",
            "az": "\n\nFORMA: Real məlumatlar verilməyib. 'Forma məlumatı əlçatan deyil' yaz — UYDURMА.",
            "en": "\n\nFORM: Real match data NOT provided. Write 'form data unavailable' — do NOT invent results.",
            "tr": "\n\nFORM: Gerçek veri sağlanmadı. 'Form verisi mevcut değil' yaz — UYDURMA.",
            "kz": "\n\nФОРМА: Деректер берілмеді. 'Форма деректері жоқ' деп жаз.",
            "uz": "\n\nSHAKL: Ma'lumotlar yo'q. 'Shakl ma'lumoti mavjud emas' deb yoz.",
            "ar": "\n\nالشكل: لا بيانات. اكتب 'بيانات الشكل غير متوفرة'.",
        }
        sys_prompt += no_data_note.get(lang, no_data_note["ru"])

    # Fetch Mostbet odds for text-based queries
    parsed_teams = context.user_data.get("parsed_teams")
    if parsed_teams:
        t1, t2 = parsed_teams
        mb_match = await mostbet_find_match(t1, t2)
        if mb_match:
            mb_odds = await mostbet_get_odds(mb_match["id"])
            odds_str = format_mostbet_odds(mb_odds, lang)
            if odds_str:
                msg_content.append({"type": "text", "text": odds_str})
                logger.info(f"Mostbet odds OK | uid={uid} match={mb_match.get('matchTitle','?')}")
            elif not _is_within_week(mb_match.get("matchBeginAt", "")):
                msg = T.get(lang, T["ru"]).get("match_too_far", T["ru"]["match_too_far"])
                await status_msg.edit_text(msg); return

    reply = await claude_forecast(uid, msg_content, sys_prompt, 1500)
    logger.info(f"FORECAST OK | uid={uid}")

    watch_kb = None
    if text:
        from config import APIFOOTBALL_KEY
        if APIFOOTBALL_KEY:
            ms = await search_match(" ".join(text.split()[:3]))
            if ms:
                m = ms[0]; context.user_data[f"mn_{m['id']}"] = m["name"]
                watch_kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                    tr(uid, "watch_btn") + f": {m['name'][:35]}",
                    callback_data=f"watch_{m['id']}")]])

    db_save_history(uid, text, reply)

    final_kb = watch_kb
    await status_msg.edit_text(reply, reply_markup=final_kb)


async def forecast_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stub kept for any old inline buttons still in user chats."""
    q = update.callback_query; await q.answer()
    await _generate_forecast(q.from_user.id, context, q.message)


async def forecast_menu_start(update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = db_lang(uid)
    loading = {
        "ru": "⏳ Загружаю матчи...", "az": "⏳ Matçlar yüklənir...",
        "en": "⏳ Loading matches...", "tr": "⏳ Maçlar yükleniyor...",
        "kz": "⏳ Матчтар жүктелуде...", "uz": "⏳ O'yinlar yuklanmoqda...",
        "ar": "⏳ جارٍ تحميل المباريات...",
    }
    msg = await update.message.reply_text(loading.get(lang, "⏳"))

    all_m = await _mostbet_load_matches()
    week_m = [m for m in all_m if m.get("isLive") or _is_within_week(m.get("matchBeginAt", ""))]

    if not week_m:
        no_m = {
            "ru": "Загрузка матчей из Mostbet временно недоступна.\n\nНапишите название матча вручную, например:\nАрсенал ПСЖ",
            "az": "Mostbet matçları müvəqqəti yüklənmir.\n\nMatç adını əl ilə yazın, məsələn:\nArsenal PSJ",
            "en": "Mostbet match loading temporarily unavailable.\n\nType the match manually, e.g.:\nArsenal PSG",
            "tr": "Mostbet maç yüklemesi geçici olarak kullanılamıyor.\n\nMaç adını manuel yazın, örn:\nArsenal PSG",
        }
        await msg.edit_text(no_m.get(lang, no_m["ru"])); return

    sports_map = {}
    for m in week_m:
        cat = (m.get("lineCategory") or "Other").strip()
        sports_map.setdefault(cat, []).append(m)

    context.user_data["fm_sports"] = sports_map
    sport_keys = sorted(sports_map, key=lambda c: -len(sports_map[c]))

    btns = []
    for i, cat in enumerate(sport_keys[:8]):
        emoji = _sport_emoji(cat)
        btns.append([InlineKeyboardButton(f"{emoji} {cat} ({len(sports_map[cat])})",
                                          callback_data=f"fm_sp_{i}")])
    title = {
        "ru": "🏟 Выберите вид спорта:", "az": "🏟 İdman növünü seçin:",
        "en": "🏟 Choose sport:", "tr": "🏟 Spor seçin:",
        "kz": "🏟 Спорт түрін таңдаңыз:", "uz": "🏟 Sport turini tanlang:",
        "ar": "🏟 اختر الرياضة:",
    }
    await msg.edit_text(title.get(lang, title["ru"]), reply_markup=InlineKeyboardMarkup(btns))


async def fm_sport_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; lang = db_lang(uid)
    idx = int(q.data.split("_")[2])
    sports_map = context.user_data.get("fm_sports", {})
    sport_keys = sorted(sports_map, key=lambda c: -len(sports_map[c]))
    if idx >= len(sport_keys):
        await q.edit_message_text("Ошибка."); return

    sport_name = sport_keys[idx]
    context.user_data["fm_sport_idx"] = idx
    leagues_map = {}
    for m in sports_map[sport_name]:
        league = (m.get("lineSubCategory") or "Other").strip()
        leagues_map.setdefault(league, []).append(m)

    league_keys = sorted(leagues_map, key=lambda l: -len(leagues_map[l]))
    context.user_data["fm_leagues"] = leagues_map

    btns = []
    for i, lg in enumerate(league_keys[:10]):
        btns.append([InlineKeyboardButton(f"🏆 {lg} ({len(leagues_map[lg])})",
                                          callback_data=f"fm_lg_{i}")])
    btns.append([InlineKeyboardButton("◀️ Назад", callback_data="fm_back_sport")])

    title = {
        "ru": f"Турниры — {sport_name}:", "az": f"Turnirler — {sport_name}:",
        "en": f"Tournaments — {sport_name}:", "tr": f"Turnuvalar — {sport_name}:",
    }
    await q.edit_message_text(title.get(lang, title["ru"]), reply_markup=InlineKeyboardMarkup(btns))


async def fm_league_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; lang = db_lang(uid)
    idx = int(q.data.split("_")[2])
    leagues_map = context.user_data.get("fm_leagues", {})
    league_keys = sorted(leagues_map, key=lambda l: -len(leagues_map[l]))
    if idx >= len(league_keys):
        await q.edit_message_text("Ошибка."); return

    league_name = league_keys[idx]
    context.user_data["fm_league_idx"] = idx
    matches = sorted(leagues_map[league_name],
                     key=lambda m: (0 if m.get("isLive") else 1, m.get("matchBeginAt", "")))
    context.user_data["fm_matches"] = matches

    btns = []
    for i, m in enumerate(matches[:10]):
        t1 = m.get("team1Title", "?")[:18]
        t2 = m.get("team2Title", "?")[:18]
        prefix = "🔴 LIVE" if m.get("isLive") else fmt_dt_for_user(m.get("matchBeginAt", ""), uid)
        btns.append([InlineKeyboardButton(f"{prefix}  {t1} — {t2}", callback_data=f"fm_mt_{i}")])
    btns.append([InlineKeyboardButton("◀️ Назад", callback_data="fm_back_league")])

    title = {
        "ru": f"Матчи — {league_name}:", "az": f"Matçlar — {league_name}:",
        "en": f"Matches — {league_name}:", "tr": f"Maçlar — {league_name}:",
    }
    await q.edit_message_text(title.get(lang, title["ru"]), reply_markup=InlineKeyboardMarkup(btns))


async def fm_match_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; lang = db_lang(uid)
    idx = int(q.data.split("_")[2])
    matches = context.user_data.get("fm_matches", [])
    if idx >= len(matches):
        await q.edit_message_text("Ошибка."); return

    m = matches[idx]
    t1     = m.get("team1Title", "?")
    t2     = m.get("team2Title", "?")
    mid    = m.get("id")
    league = m.get("lineSubCategory", "")
    dt_str = fmt_dt_for_user(m.get("matchBeginAt", ""), uid)

    loading = {
        "ru": "⏳ Загружаю коэффициенты...", "az": "⏳ Keflər yüklənir...",
        "en": "⏳ Loading odds...", "tr": "⏳ Oranlar yükleniyor...",
        "kz": "⏳ Коэффициенттер жүктелуде...", "uz": "⏳ Koeffitsientlar yuklanmoqda...",
        "ar": "⏳ جارٍ تحميل الأرباح...",
    }
    await q.edit_message_text(loading.get(lang, "⏳"))

    content = [{"type": "text", "text": f"Match: {t1} vs {t2} | Tournament: {league} | Date: {dt_str}"}]

    odds_task = asyncio.create_task(mostbet_get_odds(mid)) if mid else None
    real_data_task = asyncio.create_task(fetch_real_data(t1, t2))

    mb_odds = await odds_task if odds_task else {}
    real_data = await real_data_task

    if mb_odds:
        odds_str = format_mostbet_odds(mb_odds, lang)
        if odds_str:
            content.append({"type": "text", "text": odds_str})

    context.user_data["parsed_teams"] = (t1, t2)
    if real_data:
        content.append({"type": "text", "text": real_data})
        context.user_data["has_real_data"] = True
    else:
        context.user_data["has_real_data"] = False

    context.user_data["pending_content"] = content
    context.user_data["pending_text"] = f"{t1} {t2}"

    thinking = {
        "ru": "⏳ Анализирую...", "az": "⏳ Analiz edilir...",
        "en": "⏳ Analysing...", "tr": "⏳ Analiz ediliyor...",
        "kz": "⏳ Талдау жасалуда...", "uz": "⏳ Tahlil qilinmoqda...",
        "ar": "⏳ جارٍ التحليل...",
    }
    header = f"🏆 {t1} — {t2}\n📍 {league}"
    if dt_str: header += f"\n🕐 {dt_str}"
    status_msg = await context.bot.send_message(
        chat_id=uid, text=header + f"\n\n{thinking.get(lang, '⏳')}")
    await context.bot.send_chat_action(chat_id=uid, action="typing")
    await _generate_forecast(uid, context, status_msg)


async def fm_back_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; lang = db_lang(uid)

    if q.data == "fm_back_sport":
        sports_map = context.user_data.get("fm_sports", {})
        sport_keys = sorted(sports_map, key=lambda c: -len(sports_map[c]))
        btns = []
        for i, cat in enumerate(sport_keys[:8]):
            emoji = _sport_emoji(cat)
            btns.append([InlineKeyboardButton(f"{emoji} {cat} ({len(sports_map[cat])})",
                                              callback_data=f"fm_sp_{i}")])
        title = {
            "ru": "🏟 Выберите вид спорта:", "az": "🏟 İdman növünü seçin:",
            "en": "🏟 Choose sport:", "tr": "🏟 Spor seçin:",
        }
        await q.edit_message_text(title.get(lang, title["ru"]), reply_markup=InlineKeyboardMarkup(btns))

    elif q.data == "fm_back_league":
        idx = context.user_data.get("fm_sport_idx", 0)
        sports_map = context.user_data.get("fm_sports", {})
        sport_keys = sorted(sports_map, key=lambda c: -len(sports_map[c]))
        if idx >= len(sport_keys):
            await q.edit_message_text("Ошибка."); return
        sport_name = sport_keys[idx]
        leagues_map = context.user_data.get("fm_leagues", {})
        league_keys = sorted(leagues_map, key=lambda l: -len(leagues_map[l]))
        btns = []
        for i, lg in enumerate(league_keys[:10]):
            btns.append([InlineKeyboardButton(f"🏆 {lg} ({len(leagues_map[lg])})",
                                              callback_data=f"fm_lg_{i}")])
        btns.append([InlineKeyboardButton("◀️ Назад", callback_data="fm_back_sport")])
        title = {
            "ru": f"Турниры — {sport_name}:", "az": f"Turnirler — {sport_name}:",
            "en": f"Tournaments — {sport_name}:", "tr": f"Turnuvalar — {sport_name}:",
        }
        await q.edit_message_text(title.get(lang, title["ru"]), reply_markup=InlineKeyboardMarkup(btns))


async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; uid = user.id; info = uinfo(update)
    db_ensure(uid, user.username or "", user.language_code)
    text = update.message.text or update.message.caption or ""

    step = reg_step.get(uid)
    if step == "awaiting_name" and update.message.text:
        await handle_name(update, context); return
    if step in ("awaiting_lang", "awaiting_name", "ob_sports", "ob_exp"):
        return

    if not db_is_reg(uid):
        await update.message.reply_text(tr(uid, "need_reg")); return
    if db_is_blocked(uid):
        await update.message.reply_text(tr(uid, "db_blocked")); return

    # Timezone input
    from handlers.registration import handle_tz_input
    if await handle_tz_input(update, context): return

    # Menu routing
    lang = db_lang(uid); tl = T[lang]
    if text == tl["menu_matches"]:
        from handlers.live import matches_cmd
        await matches_cmd(update, context); return
    if text == tl["menu_profile"]:
        from handlers.registration import profile_cmd
        await profile_cmd(update, context); return
    if text == tl["menu_history"]:
        from handlers.history import history_cmd
        await history_cmd(update, context); return
    if text == tl["menu_express"]:
        from handlers.express import express_cmd
        await express_cmd(update, context); return
    if text == tl["menu_forecast"]:
        await forecast_menu_start(update, context); return

    # Compare handler
    if context.user_data.get("awaiting_compare"):
        from handlers.express import handle_compare
        if await handle_compare(uid, text, context): return

    # Security
    blk, secs = sec_blocked(uid)
    if blk:
        __import__('logging').getLogger("suspicious").warning(f"BLK | {info}")
        await update.message.reply_text(tr(uid, "blocked", m=secs//60, s=secs%60)); return
    exceeded, wait = rate_check(uid)
    if exceeded:
        from config import violations
        if record_viol(uid, info):
            await update.message.reply_text(tr(uid, "auto_blocked", min=__import__('config').SPAM_DUR//60))
        else:
            await update.message.reply_text(
                tr(uid, "rate_limit", w=wait, v=violations[uid], max=__import__('config').SPAM_AFTER))
        return
    from config import violations
    violations[uid] = 0

    mtype = "PHOTO" if update.message.photo else "TEXT"
    logger.info(f"MSG [{mtype}] | {info}")
    db_log_req(uid, mtype)
    await update.message.chat.send_action("typing")

    photo = update.message.photo
    if len(text) > 1000:
        __import__('logging').getLogger("suspicious").warning(f"LONG | {info}")
        await update.message.reply_text(tr(uid, "long_text")); return
    inj = ["ignore previous", "system prompt", "forget instructions", "act as", "jailbreak"]
    if any(k.lower() in text.lower() for k in inj):
        __import__('logging').getLogger("suspicious").warning(f"INJ | {info}")
        await update.message.reply_text(tr(uid, "injection")); return

    if photo:
        # Photo analysis - send directly to Claude
        f = await context.bot.get_file(photo[-1].file_id)
        fb = await f.download_as_bytearray()
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
             "data": base64.standard_b64encode(fb).decode("utf-8")}},
            {"type": "text", "text": tr(uid, "img_prompt")},
        ]
        context.user_data["pending_content"] = content
        context.user_data["pending_text"] = ""
        context.user_data["parsed_teams"] = None
        context.user_data["has_real_data"] = False
        thinking = {
            "ru": "⏳ Анализирую...", "az": "⏳ Analiz edilir...",
            "en": "⏳ Analysing...", "tr": "⏳ Analiz ediliyor...",
            "kz": "⏳ Талдау жасалуда...", "uz": "⏳ Tahlil qilinmoqda...",
            "ar": "⏳ جارٍ التحليل...",
        }
        status_msg = await update.message.reply_text(thinking.get(lang, "⏳"))
        await _generate_forecast(uid, context, status_msg)
        return

    # Text input - redirect to Mostbet match menu
    use_menu = {
        "ru": "📋 Выберите матч из списка Mostbet для получения точного прогноза:",
        "az": "📋 Dəqiq proqnoz üçün Mostbet siyahısından matç seçin:",
        "en": "📋 Select a match from the Mostbet list for an accurate forecast:",
        "tr": "📋 Doğru tahmin için Mostbet listesinden maç seçin:",
        "kz": "📋 Нақты болжам алу үшін Mostbet тізімінен матч таңдаңыз:",
        "uz": "📋 Aniq bashorat olish uchun Mostbet ro'yxatidan o'yin tanlang:",
        "ar": "📋 اختر مباراة من قائمة Mostbet للحصول على توقع دقيق:",
    }
    await update.message.reply_text(use_menu.get(lang, use_menu["ru"]))
    await forecast_menu_start(update, context)
