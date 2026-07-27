import asyncio
import base64
import logging
from datetime import date, datetime, timedelta, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import reg_step, violations, SPAM_DUR, SPAM_AFTER, APIFOOTBALL_KEY
from db import (db_ensure, db_get, db_lang, db_is_reg, db_is_blocked, db_log_req,
                db_save_history, db_get_tz, db_match_demand)
from translations import T, tr
from security import uinfo, sec_blocked, rate_check, record_viol, detect_injection
from claude_client import claude_forecast
from football_api import search_match, fetch_real_data
from enrichment import enrich_football_match
from match_validation import MatchRef, validate_match
from event_list import (
    normalize_fixture, select_visible, group_by_sport, group_by_league,
    paginate, PAGE_SIZE,
    available_day_options, filter_by_day, DAY_ALL, DAY_LIVE, DAY_TODAY, DAY_TOMORROW,
    available_countries, filter_by_country, COUNTRY_INTERNATIONAL,
)
from mostbet import (
    _mostbet_load_matches, _is_within_week,
    mostbet_find_match, mostbet_get_odds, format_mostbet_odds,
)
from handlers.utils import _sport_emoji, LANG_BTN, lang_kb, cb_guard, cb_release, nav_guard
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


def _parse_index(data: str) -> int | None:
    """Parse the trailing integer from callback_data (e.g. "fm_day_3" -> 3).
    Returns None on malformed data (a hand-crafted or replayed callback)
    instead of raising, so callers can degrade to the same expired-menu path
    used for an out-of-range index."""
    try:
        return int(data.split("_")[2])
    except (IndexError, ValueError):
        return None


def _match_label(it, uid: int) -> str:
    """Button label: live state or localized day/time, then the teams."""
    if it.is_live or it.status == "live":
        prefix = "🔴 LIVE"
    else:
        t = _fmt_kickoff(it.kickoff_utc, uid)
        prefix = ("⏸ " + t) if it.postponed else t
    return f"{prefix}  {it.home[:18]} — {it.away[:18]}".strip()


def _build_sport_kb(sport_groups: list, page: int, uid: int) -> InlineKeyboardMarkup:
    """Top-level sport selector, one page of the frozen ordered
    [(sport, items)] list. Button indices are the ABSOLUTE position in the
    full list, same rationale as _build_league_kb/_build_match_kb."""
    page_groups, page, has_prev, has_next = paginate(sport_groups, page, PAGE_SIZE)
    offset = page * PAGE_SIZE
    btns = []
    for i, (cat, items) in enumerate(page_groups, start=offset):
        emoji = _sport_emoji(cat)
        btns.append([InlineKeyboardButton(f"{emoji} {cat} ({len(items)})",
                                          callback_data=f"fm_sp_{i}")])
    nav = _pagination_row(uid, page, has_prev, has_next, "fm_sppg_")
    if nav:
        btns.append(nav)
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


def _pagination_row(uid: int, page: int, has_prev: bool, has_next: bool, prefix: str) -> list:
    row = []
    if has_prev:
        row.append(InlineKeyboardButton(tr(uid, "ev_page_prev"), callback_data=f"{prefix}{page-1}"))
    if has_next:
        row.append(InlineKeyboardButton(tr(uid, "ev_page_more"), callback_data=f"{prefix}{page+1}"))
    return row


def _build_league_kb(groups: list, page: int, back_cb: str, uid: int) -> InlineKeyboardMarkup:
    """Tournament selector, one page of the frozen ordered LeagueGroup list.
    Names are shown as Mostbet supplies them; a country flag aids scanning.
    Button indices are the ABSOLUTE position in the full list — pagination
    only changes which slice is shown, never what an index resolves to."""
    page_groups, page, has_prev, has_next = paginate(groups, page, PAGE_SIZE)
    offset = page * PAGE_SIZE
    btns = []
    for i, g in enumerate(page_groups, start=offset):
        flag = _country_flag(g.country or "")
        label = f"{flag} {g.league_name}"
        if g.country and flag == "🏆" and g.country.lower() not in g.league_name.lower():
            label += f" · {g.country}"
        label += f" ({len(g.items)})"
        btns.append([InlineKeyboardButton(label, callback_data=f"fm_lg_{i}")])
    nav = _pagination_row(uid, page, has_prev, has_next, "fm_lgpg_")
    if nav:
        btns.append(nav)
    btns.append([InlineKeyboardButton(tr(uid, "ev_back"), callback_data=back_cb)])
    return InlineKeyboardMarkup(btns)


def _build_match_kb(matches: list, page: int, uid: int) -> InlineKeyboardMarkup:
    """Match selector, one page of the frozen ordered match list. Button
    indices are the ABSOLUTE position in the full list, same rationale as
    _build_league_kb."""
    page_matches, page, has_prev, has_next = paginate(matches, page, PAGE_SIZE)
    offset = page * PAGE_SIZE
    btns = [[InlineKeyboardButton(_match_label(it, uid), callback_data=f"fm_mt_{i}")]
            for i, it in enumerate(page_matches, start=offset)]
    nav = _pagination_row(uid, page, has_prev, has_next, "fm_mtpg_")
    if nav:
        btns.append(nav)
    btns.append([InlineKeyboardButton(tr(uid, "ev_back"), callback_data="fm_back_league")])
    return InlineKeyboardMarkup(btns)


def _day_label(day_key: str, count: int, uid: int) -> str:
    if day_key == DAY_LIVE:
        return f"🔴 LIVE ({count})"
    if day_key == DAY_TODAY:
        return f"{tr(uid, 'ev_day_today')} ({count})"
    if day_key == DAY_TOMORROW:
        return f"{tr(uid, 'ev_day_tomorrow')} ({count})"
    return f"{date.fromisoformat(day_key).strftime('%d.%m')} ({count})"


def _build_day_kb(day_options: list, uid: int) -> InlineKeyboardMarkup:
    """Day filter selector. Index 0 is always the fixed "all days" option;
    index i (1-based) resolves to day_options[i-1] — day_options never needs
    its own pagination (at most LIVE/TODAY/TOMORROW + up to 7 dated days)."""
    btns = [[InlineKeyboardButton(tr(uid, "ev_day_all"), callback_data="fm_day_0")]]
    for i, (key, count) in enumerate(day_options, start=1):
        btns.append([InlineKeyboardButton(_day_label(key, count, uid), callback_data=f"fm_day_{i}")])
    btns.append([InlineKeyboardButton(tr(uid, "ev_back"), callback_data="fm_back_sport")])
    return InlineKeyboardMarkup(btns)


def _build_country_kb(country_options: list, page: int, uid: int) -> InlineKeyboardMarkup:
    """Country/region filter selector, paginated. Index 0 is always the fixed
    "all countries" option (shown only on page 0); index i (1-based) is the
    ABSOLUTE position in the full country_options list."""
    page_opts, page, has_prev, has_next = paginate(country_options, page, PAGE_SIZE)
    offset = page * PAGE_SIZE
    btns = []
    if page == 0:
        btns.append([InlineKeyboardButton(tr(uid, "ev_country_all"), callback_data="fm_ctry_0")])
    for i, (key, count) in enumerate(page_opts, start=offset):
        flag = "🌍" if key == COUNTRY_INTERNATIONAL else _country_flag(key)
        btns.append([InlineKeyboardButton(f"{flag} {key} ({count})", callback_data=f"fm_ctry_{i + 1}")])
    nav = _pagination_row(uid, page, has_prev, has_next, "fm_ctrypg_")
    if nav:
        btns.append(nav)
    btns.append([InlineKeyboardButton(tr(uid, "ev_back"), callback_data="fm_back_day")])
    return InlineKeyboardMarkup(btns)


_LANG_NAME = {
    "ru": "Russian", "az": "Azerbaijani", "en": "English", "tr": "Turkish",
    "kz": "Kazakh", "uz": "Uzbek", "ar": "Arabic",
}

# Experience profile hint — LLM-facing prompt text (not user UI), ru fallback is fine.
_EXP_HINTS = {
    "ru": {"expert": " Profil: ekspert — xG, aziatskie linii.", "mid": " Profil: sredniy — kratko.", "beginner": " Profil: novichok — prosto."},
    "en": {"expert": " Profile: expert — xG, Asian lines.", "mid": " Profile: intermediate — brief.", "beginner": " Profile: beginner — simple."},
    "az": {"expert": " Profil: tecrubell — xG, Asiya xetleri.", "mid": " Profil: orta — qisa.", "beginner": " Profil: yeni — sade."},
}

_DATA_NOTE = {
    "ru": "\n\nВАЖНО: В запросе есть РЕАЛЬНЫЕ ДАННЫЕ матчей. Используй ТОЛЬКО их для анализа формы и H2H. Не придумывай результаты.",
    "az": "\n\nVACİB: Sorğuda REAL MATÇ VERİLƏRİ var. Formanı YALNIZ bu verilerə əsasən analiz et. Olmayan nəticələri UYDURMA.",
    "en": "\n\nIMPORTANT: REAL MATCH DATA is provided. Use ONLY it for form and H2H. Do not invent results.",
    "tr": "\n\nÖNEMLİ: Gerçek maç verileri sağlandı. Form ve H2H için YALNIZCA bunları kullan. Sonuçları uydurma.",
    "kz": "\n\nМАНЫЗДЫ: Нақты матч деректері бар. Форма мен H2H үшін тек осыларды қолдан. Нәтижелерді ойдан шығарма.",
    "uz": "\n\nMUHIM: Haqiqiy o'yin ma'lumotlari mavjud. Faqat shular asosida forma va H2H tahlili. Natijalarni o'ylab topma.",
    "ar": "\n\nمهم: بيانات المباريات الحقيقية متوفرة. استخدمها فقط لتحليل الشكل والمواجهات. لا تخترع نتائج.",
}


def _build_system_prompt(lang: str, exp: str, has_real_data: bool) -> str:
    """Assemble the forecast system prompt. Pure (reads only the static
    translations/hint tables), so it is unit-testable across languages and both
    data modes without touching the network or DB.

    The rich, multi-section format (recent matches / injuries / form breakdown)
    is requested ONLY when real enrichment data is actually attached. Without
    it, requesting those sections merely made the model emit a "data
    unavailable" placeholder per section — several near-identical lines of
    noise. The no-data branch instead asks for a lean, odds-only forecast plus a
    SINGLE estimative marker, while keeping every anti-fabrication directive
    (never invent form/injuries/lineups/results) fully intact.
    """
    base = (T.get(lang) or T["ru"]).get("system_prompt") or T["ru"]["system_prompt"]
    hint = _EXP_HINTS.get(lang, _EXP_HINTS["ru"]).get(exp, "")
    lang_name = _LANG_NAME.get(lang, "Russian")
    sys_prompt = base + hint

    if has_real_data:
        # Quality directive (English — followed regardless of output language).
        # Overrides the base "12 lines max" rule: produce a richer, well-structured
        # analysis using the real data we now provide. Write in the user's language.
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
        sys_prompt += _DATA_NOTE.get(lang, _DATA_NOTE["ru"])
    else:
        # No real data: lean, odds-only format. Omit every data-dependent section
        # instead of printing a "data unavailable" placeholder per section, but
        # keep the honesty guarantee — no invented facts and one estimative marker.
        sys_prompt += (
            f"\n\n### OUTPUT LANGUAGE = {lang_name}. Write the ENTIRE reply in {lang_name} — "
            f"section labels AND every team / country / player name; translate names too, and "
            f"NEVER output an English word unless {lang_name} is English.\n"
            "NO real data (form, H2H, injuries, lineups, statistics) is available for this "
            "match. Do NOT invent any of it. OVERRIDE the base format's 'all lines mandatory' "
            "rule: OMIT the per-team 📊 form lines and any recent-matches / injuries / form "
            "sections ENTIRELY — do NOT print a 'data unavailable' placeholder line per "
            "section. Output ONLY these lines, nothing else:\n"
            "🏆 [team A] — [team B]\n"
            "📍 [tournament | date]\n"
            "🔑 [key factor — 1–2 sentences grounded ONLY in the odds]\n"
            "🎯 [team A] — XX% | X.XX / draw — XX% | X.XX (if applicable) / [team B] — XX% | X.XX\n"
            "💎 [value verdict — your probability vs odds-implied 1/odd]\n"
            "🔢 [most likely score + one alternative]\n"
            "⚡ **[bet: type @ X.XX]** — [reason, 1 sentence]\n"
            "⚠️ [ONE closing line: the analysis is estimative because real data is "
            "unavailable — the localized equivalent of \"(оценочно)\"]\n"
            "Formal analytical tone, ~8–10 lines total."
        )
    return sys_prompt


async def _generate_forecast(uid: int, context: ContextTypes.DEFAULT_TYPE, status_msg):
    """Build prompt, call Claude, send reply. status_msg is the '⏳' message to edit."""
    lang = db_lang(uid)
    msg_content = list(context.user_data.get("pending_content") or [])
    text = context.user_data.get("pending_text", "")
    if not msg_content:
        await status_msg.edit_text(tr(uid, "no_input")); return

    u = db_get(uid) or {}
    exp = u.get("experience", "beginner")
    sys_prompt = _build_system_prompt(lang, exp, bool(context.user_data.get("has_real_data")))

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
        now_utc, _user_tz(uid), include_later=True)  # show the full 7-day window

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
    context.user_data["fm_sport_page"] = 0
    context.user_data["fm_sport_items"] = None
    context.user_data["fm_day_options"] = None
    context.user_data["fm_day_filtered"] = None
    context.user_data["fm_country_options"] = None
    context.user_data["fm_country_page"] = 0
    context.user_data["fm_leagues"] = None
    context.user_data["fm_league_page"] = 0
    context.user_data["fm_league_back"] = "fm_back_sport"
    context.user_data["fm_matches"] = None
    context.user_data["fm_match_page"] = 0
    # Cached for this session's group_by_league calls so the demand aggregate
    # isn't recomputed on every screen.
    context.user_data["fm_now_utc"] = now_utc
    context.user_data["fm_demand"] = db_match_demand()

    await msg.edit_text(_loc(_SPORT_TITLE, lang), reply_markup=_build_sport_kb(sport_groups, 0, uid))


async def fm_sport_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    sport_groups = context.user_data.get("fm_sports")
    idx = _parse_index(q.data)
    if not sport_groups or idx is None or idx < 0 or idx >= len(sport_groups):
        # Stale/expired keyboard — cheap path, not charged against the limit.
        await q.answer()
        await _expired_menu(q, uid); return
    if not await nav_guard(update):
        return
    await q.answer()

    sport_name, sport_items = sport_groups[idx]
    context.user_data["fm_sport_idx"] = idx
    # Freeze this sport's items for the day/country filter steps below.
    context.user_data["fm_sport_items"] = sport_items

    day_options = available_day_options(sport_items, _user_tz(uid))
    context.user_data["fm_day_options"] = day_options
    await q.edit_message_text(tr(uid, "ev_day_title"), reply_markup=_build_day_kb(day_options, uid))


async def fm_sppg_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id; lang = db_lang(uid)
    sport_groups = context.user_data.get("fm_sports")
    page = _parse_index(q.data)
    if not sport_groups or page is None:
        await q.answer()
        await _expired_menu(q, uid); return
    if not await nav_guard(update):
        return
    await q.answer()
    _, page, _, _ = paginate(sport_groups, page, PAGE_SIZE)
    context.user_data["fm_sport_page"] = page
    await q.edit_message_text(_loc(_SPORT_TITLE, lang), reply_markup=_build_sport_kb(sport_groups, page, uid))


async def fm_day_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    day_options = context.user_data.get("fm_day_options")
    sport_items = context.user_data.get("fm_sport_items")
    idx = _parse_index(q.data)
    if (idx is None or idx < 0 or sport_items is None or day_options is None
            or (idx != 0 and idx - 1 >= len(day_options))):
        await q.answer(); await _expired_menu(q, uid); return
    if not await nav_guard(update):
        return
    await q.answer()

    day_key = DAY_ALL if idx == 0 else day_options[idx - 1][0]
    filtered = sport_items if day_key == DAY_ALL else filter_by_day(sport_items, day_key, _user_tz(uid))
    context.user_data["fm_day_filtered"] = filtered

    country_options = available_countries(filtered)
    if len(country_options) <= 1:
        # Nothing to narrow by country — go straight to the league list.
        await _show_league_list(q, context, uid, filtered, back_cb="fm_back_day")
        return
    context.user_data["fm_country_options"] = country_options
    context.user_data["fm_country_page"] = 0
    await q.edit_message_text(tr(uid, "ev_country_title"),
                              reply_markup=_build_country_kb(country_options, 0, uid))


async def fm_ctry_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    country_options = context.user_data.get("fm_country_options")
    filtered = context.user_data.get("fm_day_filtered")
    idx = _parse_index(q.data)
    if (idx is None or idx < 0 or filtered is None or country_options is None
            or (idx != 0 and idx - 1 >= len(country_options))):
        await q.answer(); await _expired_menu(q, uid); return
    if not await nav_guard(update):
        return
    await q.answer()

    scoped = filtered
    if idx != 0:
        country_key = country_options[idx - 1][0]
        scoped = filter_by_country(filtered, country_key)
    await _show_league_list(q, context, uid, scoped, back_cb="fm_back_country")


async def fm_ctrypg_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    country_options = context.user_data.get("fm_country_options")
    page = _parse_index(q.data)
    if country_options is None or page is None:
        await q.answer()
        await _expired_menu(q, uid); return
    if not await nav_guard(update):
        return
    await q.answer()
    _, page, _, _ = paginate(country_options, page, PAGE_SIZE)
    context.user_data["fm_country_page"] = page
    await q.edit_message_text(tr(uid, "ev_country_title"),
                              reply_markup=_build_country_kb(country_options, page, uid))


async def _show_league_list(q, context, uid: int, items: list, back_cb: str) -> None:
    """Shared tail of the day/country filter steps: group the filtered items
    by league (full list, no cap) and show page 0."""
    groups = group_by_league(items, now_utc=context.user_data.get("fm_now_utc"),
                             demand=context.user_data.get("fm_demand"))
    context.user_data["fm_leagues"] = groups
    context.user_data["fm_league_page"] = 0
    context.user_data["fm_league_back"] = back_cb
    context.user_data["fm_matches"] = None

    idx = context.user_data.get("fm_sport_idx", 0)
    sport_groups = context.user_data.get("fm_sports") or []
    sport_name = sport_groups[idx][0] if idx < len(sport_groups) else ""
    title = tr(uid, "ev_tournaments_title", name=sport_name)
    if not groups:
        # Never leave the user on a dead-end screen with no way back.
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton(tr(uid, "ev_back"), callback_data=back_cb)]])
        await q.edit_message_text(title + "\n" + tr(uid, "ev_filter_empty"), reply_markup=back_kb)
        return
    await q.edit_message_text(title, reply_markup=_build_league_kb(groups, 0, back_cb, uid))


async def fm_lgpg_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    groups = context.user_data.get("fm_leagues")
    page = _parse_index(q.data)
    if not groups or page is None:
        await q.answer()
        await _expired_menu(q, uid); return
    if not await nav_guard(update):
        return
    await q.answer()
    _, page, _, _ = paginate(groups, page, PAGE_SIZE)
    context.user_data["fm_league_page"] = page
    back_cb = context.user_data.get("fm_league_back", "fm_back_sport")

    idx = context.user_data.get("fm_sport_idx", 0)
    sport_groups = context.user_data.get("fm_sports") or []
    sport_name = sport_groups[idx][0] if idx < len(sport_groups) else ""
    title = tr(uid, "ev_tournaments_title", name=sport_name)
    await q.edit_message_text(title, reply_markup=_build_league_kb(groups, page, back_cb, uid))


async def fm_league_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    groups = context.user_data.get("fm_leagues")
    idx = _parse_index(q.data)
    if not groups or idx is None or idx < 0 or idx >= len(groups):
        await q.answer()
        await _expired_menu(q, uid); return
    if not await nav_guard(update):
        return
    await q.answer()

    g = groups[idx]
    context.user_data["fm_league_idx"] = idx
    # g.items is the FULL, uncapped match list for this league; freeze it as
    # the exact snapshot this keyboard's fm_mt_ indices resolve against —
    # pagination only changes which page is shown, never the index mapping.
    context.user_data["fm_matches"] = g.items
    context.user_data["fm_match_page"] = 0

    title = tr(uid, "ev_matches_title", name=g.league_name)
    await q.edit_message_text(title, reply_markup=_build_match_kb(g.items, 0, uid))


async def fm_mtpg_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    matches = context.user_data.get("fm_matches")
    page = _parse_index(q.data)
    if not matches or page is None:
        await q.answer()
        await _expired_menu(q, uid); return
    if not await nav_guard(update):
        return
    await q.answer()
    _, page, _, _ = paginate(matches, page, PAGE_SIZE)
    context.user_data["fm_match_page"] = page

    groups = context.user_data.get("fm_leagues") or []
    lidx = context.user_data.get("fm_league_idx", 0)
    league_name = groups[lidx].league_name if lidx < len(groups) else ""
    title = tr(uid, "ev_matches_title", name=league_name)
    await q.edit_message_text(title, reply_markup=_build_match_kb(matches, page, uid))


async def fm_match_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id; lang = db_lang(uid)
    matches = context.user_data.get("fm_matches")
    idx = _parse_index(q.data)
    if not matches or idx is None or idx < 0 or idx >= len(matches):
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
    q = update.callback_query
    uid = q.from_user.id; lang = db_lang(uid)
    if not await nav_guard(update):
        return
    await q.answer()

    if q.data == "fm_back_sport":
        sport_groups = context.user_data.get("fm_sports")
        if not sport_groups:
            await _expired_menu(q, uid); return
        page = context.user_data.get("fm_sport_page", 0)
        await q.edit_message_text(_loc(_SPORT_TITLE, lang), reply_markup=_build_sport_kb(sport_groups, page, uid))

    elif q.data == "fm_back_day":
        day_options = context.user_data.get("fm_day_options")
        if day_options is None:
            await _expired_menu(q, uid); return
        await q.edit_message_text(tr(uid, "ev_day_title"), reply_markup=_build_day_kb(day_options, uid))

    elif q.data == "fm_back_country":
        country_options = context.user_data.get("fm_country_options")
        if country_options is None:
            await _expired_menu(q, uid); return
        page = context.user_data.get("fm_country_page", 0)
        await q.edit_message_text(tr(uid, "ev_country_title"),
                                  reply_markup=_build_country_kb(country_options, page, uid))

    elif q.data == "fm_back_league":
        groups = context.user_data.get("fm_leagues")
        if not groups:
            await _expired_menu(q, uid); return
        page = context.user_data.get("fm_league_page", 0)
        back_cb = context.user_data.get("fm_league_back", "fm_back_sport")
        idx = context.user_data.get("fm_sport_idx", 0)
        sport_groups = context.user_data.get("fm_sports") or []
        sport_name = sport_groups[idx][0] if idx < len(sport_groups) else ""
        title = tr(uid, "ev_tournaments_title", name=sport_name)
        await q.edit_message_text(title, reply_markup=_build_league_kb(groups, page, back_cb, uid))


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
        "ru": "📋 Выбери матч из списка — сделаю точный разбор:",
        "az": "📋 Siyahıdan matç seçin — dəqiq təhlil verim:",
        "en": "📋 Select a match from the list for an accurate forecast:",
        "tr": "📋 Listeden maç seç — net analiz vereyim:",
        "kz": "📋 Нақты болжам алу үшін тізімнен матч таңдаңыз:",
        "uz": "📋 Aniq bashorat olish uchun ro'yxatdan o'yin tanlang:",
        "ar": "📋 اختر مباراة من القائمة للحصول على توقع دقيق:",
    }
    await update.message.reply_text(use_menu.get(lang, use_menu["ru"]))
    await forecast_menu_start(update, context)
