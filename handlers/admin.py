import asyncio
import logging
import time

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import ADMIN_ID, MOSTBET_BASE, live_subs, blocked_until, mostbet_cache
from db import db_set, db_stats, db_search, con
from translations import sport_label, exp_label
from mostbet import _mostbet_load_matches

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
        with con() as c:
            sport_rows = c.execute(
                "SELECT sports, COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 "
                "AND sports!='' GROUP BY sports ORDER BY COUNT(*) DESC"
            ).fetchall()
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
        with con() as c:
            total = c.execute(
                "SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0"
            ).fetchone()[0]
        await q.edit_message_text(
            f"📢 РАССЫЛКА\n\nВсего активных: {total} чел.\n\nВыберите аудиторию:",
            reply_markup=broadcast_menu_kb())

    # ── Segment: all ──────────────────────────────────────────────────────────
    elif data == "adm_bcast_seg_all":
        with con() as c:
            cnt = c.execute(
                "SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0"
            ).fetchone()[0]
        context.user_data["adm_bcast_seg"] = "all"
        context.user_data["adm_act"] = "broadcast"
        await q.edit_message_text(
            f"📢 РАССЫЛКА ВСЕМ\n\nПолучателей: {cnt} чел.\n\nОтправьте текст рассылки.\n/cancel — отмена.")

    # ── By language ───────────────────────────────────────────────────────────
    elif data == "adm_bcast_by_lang":
        with con() as c:
            rows = c.execute(
                "SELECT lang, COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 GROUP BY lang"
            ).fetchall()
        counts = dict(rows)
        btns = []
        for code, name in LANG_NAMES.items():
            n = counts.get(code, 0)
            btns.append([InlineKeyboardButton(f"{name} — {n} чел.", callback_data=f"adm_bcast_lang_{code}")])
        btns.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_broadcast_menu")])
        await q.edit_message_text("🌍 РАССЫЛКА ПО ЯЗЫКУ\n\nВыберите язык:", reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("adm_bcast_lang_"):
        code = data[len("adm_bcast_lang_"):]
        with con() as c:
            cnt = c.execute(
                "SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 AND lang=?", (code,)
            ).fetchone()[0]
        context.user_data["adm_bcast_seg"] = f"lang:{code}"
        context.user_data["adm_act"] = "broadcast"
        await q.edit_message_text(
            f"📢 РАССЫЛКА: {LANG_NAMES.get(code, code)}\n\nПолучателей: {cnt} чел.\n\n"
            f"Отправьте текст рассылки.\n/cancel — отмена.")

    # ── By sport ──────────────────────────────────────────────────────────────
    elif data == "adm_bcast_by_sport":
        with con() as c:
            rows = c.execute(
                "SELECT sports, COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 "
                "AND sports!='' GROUP BY sports ORDER BY COUNT(*) DESC"
            ).fetchall()
        counts = dict(rows)
        btns = []
        for code, name in SPORT_NAMES.items():
            n = counts.get(code, 0)
            btns.append([InlineKeyboardButton(f"{name} — {n} чел.", callback_data=f"adm_bcast_sport_{code}")])
        btns.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_broadcast_menu")])
        await q.edit_message_text("⚽ РАССЫЛКА ПО СПОРТУ\n\nВыберите аудиторию:", reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("adm_bcast_sport_"):
        code = data[len("adm_bcast_sport_"):]
        with con() as c:
            cnt = c.execute(
                "SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 AND sports=?", (code,)
            ).fetchone()[0]
        context.user_data["adm_bcast_seg"] = f"sport:{code}"
        context.user_data["adm_act"] = "broadcast"
        await q.edit_message_text(
            f"📢 РАССЫЛКА: {SPORT_NAMES.get(code, code)}\n\nПолучателей: {cnt} чел.\n\n"
            f"Отправьте текст рассылки.\n/cancel — отмена.")

    # ── By activity ───────────────────────────────────────────────────────────
    elif data == "adm_bcast_by_act":
        with con() as c:
            active7 = c.execute(
                "SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 "
                "AND last_active != '' AND date(last_active) >= date('now', '-7 days')"
            ).fetchone()[0]
            inactive30 = c.execute(
                "SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 "
                "AND (last_active='' OR date(last_active) < date('now', '-30 days'))"
            ).fetchone()[0]
            inactive7_30 = c.execute(
                "SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 "
                "AND last_active != '' AND date(last_active) < date('now', '-7 days') "
                "AND date(last_active) >= date('now', '-30 days')"
            ).fetchone()[0]
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
        with con() as c:
            if seg == "active":
                cnt = c.execute(
                    "SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 "
                    "AND last_active != '' AND date(last_active) >= date('now', '-7 days')"
                ).fetchone()[0]
            elif seg == "churn":
                cnt = c.execute(
                    "SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 "
                    "AND last_active != '' AND date(last_active) < date('now', '-7 days') "
                    "AND date(last_active) >= date('now', '-30 days')"
                ).fetchone()[0]
            else:
                cnt = c.execute(
                    "SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 "
                    "AND (last_active='' OR date(last_active) < date('now', '-30 days'))"
                ).fetchone()[0]
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
        with con() as c:
            rows = c.execute(
                "SELECT user_id,username,display_name FROM users WHERE is_blocked=1"
            ).fetchall()
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

    elif data == "adm_back":
        await q.edit_message_text("АДМИН ПАНЕЛЬ", reply_markup=admin_kb())


def _get_uids_for_seg(seg: str) -> list[int]:
    with con() as c:
        if seg == "all":
            rows = c.execute(
                "SELECT user_id FROM users WHERE is_registered=1 AND is_blocked=0"
            ).fetchall()
        elif seg.startswith("lang:"):
            code = seg[5:]
            rows = c.execute(
                "SELECT user_id FROM users WHERE is_registered=1 AND is_blocked=0 AND lang=?", (code,)
            ).fetchall()
        elif seg.startswith("sport:"):
            code = seg[6:]
            rows = c.execute(
                "SELECT user_id FROM users WHERE is_registered=1 AND is_blocked=0 AND sports=?", (code,)
            ).fetchall()
        elif seg == "act:active":
            rows = c.execute(
                "SELECT user_id FROM users WHERE is_registered=1 AND is_blocked=0 "
                "AND last_active != '' AND date(last_active) >= date('now', '-7 days')"
            ).fetchall()
        elif seg == "act:churn":
            rows = c.execute(
                "SELECT user_id FROM users WHERE is_registered=1 AND is_blocked=0 "
                "AND last_active != '' AND date(last_active) < date('now', '-7 days') "
                "AND date(last_active) >= date('now', '-30 days')"
            ).fetchall()
        elif seg == "act:sleep":
            rows = c.execute(
                "SELECT user_id FROM users WHERE is_registered=1 AND is_blocked=0 "
                "AND (last_active='' OR date(last_active) < date('now', '-30 days'))"
            ).fetchall()
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
