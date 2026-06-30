import asyncio
import logging
import time

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import ADMIN_ID, MOSTBET_BASE, live_subs, blocked_until, mostbet_cache
from db import db_set, db_stats, db_search, _one, _all
from translations import sport_label, exp_label
from mostbet import _mostbet_load_matches  # noqa: F401 (used in adm_cb)

logger = logging.getLogger(__name__)

LANG_NAMES = {
    "az": "🇦🇿 Azərbaycan", "ru": "🇷🇺 Русский", "en": "🇬🇧 English",
    "tr": "🇹🇷 Türkçe",    "kz": "🇰🇿 Қазақша",  "uz": "🇺🇿 O'zbek",
    "ar": "🇸🇦 العربية",
}
SPORT_NAMES = {
    "football": "⚽ Футбол", "ufc": "🥊 UFC/MMA", "nba": "🏀 Баскетбол",
    "tennis": "🎾 Теннис",  "hockey": "🏒 Хоккей", "all": "🏆 Все виды",
}


def is_adm(update):
    return (update.effective_user.id if update.effective_user else 0) == ADMIN_ID


def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика",          callback_data="adm_stats")],
        [InlineKeyboardButton("📢 Рассылка",            callback_data="adm_broadcast_menu")],
        [InlineKeyboardButton("🚫 Заблокированные",     callback_data="adm_blocklist")],
        [InlineKeyboardButton("🔍 Поиск пользователя", callback_data="adm_search")],
        [InlineKeyboardButton("🔴 Live подписки",       callback_data="adm_live")],
        [InlineKeyboardButton("🔧 Тест Mostbet API",    callback_data="adm_test_mostbet")],
        [InlineKeyboardButton("🎰 Дамп коэф. матча",   callback_data="adm_odds_dump")],
        [InlineKeyboardButton("🗂 Дамп категорий/ЧМ",  callback_data="adm_cat_dump")],
        [InlineKeyboardButton("🛰 Probe URL",          callback_data="adm_probe")],
        [InlineKeyboardButton("🧪 Тест данных матча",  callback_data="adm_data_test")],
    ])


def broadcast_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Всем",               callback_data="adm_bcast_seg_all")],
        [InlineKeyboardButton("🌍 По языку",           callback_data="adm_bcast_by_lang")],
        [InlineKeyboardButton("⚽ По спорту",          callback_data="adm_bcast_by_sport")],
        [InlineKeyboardButton("📅 По активности",      callback_data="adm_bcast_by_act")],
        [InlineKeyboardButton("◀️ Назад",              callback_data="adm_back")],
    ])


async def _broadcast(app, context, uids: list[int], text: str, status_msg) -> tuple[int, int]:
    ok = fail = 0
    total = len(uids)
    for i, uid in enumerate(uids):
        try:
            await app.bot.send_message(chat_id=uid, text=text)
            ok += 1
        except Exception:
            fail += 1
        if i % 50 == 49:
            try:
                await status_msg.edit_text(
                    f"⏳ Рассылка... {i+1}/{total}\n✅ {ok} | ❌ {fail}")
            except Exception:
                pass
        await asyncio.sleep(0.05)
    return ok, fail


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_adm(update): return
    await update.message.reply_text("АДМИН ПАНЕЛЬ", reply_markup=admin_kb())


async def adm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("Нет доступа", show_alert=True); return
    await q.answer(); data = q.data
    back = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="adm_back")]])

    # ── Stats ─────────────────────────────────────────────────────────────────
    if data == "adm_stats":
        s = db_stats()
        blk_now = sum(1 for v in blocked_until.values() if time.time() < v)
        live_now = sum(len(v) for v in live_subs.values())
        lang_str = " | ".join(f"{l}: {n}" for l, n in s["langs"])
        top = "\n".join(
            f"{i+1}. {r[1] or r[0]}: {r[2]} запросов"
            for i, r in enumerate(s["top_req"]))
        sport_rows = _all(
            "SELECT sports, COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 "
            "AND sports!='' GROUP BY sports ORDER BY COUNT(*) DESC"
        )
        sport_str = " | ".join(
            f"{SPORT_NAMES.get(s, s)}: {n}" for s, n in sport_rows)
        await q.edit_message_text(
            f"📊 СТАТИСТИКА\n\n"
            f"Пользователей: {s['total']}\n"
            f"Новых сегодня: {s['today']}\n"
            f"Заблокировано: {s['blocked']}\n"
            f"Онбординг: {s['ob_done']}\n\n"
            f"Запросов всего: {s['rqtotal']}\n"
            f"Сегодня: {s['rqtoday']}\n\n"
            f"Языки: {lang_str}\n"
            f"Спорт: {sport_str}\n\n"
            f"Live подписки: {s['live_ct']} (активных: {live_now})\n"
            f"Rate-limit блок: {blk_now}\n\n"
            f"Топ активных:\n{top}",
            reply_markup=back)

    # ── Broadcast menu ────────────────────────────────────────────────────────
    elif data == "adm_broadcast_menu":
        total = _one("SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0") or 0
        await q.edit_message_text(
            f"📢 РАССЫЛКА\n\nВсего активных: {total} чел.\n\nВыберите аудиторию:",
            reply_markup=broadcast_menu_kb())

    # ── Segment: all ──────────────────────────────────────────────────────────
    elif data == "adm_bcast_seg_all":
        cnt = _one("SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0") or 0
        context.user_data["adm_bcast_seg"] = "all"
        context.user_data["adm_act"] = "broadcast"
        await q.edit_message_text(
            f"📢 РАССЫЛКА ВСЕМ\n\nПолучателей: {cnt} чел.\n\nОтправьте текст рассылки.\n/cancel — отмена.")

    # ── By language ───────────────────────────────────────────────────────────
    elif data == "adm_bcast_by_lang":
        rows = _all("SELECT lang, COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 GROUP BY lang")
        counts = dict(rows)
        btns = []
        for code, name in LANG_NAMES.items():
            n = counts.get(code, 0)
            btns.append([InlineKeyboardButton(f"{name} — {n} чел.", callback_data=f"adm_bcast_lang_{code}")])
        btns.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_broadcast_menu")])
        await q.edit_message_text("🌍 РАССЫЛКА ПО ЯЗЫКУ\n\nВыберите язык:", reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("adm_bcast_lang_"):
        code = data[len("adm_bcast_lang_"):]
        cnt = _one("SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 AND lang=?", (code,)) or 0
        context.user_data["adm_bcast_seg"] = f"lang:{code}"
        context.user_data["adm_act"] = "broadcast"
        await q.edit_message_text(
            f"📢 РАССЫЛКА: {LANG_NAMES.get(code, code)}\n\nПолучателей: {cnt} чел.\n\n"
            f"Отправьте текст рассылки.\n/cancel — отмена.")

    # ── By sport ──────────────────────────────────────────────────────────────
    elif data == "adm_bcast_by_sport":
        rows = _all(
            "SELECT sports, COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 "
            "AND sports!='' GROUP BY sports ORDER BY COUNT(*) DESC"
        )
        counts = dict(rows)
        btns = []
        for code, name in SPORT_NAMES.items():
            n = counts.get(code, 0)
            btns.append([InlineKeyboardButton(f"{name} — {n} чел.", callback_data=f"adm_bcast_sport_{code}")])
        btns.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_broadcast_menu")])
        await q.edit_message_text("⚽ РАССЫЛКА ПО СПОРТУ\n\nВыберите аудиторию:", reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("adm_bcast_sport_"):
        code = data[len("adm_bcast_sport_"):]
        cnt = _one("SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 AND sports=?", (code,)) or 0
        context.user_data["adm_bcast_seg"] = f"sport:{code}"
        context.user_data["adm_act"] = "broadcast"
        await q.edit_message_text(
            f"📢 РАССЫЛКА: {SPORT_NAMES.get(code, code)}\n\nПолучателей: {cnt} чел.\n\n"
            f"Отправьте текст рассылки.\n/cancel — отмена.")

    # ── By activity ───────────────────────────────────────────────────────────
    elif data == "adm_bcast_by_act":
        active7 = _one(
            "SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 "
            "AND last_active != '' AND date(last_active) >= date('now', '-7 days')"
        ) or 0
        inactive30 = _one(
            "SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 "
            "AND (last_active='' OR date(last_active) < date('now', '-30 days'))"
        ) or 0
        inactive7_30 = _one(
            "SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 "
            "AND last_active != '' AND date(last_active) < date('now', '-7 days') "
            "AND date(last_active) >= date('now', '-30 days')"
        ) or 0
        btns = [
            [InlineKeyboardButton(f"🟢 Активные (≤7 дней) — {active7} чел.",   callback_data="adm_bcast_act_active")],
            [InlineKeyboardButton(f"🟡 Отток (7-30 дней) — {inactive7_30} чел.", callback_data="adm_bcast_act_churn")],
            [InlineKeyboardButton(f"🔴 Спящие (>30 дней) — {inactive30} чел.", callback_data="adm_bcast_act_sleep")],
            [InlineKeyboardButton("◀️ Назад", callback_data="adm_broadcast_menu")],
        ]
        await q.edit_message_text("📅 РАССЫЛКА ПО АКТИВНОСТИ\n\nВыберите сегмент:", reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("adm_bcast_act_"):
        seg = data[len("adm_bcast_act_"):]
        seg_labels = {"active": "🟢 Активные (≤7 дней)", "churn": "🟡 Отток (7-30 дней)", "sleep": "🔴 Спящие (>30 дней)"}
        if seg == "active":
            cnt = _one(
                "SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 "
                "AND last_active != '' AND date(last_active) >= date('now', '-7 days')"
            ) or 0
        elif seg == "churn":
            cnt = _one(
                "SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 "
                "AND last_active != '' AND date(last_active) < date('now', '-7 days') "
                "AND date(last_active) >= date('now', '-30 days')"
            ) or 0
        else:
            cnt = _one(
                "SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 "
                "AND (last_active='' OR date(last_active) < date('now', '-30 days'))"
            ) or 0
        context.user_data["adm_bcast_seg"] = f"act:{seg}"
        context.user_data["adm_act"] = "broadcast"
        await q.edit_message_text(
            f"📢 РАССЫЛКА: {seg_labels[seg]}\n\nПолучателей: {cnt} чел.\n\n"
            f"Отправьте текст рассылки.\n/cancel — отмена.")

    # ── Confirm broadcast ─────────────────────────────────────────────────────
    elif data.startswith("adm_bcast_confirm_"):
        seg = context.user_data.get("adm_bcast_seg", "")
        text = context.user_data.get("adm_bcast_text", "")
        uids = _get_uids_for_seg(seg)
        status = await context.bot.send_message(
            chat_id=q.from_user.id, text=f"⏳ Рассылка для {len(uids)} чел...")
        ok, fail = await _broadcast(context.application, context, uids, text, status)
        await status.edit_text(f"✅ Готово!\nДоставлено: {ok}\nНе доставлено: {fail}")
        context.user_data.pop("adm_bcast_seg", None)
        context.user_data.pop("adm_bcast_text", None)

    elif data == "adm_bcast_cancel":
        context.user_data.pop("adm_bcast_seg", None)
        context.user_data.pop("adm_bcast_text", None)
        await q.edit_message_text("❌ Рассылка отменена.", reply_markup=back)

    # ── Blocklist ─────────────────────────────────────────────────────────────
    elif data == "adm_blocklist":
        rows = _all("SELECT user_id,username,display_name FROM users WHERE is_blocked=1")
        if not rows:
            await q.edit_message_text("Нет заблокированных.", reply_markup=back); return
        btns = [
            [InlineKeyboardButton(f"Разблокировать: {r[2] or r[1] or r[0]}",
                                  callback_data=f"adm_unblk_{r[0]}")]
            for r in rows
        ]
        btns.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_back")])
        lines = ["🚫 ЗАБЛОКИРОВАННЫЕ:"] + [f"- {r[2] or r[1] or r[0]} (id={r[0]})" for r in rows]
        await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("adm_unblk_"):
        uid = int(data.split("_")[2]); db_set(uid, "is_blocked", 0)
        await q.edit_message_text(f"✅ Пользователь {uid} разблокирован.", reply_markup=back)

    elif data.startswith("adm_blk_"):
        uid = int(data.split("_")[2]); db_set(uid, "is_blocked", 1)
        await q.edit_message_text(f"🚫 Пользователь {uid} заблокирован.", reply_markup=back)

    # ── Search ────────────────────────────────────────────────────────────────
    elif data == "adm_search":
        context.user_data["adm_act"] = "search"
        await q.edit_message_text("🔍 Введите ID, username или имя.")

    # ── Live subs ─────────────────────────────────────────────────────────────
    elif data == "adm_live":
        live_now = sum(len(v) for v in live_subs.values())
        lines = [f"🔴 LIVE ПОДПИСКИ: {live_now} активных\n"]
        for mid, uids in live_subs.items():
            if uids: lines.append(f"Матч {mid}: {len(uids)} подписчиков")
        await q.edit_message_text(
            "\n".join(lines) if len(lines) > 1 else "Нет активных.", reply_markup=back)

    # ── Test Mostbet ──────────────────────────────────────────────────────────
    elif data == "adm_test_mostbet":
        await q.edit_message_text("🔧 Тестирую Mostbet API...")
        try:
            matches = await _mostbet_load_matches()
            if not matches:
                await q.edit_message_text(
                    "MOSTBET API\n\nСтатус: НЕТ ДАННЫХ\n\nВозможные причины:\n"
                    "- 429 Rate limit\n- IP не в whitelist\n- Проблемы с сетью",
                    reply_markup=back)
            else:
                lines = [f"MOSTBET API\n\nСтатус: ✅ РАБОТАЕТ\nВсего матчей: {len(matches)}\n\nПримеры:"]
                for m in matches[:5]:
                    t1 = m.get("team1Title", "?"); t2 = m.get("team2Title", "?")
                    league = m.get("lineSubCategory", "")
                    live = "LIVE" if m.get("isLive") else "Pre"
                    lines.append(f"[{live}] {t1} vs {t2} ({league})")
                cache_ts = mostbet_cache.get("all_matches", (0, []))[0]
                if cache_ts:
                    lines.append(f"\nКэш: {int(time.time() - cache_ts)} сек назад")
                await q.edit_message_text("\n".join(lines), reply_markup=back)
        except Exception as e:
            await q.edit_message_text(f"MOSTBET API\n\nОшибка: {e}", reply_markup=back)

    # ── Odds dump ─────────────────────────────────────────────────────────────
    elif data == "adm_odds_dump":
        context.user_data["adm_act"] = "odds_dump"
        await q.edit_message_text(
            "🎰 ДАМП КОЭФФИЦИЕНТОВ\n\nОтправьте ID матча Mostbet (числовой).\n"
            "Можно найти в /testapi или через меню прогнозов.")

    # ── Categories dump ───────────────────────────────────────────────────────
    elif data == "adm_cat_dump":
        await q.edit_message_text("⏳ Запрашиваю сырые данные Mostbet (несколько страниц)...")
        try:
            import json as _json
            from collections import Counter
            from config import MOSTBET_BASE

            # Direct UNFILTERED fetch so we see every sport the API actually returns.
            raw = []
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as hc:
                last_id = 0
                for _ in range(8):  # up to 8 pages = 800 matches sample
                    r = await hc.get(
                        f"{MOSTBET_BASE}/api/v3/advertiser/oddschecker/line/list",
                        headers={"Accept": "application/json", "User-Agent": "ProqnozAI/1.0"},
                        params={"lastId": last_id, "locale": "en", "limit": 100})
                    if r.status_code != 200:
                        raw = raw or f"HTTP {r.status_code}"
                        break
                    page = r.json().get("lineMatches", [])
                    if not page:
                        break
                    raw.extend(page)
                    if len(page) < 100:
                        break
                    last_id = page[-1]["id"]
                    await asyncio.sleep(0.5)

            if isinstance(raw, str):
                await q.edit_message_text(f"❌ Ошибка API: {raw}", reply_markup=back); return
            if not raw:
                await q.edit_message_text("❌ Пустой ответ API.", reply_markup=back); return

            cats = Counter((m.get("lineCategory") or "?").strip() for m in raw)
            kw = ("world", "cup", "fifa", "mundial", "чемпионат мира", "кубок мира", "dünya")
            wc = [m for m in raw if any(
                k in " ".join([str(m.get("lineSubCategory") or ""), str(m.get("lineCategory") or ""),
                               str(m.get("team1Title") or ""), str(m.get("team2Title") or "")]).lower()
                for k in kw)]

            # 1. Raw fields of the first match — what the API actually sends
            sample = raw[0]
            sample_dump = _json.dumps(sample, ensure_ascii=False, indent=1)[:2500]
            await context.bot.send_message(
                chat_id=q.from_user.id,
                text=f"🧬 СЫРЫЕ ПОЛЯ первого матча (выборка {len(raw)}):\n\n{sample_dump}")

            # 2. Categories + WC search
            lines = [f"🏷 КАТЕГОРИИ (из {len(raw)} матчей):"]
            for c, n in cats.most_common(25):
                lines.append(f"  {c}: {n}")
            lines.append(f"\n🔎 Совпадений по ЧМ-словам: {len(wc)}")
            for m in wc[:15]:
                lines.append(
                    f"  [{m.get('lineCategory')}] / {m.get('lineSubCategory')} | "
                    f"{m.get('team1Title')} vs {m.get('team2Title')} | "
                    f"{'LIVE' if m.get('isLive') else m.get('matchBeginAt','?')} | id={m.get('id')}")
            msg = "\n".join(lines)
            for i in range(0, len(msg), 3800):
                await context.bot.send_message(chat_id=q.from_user.id, text=msg[i:i+3800])
        except Exception as e:
            await q.edit_message_text(f"❌ Ошибка: {e}", reply_markup=back)

    # ── Test real-data fetch ──────────────────────────────────────────────────
    elif data == "adm_data_test":
        import os as _os
        from config import APIFOOTBALL_KEY, FOOTBALL_KEY
        context.user_data["adm_act"] = "data_test"
        # Show presence of core vars too — confirms this is the right service.
        core = " ".join(
            f"{name}={'✅' if _os.environ.get(name) else '❌'}"
            for name in ("TELEGRAM_TOKEN", "ANTHROPIC_API_KEY", "ADMIN_ID", "BOT_DB_DIR"))
        # Dump env var NAMES (no values) that look related — reveals typos/spaces.
        related = sorted(n for n in _os.environ
                         if any(k in n.upper() for k in ("FOOTBALL", "API", "KEY")))
        names = "\n".join(f"  «{n}» (len={len(_os.environ.get(n,''))})" for n in related) or "  (нет)"
        await q.edit_message_text(
            "🧪 ТЕСТ ДАННЫХ МАТЧА\n\n"
            f"APIFOOTBALL_KEY: {'✅ задан' if APIFOOTBALL_KEY else '❌ НЕ задан'}\n"
            f"FOOTBALL_KEY: {'✅ задан' if FOOTBALL_KEY else '❌ НЕ задан'}\n\n"
            f"Контроль:\n{core}\n\n"
            f"Похожие переменные в окружении:\n{names}\n\n"
            "Отправьте две команды через дефис: Germany - Paraguay")

    # ── Probe arbitrary URL (from whitelisted IP) ─────────────────────────────
    elif data == "adm_probe":
        context.user_data["adm_act"] = "probe"
        await q.edit_message_text(
            "🛰 PROBE URL\n\nОтправьте полный URL (http/https) — бот запросит его "
            "со своего IP и вернёт статус + начало ответа.\n\n"
            "Как найти линию сайта:\n"
            "1. Открой сайт Mostbet в браузере (ПК)\n"
            "2. F12 → вкладка Network → фильтр Fetch/XHR\n"
            "3. Кликни Футбол / нужный турнир\n"
            "4. Найди запрос с матчами, ПКМ → Copy → Copy link address\n"
            "5. Пришли этот URL сюда")

    elif data == "adm_back":
        await q.edit_message_text("АДМИН ПАНЕЛЬ", reply_markup=admin_kb())


def _get_uids_for_seg(seg: str) -> list[int]:
    base = "SELECT user_id FROM users WHERE is_registered=1 AND is_blocked=0"
    if seg == "all":
        rows = _all(base)
    elif seg.startswith("lang:"):
        rows = _all(base + " AND lang=?", (seg[5:],))
    elif seg.startswith("sport:"):
        rows = _all(base + " AND sports=?", (seg[6:],))
    elif seg == "act:active":
        rows = _all(base + " AND last_active != '' AND date(last_active) >= date('now', '-7 days')")
    elif seg == "act:churn":
        rows = _all(base + " AND last_active != '' AND date(last_active) < date('now', '-7 days')"
                         " AND date(last_active) >= date('now', '-30 days')")
    elif seg == "act:sleep":
        rows = _all(base + " AND (last_active='' OR date(last_active) < date('now', '-30 days'))")
    else:
        rows = []
    return [r[0] for r in rows]


async def handle_adm_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_adm(update): return
    act = context.user_data.get("adm_act")
    if not act: return
    context.user_data.pop("adm_act")
    text = update.message.text or ""

    if act == "broadcast":
        seg = context.user_data.get("adm_bcast_seg", "all")
        uids = _get_uids_for_seg(seg)
        # Show confirmation
        preview = text[:200] + ("..." if len(text) > 200 else "")
        seg_label = _seg_label(seg)
        confirm_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Отправить", callback_data="adm_bcast_confirm_go"),
            InlineKeyboardButton("❌ Отмена",    callback_data="adm_bcast_cancel"),
        ]])
        context.user_data["adm_bcast_text"] = text
        await update.message.reply_text(
            f"📢 ПОДТВЕРЖДЕНИЕ РАССЫЛКИ\n\n"
            f"Аудитория: {seg_label}\n"
            f"Получателей: {len(uids)} чел.\n\n"
            f"Превью:\n{preview}",
            reply_markup=confirm_kb)

    elif act == "data_test":
        sep = "-" if "-" in text else ("—" if "—" in text else None)
        if not sep:
            await update.message.reply_text("❌ Формат: Команда1 - Команда2 [- Лига]"); return
        bits = [p.strip() for p in text.split(sep)]
        t1 = bits[0] if bits else ""
        t2 = bits[1] if len(bits) > 1 else ""
        league_hint = bits[2] if len(bits) > 2 else "World Cup"
        if not t1 or not t2:
            await update.message.reply_text("❌ Нужны обе команды."); return
        await update.message.reply_text(f"⏳ Тяну данные для {t1} vs {t2}...")
        try:
            import httpx as _httpx
            from config import APIFOOTBALL_KEY
            # Raw api-sports diagnostics: /status reveals key validity, plan,
            # and remaining quota; /teams reveals lookup + any "errors" field.
            diag = ["🔬 api-sports диагностика:"]
            async with _httpx.AsyncClient(timeout=12) as _h:
                hd = {"x-apisports-key": APIFOOTBALL_KEY}
                rs = await _h.get("https://v3.football.api-sports.io/status", headers=hd)
                js = rs.json() if rs.status_code == 200 else {}
                resp = js.get("response", {}) or {}
                acc = resp.get("subscription", {}) or {}
                req = resp.get("requests", {}) or {}
                diag.append(f"/status HTTP {rs.status_code}")
                if js.get("errors"):
                    diag.append(f"❌ errors: {js.get('errors')}")
                if acc:
                    diag.append(f"План: {acc.get('plan')} | active: {acc.get('active')} | end: {acc.get('end')}")
                if req:
                    diag.append(f"Запросы: {req.get('current')}/{req.get('limit_day')} за день")
                rt = await _h.get("https://v3.football.api-sports.io/teams",
                                  headers=hd, params={"name": t1})
                jt = rt.json() if rt.status_code == 200 else {}
                diag.append(f"\n/teams?name={t1}: HTTP {rt.status_code}, "
                            f"найдено {jt.get('results', 0)}")
                if jt.get("errors"):
                    diag.append(f"❌ errors: {jt.get('errors')}")
                elif jt.get("response"):
                    tm = jt["response"][0]["team"]
                    tid = tm.get("id")
                    diag.append(f"→ {tm.get('name')} (id={tid}, "
                                f"national={tm.get('national')})")
                    # Free plan blocks `last`; probe by season to see which
                    # seasons this plan can actually access.
                    for season in (2026, 2025, 2023):
                        rf = await _h.get("https://v3.football.api-sports.io/fixtures",
                                          headers=hd, params={"team": tid, "season": season})
                        jf = rf.json() if rf.status_code == 200 else {}
                        msg = f"/fixtures season={season}: HTTP {rf.status_code}, найдено {jf.get('results', 0)}"
                        if jf.get("errors"):
                            msg += f" | errors: {jf.get('errors')}"
                        diag.append(msg)
            await update.message.reply_text("\n".join(diag))

            # ── football-data.org diagnostics ─────────────────────────────────
            from config import FOOTBALL_KEY
            fdiag = ["🔬 football-data.org:"]
            if not FOOTBALL_KEY:
                fdiag.append("❌ FOOTBALL_KEY не задан в config")
            else:
                async with _httpx.AsyncClient(timeout=12) as _h:
                    fh = {"X-Auth-Token": FOOTBALL_KEY}
                    rt = await _h.get("https://api.football-data.org/v4/teams",
                                      headers=fh, params={"name": t1, "limit": 1})
                    fdiag.append(f"/teams?name={t1}: HTTP {rt.status_code}")
                    try:
                        jt = rt.json()
                        if rt.status_code == 200:
                            tms = jt.get("teams", [])
                            fdiag.append(f"  teams найдено: {len(tms)}"
                                         + (f" → {tms[0].get('name')} (id={tms[0].get('id')})" if tms else ""))
                        else:
                            fdiag.append(f"  message: {jt.get('message', rt.text[:120])}")
                    except Exception:
                        fdiag.append(f"  raw: {rt.text[:120]}")
                    # Can we reach the World Cup competition at all?
                    rc = await _h.get("https://api.football-data.org/v4/competitions/WC/matches",
                                      headers=fh, params={"status": "FINISHED"})
                    fdiag.append(f"/competitions/WC/matches: HTTP {rc.status_code}")
                    try:
                        fdiag.append(f"  matches: {rc.json().get('resultSet', {}).get('count', '?')}"
                                     if rc.status_code == 200 else f"  message: {rc.json().get('message','')[:120]}")
                    except Exception:
                        pass
            await update.message.reply_text("\n".join(fdiag))

            from football_api import fetch_real_data
            res = await fetch_real_data(t1, t2, league_hint)
            if not res:
                verdict = "❌ ПУСТО — ни реальных данных, ни оценки."
            elif res.startswith("REAL MATCH DATA"):
                verdict = "✅ РЕАЛЬНЫЕ ДАННЫЕ получены."
            else:
                verdict = "⚠️ Только ОЦЕНКА ИИ (реальные API ничего не вернули)."
            out = f"{verdict}\n\n{res or '(пусто)'}"
            for i in range(0, min(len(out), 7600), 3800):
                await update.message.reply_text(out[i:i+3800])
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    elif act == "probe":
        url = text.strip()
        if not url.lower().startswith(("http://", "https://")):
            await update.message.reply_text("❌ Нужен полный URL с http(s)://"); return
        await update.message.reply_text(f"⏳ Запрашиваю {url[:80]}...")
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as hc:
                r = await hc.get(url, headers={
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                })
            ct = r.headers.get("content-type", "?")
            head = f"📡 {r.status_code} | {ct} | {len(r.content)} bytes\n\n"
            body = r.text
            # If JSON, try to summarize top-level keys
            try:
                j = r.json()
                if isinstance(j, dict):
                    head += "JSON keys: " + ", ".join(list(j.keys())[:20]) + "\n\n"
                elif isinstance(j, list):
                    head += f"JSON array, len={len(j)}\n\n"
            except Exception:
                pass
            out = head + body
            for i in range(0, min(len(out), 7600), 3800):
                await update.message.reply_text(out[i:i+3800])
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    elif act == "odds_dump":
        try:
            line_id = int(text.strip())
        except ValueError:
            await update.message.reply_text("❌ Неверный ID. Нужно число."); return
        await update.message.reply_text(f"⏳ Загружаю коэффициенты матча {line_id}...")
        try:
            import httpx as _httpx
            from collections import defaultdict as _dd
            from config import MOSTBET_BASE
            from mostbet import mostbet_get_odds, format_mostbet_odds
            async with _httpx.AsyncClient(timeout=12, follow_redirects=True) as h:
                r = await h.get(
                    f"{MOSTBET_BASE}/api/v3/advertiser/oddschecker/line/{line_id}/outcomes/list",
                    headers={"Accept": "application/json"},
                    params={"locale": "en", "limit": 100}
                )
            if r.status_code != 200:
                await update.message.reply_text(f"❌ HTTP {r.status_code}"); return
            outcomes = r.json().get("lineMatchOutcomes", [])

            # 1. Raw groups
            groups = _dd(list)
            for o in outcomes:
                g = o.get("groupTitle", "NO_GROUP")
                groups[g].append(f"{o.get('outcomeTitle')}={o.get('odd')}")
            lines = [f"📦 Матч {line_id}: {len(outcomes)} исходов (1 стр.)\n",
                     "🧩 СЫРЫЕ ГРУППЫ:"]
            for g, items in sorted(groups.items()):
                chunk = " | ".join(items[:5])
                if len(items) > 5: chunk += f" (+{len(items)-5})"
                lines.append(f"▪ {g}: {chunk}")

            # 2. What the parser actually recognised (full paginated fetch)
            parsed = await mostbet_get_odds(line_id)
            recognised = {k: v for k, v in parsed.items() if v is not None}
            lines.append(f"\n✅ РАСПОЗНАНО ПАРСЕРОМ ({len(recognised)} полей):")
            if recognised:
                for k, v in recognised.items():
                    lines.append(f"  {k} = {v}")
            else:
                lines.append("  — ничего (рынки не совпали с ключевыми словами)")

            # 3. The exact text injected into Claude's prompt
            prompt_str = format_mostbet_odds(parsed, "ru")
            lines.append("\n📤 В ПРОМПТ CLAUDE:")
            lines.append(prompt_str or "  — (пусто, не передаётся)")

            msg = "\n".join(lines)
            for i in range(0, len(msg), 3800):
                await update.message.reply_text(msg[i:i+3800])
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    elif act == "search":
        results = db_search(text.strip())
        if not results:
            await update.message.reply_text("Не найдено."); return
        for u in results:
            btns = []
            if u["is_blocked"]:
                btns.append([InlineKeyboardButton("Разблокировать",
                                                   callback_data=f"adm_unblk_{u['user_id']}")])
            else:
                btns.append([InlineKeyboardButton("Заблокировать",
                                                   callback_data=f"adm_blk_{u['user_id']}")])
            await update.message.reply_text(
                f"ID: {u['user_id']}\n"
                f"Username: @{u['username'] or '-'}\n"
                f"Имя: {u['display_name'] or '-'}\n"
                f"Язык: {LANG_NAMES.get(u['lang'], u['lang'])}\n"
                f"Статус: {'🚫 ЗАБЛОКИРОВАН' if u['is_blocked'] else '✅ Активен'}\n"
                f"Спорт: {SPORT_NAMES.get(u.get('sports', ''), '-')}\n"
                f"Запросов: {u['total_requests']}\n"
                f"Зарегистрирован: {u['joined_at']}",
                reply_markup=InlineKeyboardMarkup(btns))


def _seg_label(seg: str) -> str:
    if seg == "all": return "👥 Все пользователи"
    if seg.startswith("lang:"): return f"🌍 Язык: {LANG_NAMES.get(seg[5:], seg[5:])}"
    if seg.startswith("sport:"): return f"⚽ Спорт: {SPORT_NAMES.get(seg[6:], seg[6:])}"
    if seg == "act:active": return "🟢 Активные (≤7 дней)"
    if seg == "act:churn":  return "🟡 Отток (7-30 дней)"
    if seg == "act:sleep":  return "🔴 Спящие (>30 дней)"
    return seg


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("adm_act", None)
    context.user_data.pop("adm_bcast_seg", None)
    context.user_data.pop("adm_bcast_text", None)
    await update.message.reply_text("❌ Отменено.")


async def testapi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_adm(update): return
    await update.message.reply_text("🔧 Тестирую Mostbet API...")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as h:
            r = await h.get(
                f"{MOSTBET_BASE}/api/v3/advertiser/oddschecker/line/list",
                headers={"Accept": "application/json", "User-Agent": "ProqnozAI/1.0"},
                params={"lastId": 0, "locale": "ru", "limit": 3}
            )
            if r.status_code == 200:
                matches = r.json().get("lineMatches", [])
                lines = [f"Mostbet API: ✅ OK ({r.status_code})\nМатчей в ответе: {len(matches)}\n"]
                for m in matches[:3]:
                    t1 = m.get("team1Title", "?"); t2 = m.get("team2Title", "?")
                    lines.append(f"{'LIVE' if m.get('isLive') else 'Pre'}: {t1} vs {t2}")
                await update.message.reply_text("\n".join(lines))
            else:
                await update.message.reply_text(
                    f"Mostbet API: ❌ ОШИБКА\nСтатус: {r.status_code}\n{r.text[:200]}")
    except Exception as e:
        await update.message.reply_text(f"Mostbet API: ❌ ИСКЛЮЧЕНИЕ\n{e}")
