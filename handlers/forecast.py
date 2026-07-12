import asyncio
import base64
import logging
from datetime import datetime, timedelta, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import reg_step, violations, SPAM_DUR, SPAM_AFTER, APIFOOTBALL_KEY
from db import (db_ensure, db_get, db_lang, db_is_reg, db_is_blocked, db_log_req,
                db_save_history, db_get_tz)
from translations import T, tr
from security import uinfo, sec_blocked, rate_check, record_viol, detect_injection
from claude_client import claude_forecast
from football_api import search_match, fetch_real_data
from enrichment import enrich_football_match
from match_validation import MatchRef, validate_match
from event_list import (
    normalize_fixture, select_visible, group_by_sport, group_by_league,
    MAX_LEAGUES, MAX_MATCHES_PER_LEAGUE,
)
from mostbet import (
    _mostbet_load_matches, _is_within_week,
    mostbet_find_match, mostbet_get_odds, format_mostbet_odds,
)
from handlers.utils import _sport_emoji, LANG_BTN, lang_kb, cb_guard, cb_release
from handlers.registration import handle_name

logger = logging.getLogger(__name__)
sus = logging.getLogger("suspicious")

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB cap on uploaded images

# ─── Localized UI strings ─────────────────────────────────────────────────────
_THINKING = {
    "ru": "⏳ Анализирую...", "az": "⏳ Analiz edilir...",
    "en": "⏳ Analysing...", "tr": "⏳ Analiz ediliyor...",
    "kz": "⏳ Талдау жасалуда...", "uz": "⏳ Tahlil qilinmoqda...",
    "ar": "⏳ جارٍ التحليل...",
}
_SPORT_TITLE = {
    "ru": "🏟 Выберите вид спорта:", "az": "🏟 İdman növünü seçin:",
    "en": "🏟 Choose sport:", "tr": "🏟 Spor seçin:",
    "kz": "🏟 Спорт түрін таңдаңыз:", "uz": "🏟 Sport turini tanlang:",
    "ar": "🏟 اختر الرياضة:",
}


def _loc(d: dict, lang: str) -> str:
    """Pick a localized string from a dict, falling back to Russian."""
    return d.get(lang, d["ru"])


# Mostbet lineCategory values that mean association football (enrichment scope).
_FOOTBALL_SPORTS = {"football", "soccer", "futbol"}

# Enrichment block name (from EnrichmentResult.missing_fields) → localized note
# shown to the user when a verified fixture is missing that block.
_ENRICH_GAP_KEYS = {
    "standings": "enr_standings_unavailable",
    "lineups": "enr_lineups_unavailable",
    "injuries": "enr_injuries_unavailable",
}


def _enrichment_gap_note(uid: int, missing_fields: list) -> str | None:
    """Honest localized note listing which verified blocks are unavailable.
    Only the user-facing blocks (standings/lineups/injuries) are surfaced;
    recent/H2H/stats gaps are already stated in the analysis itself."""
    seen, lines = set(), []
    for name in missing_fields:
        key = _ENRICH_GAP_KEYS.get(name)
        if key and key not in seen:
            seen.add(key)
            lines.append(tr(uid, key))
    return "\n".join(lines) if lines else None


def _pick_watch_candidate(candidates: list, ref: dict | None) -> dict | None:
    """Choose a live/today fixture to attach a watch button to, validating each
    candidate against the requested match so we never attach a DIFFERENT match's
    fixture id (which would then drive live tracking and odds alerts). Without a
    reference (e.g. photo flow) preserve the previous first-hit behavior."""
    if not candidates:
        return None
    if not ref:
        return candidates[0]
    requested = MatchRef(home=ref.get("home", ""), away=ref.get("away", ""),
                         is_live=ref.get("is_live"))
    for c in candidates:
        cand = MatchRef(home=c.get("home", ""), away=c.get("away", ""),
                        is_live=c.get("live"))
        if validate_match(requested, cand).usable:
            return c
    return None


# Pagination caps (from event_list): 15 leagues, 10 matches per league.
_LEAGUE_LIMIT = MAX_LEAGUES
_MATCH_LIMIT = MAX_MATCHES_PER_LEAGUE


def _user_tz(uid: int) -> timezone:
    """The user's timezone (from their stored offset). Display uses this; all
    internal comparisons stay in UTC."""
    return timezone(timedelta(hours=db_get_tz(uid) or 0))


def _fmt_kickoff(dt_utc, uid: int) -> str:
    """Format a tz-aware UTC kickoff in the user's local timezone."""
    if dt_utc is None:
        return ""
    off = db_get_tz(uid) or 0
    local = dt_utc.astimezone(timezone(timedelta(hours=off)))
    sign = "+" if off >= 0 else "-"
    return local.strftime("%d.%m %H:%M") + f" (UTC{sign}{abs(off)})"


async def _expired_menu(q, uid: int) -> None:
    """Shown when a keyboard's snapshot is gone or an index is stale/invalid, so
    an index from an old keyboard can never silently resolve to another event."""
    await q.edit_message_text(tr(uid, "ev_menu_expired"))


def _match_label(it, uid: int) -> str:
    """Button label: live state or localized day/time, then the teams."""
    if it.is_live or it.status == "live":
        prefix = "🔴 LIVE"
    else:
        t = _fmt_kickoff(it.kickoff_utc, uid)
        prefix = ("⏸ " + t) if it.postponed else t
    return f"{prefix}  {it.home[:18]} — {it.away[:18]}".strip()


def _build_sport_kb(sport_groups: list) -> InlineKeyboardMarkup:
    """Top-level sport selector from the frozen ordered [(sport, items)] list."""
    btns = []
    for i, (cat, items) in enumerate(sport_groups[:8]):
        emoji = _sport_emoji(cat)
        btns.append([InlineKeyboardButton(f"{emoji} {cat} ({len(items)})",
                                          callback_data=f"fm_sp_{i}")])
    return InlineKeyboardMarkup(btns)


# Country/region (lineSuperCategory, English) → flag emoji. Falls back to 🏆.
_COUNTRY_FLAG = {
    "england": "🏴", "spain": "🇪🇸", "germany": "🇩🇪", "italy": "🇮🇹",
    "france": "🇫🇷", "netherlands": "🇳🇱", "portugal": "🇵🇹", "belgium": "🇧🇪",
    "turkey": "🇹🇷", "russia": "🇷🇺", "ukraine": "🇺🇦", "scotland": "🏴",
    "greece": "🇬🇷", "austria": "🇦🇹", "switzerland": "🇨🇭", "poland": "🇵🇱",
    "denmark": "🇩🇰", "norway": "🇳🇴", "sweden": "🇸🇪", "czech republic": "🇨🇿",
    "croatia": "🇭🇷", "serbia": "🇷🇸", "romania": "🇷🇴", "hungary": "🇭🇺",
    "ireland": "🇮🇪", "wales": "🏴", "finland": "🇫🇮", "bulgaria": "🇧🇬",
    "usa": "🇺🇸", "united states": "🇺🇸", "mexico": "🇲🇽", "brazil": "🇧🇷",
    "argentina": "🇦🇷", "chile": "🇨🇱", "colombia": "🇨🇴", "uruguay": "🇺🇾",
    "japan": "🇯🇵", "south korea": "🇰🇷", "china": "🇨🇳", "australia": "🇦🇺",
    "saudi arabia": "🇸🇦", "qatar": "🇶🇦", "uae": "🇦🇪", "egypt": "🇪🇬",
    "morocco": "🇲🇦", "azerbaijan": "🇦🇿", "kazakhstan": "🇰🇿", "uzbekistan": "🇺🇿",
    "georgia": "🇬🇪", "israel": "🇮🇱", "iran": "🇮🇷", "india": "🇮🇳",
    "south africa": "🇿🇦", "nigeria": "🇳🇬", "ecuador": "🇪🇨", "peru": "🇵🇪",
    "paraguay": "🇵🇾", "bolivia": "🇧🇴", "venezuela": "🇻🇪", "canada": "🇨🇦",
    "slovakia": "🇸🇰", "slovenia": "🇸🇮", "cyprus": "🇨🇾", "iceland": "🇮🇸",
    # Regions / international
    "international": "🌍", "world": "🌍", "europe": "🇪🇺", "europa": "🇪🇺",
    "south america": "🌎", "asia": "🌏", "africa": "🌍", "north america": "🌎",
    "club friendlies": "🤝", "friendlies": "🤝",
}


def _country_flag(country: str) -> str:
    return _COUNTRY_FLAG.get((country or "").strip().lower(), "🏆")


def _build_league_kb(groups: list) -> InlineKeyboardMarkup:
    """Tournament selector from the frozen ordered LeagueGroup list. Names are
    shown as Mostbet supplies them; a country flag aids scanning. `groups` is
    already priority-sorted and capped, and each button index resolves against
    this exact stored list."""
    btns = []
    for i, g in enumerate(groups):
        flag = _country_flag(g.country or "")
        label = f"{flag} {g.league_name}"
        if g.country and flag == "🏆" and g.country.lower() not in g.league_name.lower():
            label += f" · {g.country}"
        label += f" ({len(g.items)})"
        btns.append([InlineKeyboardButton(label, callback_data=f"fm_lg_{i}")])
    btns.append([InlineKeyboardButton("◀️ Назад", callback_data="fm_back_sport")])
    return InlineKeyboardMarkup(btns)


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
    # Quality directive (English — followed regardless of output language).
    # Overrides the base "12 lines max" rule: produce a richer, well-structured
    # analysis using the real data we now provide. Write in the user's language.
    lang_name = {
        "ru": "Russian", "az": "Azerbaijani", "en": "English", "tr": "Turkish",
        "kz": "Kazakh", "uz": "Uzbek", "ar": "Arabic",
    }.get(lang, "Russian")
    sys_prompt += (
        f"\n\n### OUTPUT LANGUAGE = {lang_name}. The ENTIRE reply — section labels "
        f"AND every team / country / player name — MUST be written in {lang_name}. "
        f"Translate names too: e.g. Germany→(Almaniya/Германия), Norway→(Norveç/Норвегия), "
        f"Ivory Coast→(Fil Dişi Sahili/Кот-д'Ивуар). NEVER output an English word if "
        f"{lang_name} is not English. The labels below are written in English ONLY to "
        f"tell you what to include — you MUST translate each label into {lang_name}.\n"
        "Extend the format with these sections (emojis stay, no markdown except the bet line):\n"
        "[📋 recent matches] — when REAL DATA is provided, list each team's last 5 "
        "results (date, teams, score) under the localized team name; skip if no real data.\n"
        "[🔑 key factor] — 1–2 sentences on the single biggest factor.\n"
        "[🩹 injuries/absences] — list key missing players ONLY if they appear in the "
        "provided data. If the data marks injuries as unavailable or does not include "
        "them, write that injury data is unavailable — NEVER claim a team has no "
        "injuries/absences when the feed provided no information.\n"
        "[📈 form] — one line per team: trend + avg total goals/match, using ONLY the "
        "provided computed metrics; if no data, write that form data is unavailable.\n"
        "[💎 value verdict] — compare your probability vs odds-implied (1/odd); is there value?\n"
        "[🔢 exact score] — most likely final score + one alternative.\n"
        "TONE: write in a formal, professional analytical register — like a serious "
        "betting-analyst report. No slang, no casual or chatty phrasing, no emojis "
        "inside sentences (only the section-label emojis). Use complete, precise, "
        "neutral sentences.\n"
        "Think carefully, ground everything in the provided data, ~18-24 lines."
    )

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
            "ru": "\n\nДАННЫЕ: Реальные данные о матчах, форме, травмах и статистике НЕ предоставлены. НЕ придумывай результаты, форму, травмы, составы или статистику. Честно укажи, что данные недоступны, и строй анализ только на коэффициентах (если они есть) и общих тактических соображениях, без конкретных вымышленных фактов.",
            "az": "\n\nMƏLUMAT: Matç, forma, zədə və statistika üzrə real məlumat VERİLMƏYİB. Nəticələri, formanı, zədələri, heyəti və ya statistikanı UYDURMA. Məlumatın mövcud olmadığını açıq yaz və analizi yalnız keflərə (varsa) və ümumi taktiki mülahizələrə əsaslandır.",
            "en": "\n\nDATA: No real data on matches, form, injuries or statistics was provided. Do NOT invent results, form, injuries, lineups or statistics. State honestly that the data is unavailable and base the analysis only on the odds (if any) and general tactical reasoning — no specific fabricated facts.",
            "tr": "\n\nVERİ: Maç, form, sakatlık ve istatistik hakkında gerçek veri SAĞLANMADI. Sonuçları, formu, sakatlıkları, kadroyu veya istatistiği UYDURMA. Verinin mevcut olmadığını dürüstçe belirt ve analizi yalnızca oranlara (varsa) ve genel taktik değerlendirmeye dayandır.",
            "kz": "\n\nДЕРЕК: Матч, форма, жарақат және статистика бойынша нақты дерек БЕРІЛМЕДІ. Нәтижелерді, форманы, жарақаттарды, құрамды немесе статистиканы ОЙДАН ШЫҒАРМА. Деректің қолжетімсіз екенін шыншыл көрсет және талдауды тек коэффициенттерге (болса) және жалпы тактикалық пайымдауға негізде.",
            "uz": "\n\nMA'LUMOT: O'yin, forma, jarohat va statistika bo'yicha haqiqiy ma'lumot BERILMAGAN. Natijalar, forma, jarohatlar, tarkib yoki statistikani O'YLAB TOPMA. Ma'lumot mavjud emasligini rostini yoz va tahlilni faqat koeffitsientlar (bo'lsa) va umumiy taktik mulohazaga asosla.",
            "ar": "\n\nالبيانات: لم تُقدَّم بيانات حقيقية عن المباريات أو الشكل أو الإصابات أو الإحصائيات. لا تختلق نتائج أو شكلاً أو إصابات أو تشكيلات أو إحصائيات. اذكر بصدق أن البيانات غير متوفرة وابنِ التحليل فقط على الأرباح (إن وُجدت) والاعتبارات التكتيكية العامة دون وقائع مُختلقة.",
        }
        sys_prompt += no_data_note.get(lang, no_data_note["ru"])

    # Fetch Mostbet odds for text-based queries. In the menu flow fm_match_cb has
    # already attached odds for this exact match, so guard against a second
    # (duplicate) injection — the fuzzy re-lookup here is only for other flows.
    parsed_teams = context.user_data.get("parsed_teams")
    odds_attached = context.user_data.pop("odds_attached", False)
    mb_match = None
    if parsed_teams and not odds_attached:
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

    reply = await claude_forecast(uid, msg_content, sys_prompt, 1400)
    logger.info(f"FORECAST OK | uid={uid}")

    watch_kb = None
    if text:
        from config import APIFOOTBALL_KEY
        if APIFOOTBALL_KEY:
            ms = await search_match(" ".join(text.split()[:3]))
            m = _pick_watch_candidate(ms, context.user_data.get("match_ref"))
            if m:
                context.user_data[f"mn_{m['id']}"] = m["name"]
                mb_line_id = context.user_data.get("pending_mostbet_line_id")
                if not mb_line_id and mb_match:
                    mb_line_id = str(mb_match.get("id") or "")
                if mb_line_id:
                    context.user_data[f"mb_line_{m['id']}"] = mb_line_id
                watch_kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                    tr(uid, "watch_btn") + f": {m['name'][:35]}",
                    callback_data=f"watch_{m['id']}")]])

    db_save_history(uid, text, reply)

    # Append the honest enrichment note (unverified fixture, or missing verified
    # blocks) so the user sees exactly what real data was / was not available.
    note = context.user_data.pop("enrichment_note", None)
    if note:
        reply = f"{reply}\n\n{note}"

    final_kb = watch_kb
    await status_msg.edit_text(reply, reply_markup=final_kb)


async def forecast_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stub kept for any old inline buttons still in user chats."""
    q = update.callback_query
    if not await cb_guard(update):  # triggers Claude → same limits as text
        return
    await q.answer()
    try:
        await _generate_forecast(q.from_user.id, context, q.message)
    finally:
        cb_release(q.from_user.id)


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
    if not all_m:
        # Empty feed = provider failure (network/429), not "no matches".
        await msg.edit_text(tr(uid, "ev_provider_unavailable")); return

    now_utc = datetime.now(timezone.utc)
    items = select_visible(
        [it for m in all_m if (it := normalize_fixture(m)) is not None],
        now_utc, _user_tz(uid), include_later=False)

    if not items:
        # Nothing LIVE / today / tomorrow — all three buckets are empty.
        await msg.edit_text("\n".join(
            [tr(uid, "ev_no_live"), tr(uid, "ev_no_today"), tr(uid, "ev_no_tomorrow")]))
        return

    sport_groups = group_by_sport(items)
    # Start a new event-list session: freeze this snapshot and invalidate the
    # deeper screens so an old league/match keyboard can never resolve against a
    # newly-built list (it hits a missing snapshot → expired-menu message).
    context.user_data["ev_session"] = context.user_data.get("ev_session", 0) + 1
    context.user_data["fm_sports"] = sport_groups
    context.user_data["fm_leagues"] = None
    context.user_data["fm_matches"] = None

    await msg.edit_text(_loc(_SPORT_TITLE, lang), reply_markup=_build_sport_kb(sport_groups))


def _tournaments_title(sport_name: str, lang: str) -> str:
    title = {
        "ru": f"Турниры — {sport_name}:", "az": f"Turnirler — {sport_name}:",
        "en": f"Tournaments — {sport_name}:", "tr": f"Turnuvalar — {sport_name}:",
    }
    return title.get(lang, title["ru"])


async def fm_sport_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; lang = db_lang(uid)
    sport_groups = context.user_data.get("fm_sports")
    idx = int(q.data.split("_")[2])
    if not sport_groups or idx >= len(sport_groups):
        await _expired_menu(q, uid); return

    sport_name, sport_items = sport_groups[idx]
    context.user_data["fm_sport_idx"] = idx

    groups, leagues_truncated = group_by_league(sport_items)
    # Freeze the exact ordered league list this keyboard is built from; the
    # match screen is invalidated until a league is chosen.
    context.user_data["fm_leagues"] = groups
    context.user_data["fm_matches"] = None

    title = _tournaments_title(sport_name, lang)
    if leagues_truncated:
        title += "\n" + tr(uid, "ev_more_leagues")
    await q.edit_message_text(title, reply_markup=_build_league_kb(groups))


async def fm_league_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; lang = db_lang(uid)
    groups = context.user_data.get("fm_leagues")
    idx = int(q.data.split("_")[2])
    if not groups or idx >= len(groups):
        await _expired_menu(q, uid); return

    g = groups[idx]
    context.user_data["fm_league_idx"] = idx
    # g.items is already kickoff-sorted and capped to _MATCH_LIMIT; freeze it as
    # the exact snapshot this keyboard's fm_mt_ indices resolve against.
    matches = g.items
    context.user_data["fm_matches"] = matches

    btns = [[InlineKeyboardButton(_match_label(it, uid), callback_data=f"fm_mt_{i}")]
            for i, it in enumerate(matches)]
    btns.append([InlineKeyboardButton("◀️ Назад", callback_data="fm_back_league")])

    title = {
        "ru": f"Матчи — {g.league_name}:", "az": f"Matçlar — {g.league_name}:",
        "en": f"Matches — {g.league_name}:", "tr": f"Maçlar — {g.league_name}:",
    }.get(lang)
    title = title or f"Матчи — {g.league_name}:"
    if g.truncated:
        title += "\n" + tr(uid, "ev_more_matches")
    await q.edit_message_text(title, reply_markup=InlineKeyboardMarkup(btns))


async def fm_match_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id; lang = db_lang(uid)
    matches = context.user_data.get("fm_matches")
    idx = int(q.data.split("_")[2])
    if not matches or idx >= len(matches):
        # Stale/expired keyboard — cheap path, not charged against the limit.
        await q.answer()
        await _expired_menu(q, uid); return

    # Everything below costs money (Mostbet odds + enrichment + Opus). Apply
    # the same limits as text input plus a per-user in-flight lock; cb_guard
    # answers the query itself on refusal so the spinner never hangs.
    if not await cb_guard(update):
        return
    await q.answer()
    try:
        await _fm_match_run(context, q, uid, lang, matches[idx])
    finally:
        cb_release(uid)


async def _fm_match_run(context, q, uid: int, lang: str, it) -> None:
    """Expensive body of fm_match_cb; the caller holds the in-flight slot."""
    t1     = it.home
    t2     = it.away
    mid    = it.fixture_id            # authoritative provider fixture id
    league = it.league_name
    league_raw = league  # keep raw tournament name for data-source mapping
    country = it.country or ""
    flag = _country_flag(country)
    if country and flag == "🏆" and country.lower() not in league.lower():
        league = f"{league} · {country}"
    league = f"{flag} {league}".strip()
    dt_str = "🔴 LIVE" if it.is_live else _fmt_kickoff(it.kickoff_utc, uid)

    loading = {
        "ru": "⏳ Загружаю коэффициенты...", "az": "⏳ Əmsallar yüklənir...",
        "en": "⏳ Loading odds...", "tr": "⏳ Oranlar yükleniyor...",
        "kz": "⏳ Коэффициенттер жүктелуде...", "uz": "⏳ Koeffitsientlar yuklanmoqda...",
        "ar": "⏳ جارٍ تحميل الأرباح...",
    }
    await q.edit_message_text(loading.get(lang, "⏳"))

    content = [{"type": "text", "text": f"Match: {t1} vs {t2} | Tournament: {league} | Date: {dt_str}"}]

    odds_task = asyncio.create_task(mostbet_get_odds(mid)) if mid else None
    # Competition name lives in lineSuperCategory ("World Cup 2026"), the stage
    # ("Round of 32") in lineSubCategory — pass both so the mapping finds it.
    league_hint = f"{country} {league_raw}".strip()

    # Football matches get VERIFIED API-Football enrichment (HIGH-confidence
    # fixture only). Everything else keeps the existing provider path. Mostbet
    # remains the source of the event and the odds regardless.
    is_football = (it.sport or "").strip().lower() in _FOOTBALL_SPORTS
    enr_task = real_data_task = None
    if is_football and APIFOOTBALL_KEY:
        enr_task = asyncio.create_task(enrich_football_match(
            line_id=str(mid or ""), home=t1, away=t2, kickoff=it.kickoff_utc,
            league=league_hint, is_live=it.is_live))
    else:
        real_data_task = asyncio.create_task(fetch_real_data(t1, t2, league_hint))

    mb_odds = await odds_task if odds_task else {}
    real_data = ""
    context.user_data.pop("enrichment_note", None)
    if enr_task is not None:
        try:
            enr = await enr_task
        except Exception as e:  # provider failure must never break the forecast
            logger.error(f"enrichment failed uid={uid}: {e}")
            enr = None
        if enr is not None and enr.verified:
            real_data = enr.prompt_text()
            note = _enrichment_gap_note(uid, enr.missing_fields)
            if note:
                context.user_data["enrichment_note"] = note
        else:
            # No verified fixture → keep odds, no LLM factual fallback, be honest.
            context.user_data["enrichment_note"] = tr(uid, "enr_football_unavailable")
    elif real_data_task is not None:
        real_data = await real_data_task

    if mb_odds:
        odds_str = format_mostbet_odds(mb_odds, lang)
        if odds_str:
            content.append({"type": "text", "text": odds_str})

    context.user_data["parsed_teams"] = (t1, t2)
    # Odds for this exact match are already in `content`; tell _generate_forecast
    # not to re-fetch and inject them a second time (duplicate-odds fix).
    context.user_data["odds_attached"] = True
    # Deterministic reference for validating any live fixture we later attach.
    context.user_data["match_ref"] = {
        "home": t1, "away": t2, "is_live": it.is_live,
    }
    if real_data:
        content.append({"type": "text", "text": real_data})
        context.user_data["has_real_data"] = True
    else:
        context.user_data["has_real_data"] = False

    context.user_data["pending_content"] = content
    context.user_data["pending_text"] = f"{t1} {t2}"
    context.user_data["pending_mostbet_line_id"] = str(mid) if mid else ""

    header = f"🏆 {t1} — {t2}\n📍 {league}"
    if dt_str: header += f"\n🕐 {dt_str}"
    status_msg = await context.bot.send_message(
        chat_id=uid, text=header + f"\n\n{_loc(_THINKING, lang)}")
    await context.bot.send_chat_action(chat_id=uid, action="typing")
    await _generate_forecast(uid, context, status_msg)


async def fm_back_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; lang = db_lang(uid)

    if q.data == "fm_back_sport":
        sport_groups = context.user_data.get("fm_sports")
        if not sport_groups:
            await _expired_menu(q, uid); return
        await q.edit_message_text(_loc(_SPORT_TITLE, lang), reply_markup=_build_sport_kb(sport_groups))

    elif q.data == "fm_back_league":
        groups = context.user_data.get("fm_leagues")
        if not groups:
            await _expired_menu(q, uid); return
        idx = context.user_data.get("fm_sport_idx", 0)
        sport_groups = context.user_data.get("fm_sports") or []
        sport_name = sport_groups[idx][0] if idx < len(sport_groups) else ""
        title = _tournaments_title(sport_name, lang)
        await q.edit_message_text(title, reply_markup=_build_league_kb(groups))


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
    if text == LANG_BTN:
        await update.message.reply_text(tr(uid, "choose_lang"), reply_markup=lang_kb())
        return

    # Security
    blk, secs = sec_blocked(uid)
    if blk:
        sus.warning(f"BLK | {info}")
        await update.message.reply_text(tr(uid, "blocked", m=secs//60, s=secs%60)); return
    exceeded, wait = rate_check(uid)
    if exceeded:
        if record_viol(uid, info):
            await update.message.reply_text(tr(uid, "auto_blocked", min=SPAM_DUR//60))
        else:
            await update.message.reply_text(
                tr(uid, "rate_limit", w=wait, v=violations[uid], max=SPAM_AFTER))
        return
    violations[uid] = 0

    mtype = "PHOTO" if update.message.photo else "TEXT"
    logger.info(f"MSG [{mtype}] | {info}")
    db_log_req(uid, mtype)
    await update.message.chat.send_action("typing")

    photo = update.message.photo
    if len(text) > 1000:
        sus.warning(f"LONG | {info}")
        await update.message.reply_text(tr(uid, "long_text")); return
    if detect_injection(text):
        sus.warning(f"INJ | {info} | text={text[:120]!r}")
        if record_viol(uid, info):
            await update.message.reply_text(tr(uid, "auto_blocked", min=SPAM_DUR//60))
        else:
            await update.message.reply_text(tr(uid, "injection"))
        return

    # Compare handler — AFTER the full security gate above (blocked, rate,
    # length, injection): compare text reaches Claude, so it must never bypass
    # the same checks ordinary forecast text goes through.
    if context.user_data.get("awaiting_compare"):
        from handlers.express import handle_compare
        if await handle_compare(uid, text, context): return

    if photo:
        # Photo analysis - send directly to Claude
        largest = photo[-1]
        if (largest.file_size or 0) > MAX_IMAGE_BYTES:
            __import__('logging').getLogger("suspicious").warning(f"BIGIMG | {info} | {largest.file_size}b")
            await update.message.reply_text(tr(uid, "img_too_big")); return
        try:
            f = await context.bot.get_file(largest.file_id)
            fb = await f.download_as_bytearray()
        except Exception as e:
            logger.error(f"photo download error uid={uid}: {e}")
            await update.message.reply_text(tr(uid, "api_error")); return
        if len(fb) > MAX_IMAGE_BYTES:
            await update.message.reply_text(tr(uid, "img_too_big")); return
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
             "data": base64.standard_b64encode(fb).decode("utf-8")}},
            {"type": "text", "text": tr(uid, "img_prompt")},
        ]
        context.user_data["pending_content"] = content
        context.user_data["pending_text"] = ""
        context.user_data["parsed_teams"] = None
        context.user_data["match_ref"] = None
        context.user_data["has_real_data"] = False
        status_msg = await update.message.reply_text(_loc(_THINKING, lang))
        await _generate_forecast(uid, context, status_msg)
        return

    # Text input - redirect to match menu
    use_menu = {
        "ru": "📋 Выберите матч из списка для получения точного прогноза:",
        "az": "📋 Dəqiq proqnoz üçün siyahıdan matç seçin:",
        "en": "📋 Select a match from the list for an accurate forecast:",
        "tr": "📋 Doğru tahmin için listeden maç seçin:",
        "kz": "📋 Нақты болжам алу үшін тізімнен матч таңдаңыз:",
        "uz": "📋 Aniq bashorat olish uchun ro'yxatdan o'yin tanlang:",
        "ar": "📋 اختر مباراة من القائمة للحصول على توقع دقيق:",
    }
    await update.message.reply_text(use_menu.get(lang, use_menu["ru"]))
    await forecast_menu_start(update, context)
