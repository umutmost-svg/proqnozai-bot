import asyncio
import logging
import time

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import ADMIN_ID, MOSTBET_BASE, live_subs, blocked_until, mostbet_cache
from db import db_set, db_stats, db_search, db_all_uids, con
from translations import sport_label, exp_label
from mostbet import _mostbet_load_matches

logger = logging.getLogger(__name__)


def is_adm(update):
    return (update.effective_user.id if update.effective_user else 0) == ADMIN_ID


def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Статистика",             callback_data="adm_stats")],
        [InlineKeyboardButton("Рассылка — Все",         callback_data="adm_broadcast_all")],
        [InlineKeyboardButton("Рассылка по языку/гео",  callback_data="adm_broadcast_geo")],
        [InlineKeyboardButton("Заблокированные",        callback_data="adm_blocklist")],
        [InlineKeyboardButton("Поиск пользователя",     callback_data="adm_search")],
        [InlineKeyboardButton("Изменить язык",           callback_data="adm_setlang")],
        [InlineKeyboardButton("Live подписки",           callback_data="adm_live")],
        [InlineKeyboardButton("Тест Mostbet API",        callback_data="adm_test_mostbet")],
    ])


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_adm(update): return
    await update.message.reply_text("АДМИН ПАНЕЛЬ", reply_markup=admin_kb())


async def adm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("Нет доступа", show_alert=True); return
    await q.answer(); data = q.data
    back = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="adm_back")]])

    if data == "adm_stats":
        s = db_stats()
        blk_now = sum(1 for v in blocked_until.values() if time.time() < v)
        live_now = sum(len(v) for v in live_subs.values())
        lang_str = " | ".join(f"{l}: {n}" for l, n in s["langs"])
        top = "\n".join(f"{i+1}. {r[1] or r[0]}: {r[2]} запросов" for i, r in enumerate(s["top_req"]))
        await q.edit_message_text(
            f"СТАТИСТИКА\n\n"
            f"Пользователей: {s['total']}\n"
            f"Новых сегодня: {s['today']}\n"
            f"Заблокировано: {s['blocked']}\n"
            f"Онбординг: {s['ob_done']}\n\n"
            f"Запросов всего: {s['rqtotal']}\n"
            f"Сегодня: {s['rqtoday']}\n\n"
            f"Языки: {lang_str}\n\n"
            f"Live подписки: {s['live_ct']} (активных: {live_now})\n"
            f"Rate-limit блок: {blk_now}\n\n"
            f"Топ активных:\n{top}",
            reply_markup=back)

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
        btns.append([InlineKeyboardButton("Назад", callback_data="adm_back")])
        lines = ["ЗАБЛОКИРОВАННЫЕ:"] + [f"- {r[2] or r[1] or r[0]} (id={r[0]})" for r in rows]
        await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("adm_unblk_"):
        uid = int(data.split("_")[2]); db_set(uid, "is_blocked", 0)
        await q.edit_message_text(f"Пользователь {uid} разблокирован.", reply_markup=back)

    elif data.startswith("adm_blk_"):
        uid = int(data.split("_")[2]); db_set(uid, "is_blocked", 1)
        await q.edit_message_text(f"Пользователь {uid} заблокирован.", reply_markup=back)

    elif data == "adm_broadcast_all":
        context.user_data["adm_act"] = "broadcast_all"
        with con() as c:
            cnt = c.execute(
                "SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0"
            ).fetchone()[0]
        await q.edit_message_text(
            f"РАССЫЛКА ВСЕМ\n\nПолучателей: {cnt}\n\nОтправьте текст. /cancel — отмена.")

    elif data == "adm_broadcast_geo":
        geo_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Azərbaycan (az)", callback_data="adm_bcast_az")],
            [InlineKeyboardButton("Русский (ru)",    callback_data="adm_bcast_ru")],
            [InlineKeyboardButton("English (en)",    callback_data="adm_bcast_en")],
            [InlineKeyboardButton("Türkçe (tr)",     callback_data="adm_bcast_tr")],
            [InlineKeyboardButton("Қазақша (kz)",    callback_data="adm_bcast_kz")],
            [InlineKeyboardButton("O'zbek (uz)",     callback_data="adm_bcast_uz")],
            [InlineKeyboardButton("العربية (ar)",    callback_data="adm_bcast_ar")],
            [InlineKeyboardButton("Назад",           callback_data="adm_back")],
        ])
        with con() as c:
            langs = c.execute(
                "SELECT lang, COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 GROUP BY lang"
            ).fetchall()
        lang_counts = {l: n for l, n in langs}
        lines = ["РАССЫЛКА ПО ГЕО\n\nВыберите аудиторию:\n"]
        for code, name in [("az", "Azərbaycan"), ("ru", "Русский"), ("en", "English"),
                           ("tr", "Türkçe"), ("kz", "Қазақша"), ("uz", "O'zbek"), ("ar", "العربية")]:
            lines.append(f"{name}: {lang_counts.get(code, 0)} чел.")
        await q.edit_message_text("\n".join(lines), reply_markup=geo_kb)

    elif data.startswith("adm_bcast_"):
        lang_code = data.split("_")[2]
        with con() as c:
            cnt = c.execute(
                "SELECT COUNT(*) FROM users WHERE is_registered=1 AND is_blocked=0 AND lang=?",
                (lang_code,)).fetchone()[0]
        lang_names = {"az": "Azərbaycan", "ru": "Русский", "en": "English",
                      "tr": "Türkçe", "kz": "Қазақша", "uz": "O'zbek", "ar": "العربية"}
        context.user_data["adm_act"] = f"broadcast_geo_{lang_code}"
        await q.edit_message_text(
            f"РАССЫЛКА: {lang_names.get(lang_code, lang_code)}\n\nПолучателей: {cnt}\n\n"
            f"Отправьте текст. /cancel — отмена.")

    elif data == "adm_search":
        context.user_data["adm_act"] = "search"
        await q.edit_message_text("Введите ID, username или имя.")

    elif data == "adm_setlang":
        context.user_data["adm_act"] = "setlang"
        await q.edit_message_text("Формат: 123456789 ru\nЯзыки: az, ru, en")

    elif data == "adm_live":
        live_now = sum(len(v) for v in live_subs.values())
        lines = [f"LIVE ПОДПИСКИ: {live_now} активных\n"]
        for mid, uids in live_subs.items():
            if uids: lines.append(f"Матч {mid}: {len(uids)} подписчиков")
        await q.edit_message_text(
            "\n".join(lines) if len(lines) > 1 else "Нет активных.", reply_markup=back)

    elif data == "adm_test_mostbet":
        await q.edit_message_text("Тестирую Mostbet API...")
        try:
            matches = await _mostbet_load_matches()
            if not matches:
                await q.edit_message_text(
                    "MOSTBET API\n\nСтатус: НЕТ ДАННЫХ\n\nВозможные причины:\n"
                    "- 429 Rate limit\n- IP не в whitelist\n- Проблемы с сетью",
                    reply_markup=back)
            else:
                sample = matches[:5]
                lines = [f"MOSTBET API\n\nСтатус: РАБОТАЕТ\nВсего матчей: {len(matches)}\n\nПримеры:"]
                for m in sample:
                    t1 = m.get("team1Title", "?"); t2 = m.get("team2Title", "?")
                    league = m.get("lineSubCategory", "")
                    live = "LIVE" if m.get("isLive") else "Pre"
                    lines.append(f"[{live}] {t1} vs {t2} ({league})")
                cache_ts = mostbet_cache.get("all_matches", (0, []))[0]
                if cache_ts:
                    age = int(time.time() - cache_ts)
                    lines.append(f"\nКэш: {age} сек назад")
                await q.edit_message_text("\n".join(lines), reply_markup=back)
        except Exception as e:
            await q.edit_message_text(f"MOSTBET API\n\nОшибка: {e}", reply_markup=back)

    elif data == "adm_back":
        await q.edit_message_text("АДМИН ПАНЕЛЬ", reply_markup=admin_kb())


async def handle_adm_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_adm(update): return
    act = context.user_data.get("adm_act")
    if not act: return
    context.user_data.pop("adm_act")
    text = update.message.text or ""

    if act == "broadcast_all":
        uids = db_all_uids()
        status = await update.message.reply_text(f"Рассылка для {len(uids)} пользователей...")
        ok = fail = 0
        for uid in uids:
            try:
                await context.bot.send_message(chat_id=uid, text=text); ok += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.05)
        await status.edit_text(f"Готово! Доставлено: {ok} | Не доставлено: {fail}")

    elif act.startswith("broadcast_geo_"):
        lang_code = act.split("_")[2]
        with con() as c:
            uids = [r[0] for r in c.execute(
                "SELECT user_id FROM users WHERE is_registered=1 AND is_blocked=0 AND lang=?",
                (lang_code,)).fetchall()]
        lang_names = {"az": "Azərbaycan", "ru": "Русский", "en": "English",
                      "tr": "Türkçe", "kz": "Қазақша", "uz": "O'zbek", "ar": "العربية"}
        status = await update.message.reply_text(
            f"Рассылка [{lang_names.get(lang_code, lang_code)}]: {len(uids)} пользователей...")
        ok = fail = 0
        for uid in uids:
            try:
                await context.bot.send_message(chat_id=uid, text=text); ok += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.05)
        await status.edit_text(
            f"Готово! [{lang_names.get(lang_code, lang_code)}]\n"
            f"Доставлено: {ok} | Не доставлено: {fail}")

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
                f"Язык: {u['lang']}\n"
                f"Статус: {'ЗАБЛОКИРОВАН' if u['is_blocked'] else 'Активен'}\n"
                f"Спорт: {sport_label(u['user_id'], u['sports']) if u['sports'] else '-'}\n"
                f"Опыт: {exp_label(u['user_id'], u['experience']) if u['experience'] else '-'}\n"
                f"Запросов: {u['total_requests']}\n"
                f"Зарегистрирован: {u['joined_at']}",
                reply_markup=InlineKeyboardMarkup(btns))

    elif act == "setlang":
        parts = text.strip().split()
        if len(parts) != 2 or parts[1] not in ("az", "ru", "en"):
            await update.message.reply_text("Формат: 123456789 ru"); return
        db_set(int(parts[0]), "lang", parts[1])
        await update.message.reply_text(f"Язык {parts[0]} изменён на {parts[1]}.")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("adm_act", None)
    await update.message.reply_text("Отменено.")


async def testapi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test Mostbet API directly - admin only."""
    if not is_adm(update): return
    await update.message.reply_text("Тестирую Mostbet API напрямую...")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as h:
            r = await h.get(
                f"{MOSTBET_BASE}/api/v3/advertiser/oddschecker/line/list",
                headers={"Accept": "application/json", "User-Agent": "ProqnozAI/1.0"},
                params={"lastId": 0, "locale": "ru", "limit": 3}
            )
            status = r.status_code
            if status == 200:
                data = r.json()
                matches = data.get("lineMatches", [])
                lines = [f"Mostbet API: OK ({status})\nМатчей в ответе: {len(matches)}\n"]
                for m in matches[:3]:
                    t1 = m.get("team1Title", "?"); t2 = m.get("team2Title", "?")
                    league = m.get("lineSubCategory", "")
                    live = "LIVE" if m.get("isLive") else "Pre-match"
                    lines.append(f"[{live}] {t1} vs {t2} ({league})")
                await update.message.reply_text("\n".join(lines))
            else:
                deny = r.headers.get("x-deny-reason", "")
                await update.message.reply_text(
                    f"Mostbet API: ОШИБКА\nСтатус: {status}\nПричина: {deny}\n"
                    f"Ответ: {r.text[:200]}")
    except Exception as e:
        await update.message.reply_text(f"Mostbet API: ИСКЛЮЧЕНИЕ\n{e}")
