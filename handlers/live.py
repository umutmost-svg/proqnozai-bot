import asyncio
import logging
from collections import defaultdict
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import live_subs, ht_sent, last_events
from db import (
    db_lang, db_is_reg, db_user_lsubs, db_add_lsub, db_del_lsub, con
)
from translations import T, tr
from football_api import get_status, get_events
from mostbet import mostbet_get_odds
from claude_client import live_tip

logger = logging.getLogger(__name__)


async def matches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db_is_reg(uid):
        await update.message.reply_text(tr(uid, "need_reg")); return
    subs = db_user_lsubs(uid)
    if not subs:
        await update.message.reply_text(tr(uid, "no_subs")); return
    lines = []; btns = []
    for s in subs:
        lines.append(f"- {s['match_name']}")
        btns.append([InlineKeyboardButton(f"X {s['match_name'][:30]}",
                                          callback_data=f"unwatch_{s['match_id']}")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(btns))


async def watch_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); uid = q.from_user.id
    if q.data.startswith("watch_"):
        mid = q.data[6:]; mname = context.user_data.get(f"mn_{mid}", mid)
        live_subs[mid].add(uid); db_add_lsub(uid, mid, mname)
        try:
            odds = await mostbet_get_odds(int(mid))
            with con() as c:
                for market, odd in [("w1", odds["w1"]), ("over25", odds["over25"])]:
                    if odd:
                        c.execute(
                            "INSERT OR REPLACE INTO odds_alerts VALUES (?,?,?,?,datetime('now'))",
                            (uid, mid, market, odd))
        except Exception:
            pass
        await q.edit_message_text(q.message.text + "\n\n" + tr(uid, "watch_started", match=mname))
    elif q.data.startswith("unwatch_"):
        mid = q.data[8:]
        mname = next((s["match_name"] for s in db_user_lsubs(uid) if s["match_id"] == mid), mid)
        live_subs[mid].discard(uid); db_del_lsub(uid, mid)
        await q.edit_message_text(tr(uid, "watch_stopped", match=mname))


async def poller(app):
    alert_cnt: dict[str, int] = defaultdict(int)
    while True:
        await asyncio.sleep(60)
        if not live_subs: continue
        for mid, uids in list(live_subs.items()):
            if not uids: continue
            try:
                st = await get_status(mid)
                if not st: continue
                score = st["score"]; minute = st["minute"] or 0; status = st["status"]
                match_name = None
                for uid in uids:
                    for s in db_user_lsubs(uid):
                        if s["match_id"] == mid: match_name = s["match_name"]; break
                    if match_name: break
                if not match_name: match_name = f"{st['home']} vs {st['away']}"

                evs = await get_events(mid)
                prev = last_events.get(mid, [])
                new_evs = evs[len(prev):]
                last_events[mid] = evs

                for ev in new_evs:
                    etype = ev.get("type", ""); detail = ev.get("detail", "")
                    team  = ev.get("team", {}).get("name", "")
                    player = ev.get("player", {}).get("name", "")
                    ev_min = ev.get("time", {}).get("elapsed", minute)
                    tip = await live_tip(next(iter(uids)), match_name, ev_min, score,
                                        f"{etype}-{detail}-{team}")
                    for uid in list(uids):
                        lang = db_lang(uid)
                        try:
                            if etype == "Goal":
                                msg = T[lang]["live_goal"].format(
                                    match=match_name, minute=ev_min, team=team,
                                    score=score, tip=tip)
                            elif etype == "Card":
                                card = {
                                    "az": "Qirmizi" if "Red" in detail else "Sari",
                                    "ru": "Красная" if "Red" in detail else "Жёлтая",
                                    "en": "Red" if "Red" in detail else "Yellow",
                                }.get(lang, "Card")
                                msg = T[lang]["live_card"].format(
                                    match=match_name, minute=ev_min, player=player,
                                    team=team, card=card, tip=tip)
                            else:
                                continue
                            await app.bot.send_message(chat_id=uid, text=msg)
                        except Exception as e:
                            logger.error(f"notify uid={uid}: {e}")

                if status == "HT" and mid not in ht_sent:
                    ht_sent.add(mid)
                    for uid in list(uids):
                        lang = db_lang(uid)
                        tip = await live_tip(uid, match_name, 45, score, "Half time")
                        try:
                            await app.bot.send_message(
                                chat_id=uid,
                                text=T[lang]["live_halftime"].format(
                                    match=match_name, score=score, tip=tip))
                        except Exception:
                            pass

                if status in ("FT", "AET", "PEN"):
                    for uid in list(uids):
                        lang = db_lang(uid)
                        try:
                            await app.bot.send_message(
                                chat_id=uid,
                                text=T[lang]["live_fulltime"].format(
                                    match=match_name, score=score))
                        except Exception:
                            pass
                        db_del_lsub(uid, mid); uids.discard(uid)
                    live_subs[mid].clear()
            except Exception as e:
                logger.error(f"poller mid={mid}: {e}")


async def check_odds_changes(app):
    """Background: check if odds changed significantly for subscribed matches."""
    while True:
        await asyncio.sleep(300)
        try:
            with con() as c:
                alerts = c.execute(
                    "SELECT user_id, match_id, market, last_odd FROM odds_alerts"
                ).fetchall()
            for uid, mid, market, last_odd in alerts:
                try:
                    odds = await mostbet_get_odds(int(mid))
                    market_map = {
                        "w1": odds["w1"], "x": odds["x"], "w2": odds["w2"],
                        "over25": odds["over25"], "under25": odds["under25"],
                    }
                    new_odd = market_map.get(market)
                    if new_odd and last_odd and abs(new_odd - last_odd) >= 0.3:
                        lang = db_lang(uid)
                        direction = "↑" if new_odd > last_odd else "↓"
                        msgs = {
                            "ru": f"ИЗМЕНЕНИЕ КОЭФФИЦИЕНТА {direction}\nМатч: {mid}\nРынок: {market}\nБыло: {last_odd} → Стало: {new_odd}\nРазница: {abs(new_odd-last_odd):.2f}",
                            "en": f"ODDS CHANGE {direction}\nMatch: {mid}\n{market}: {last_odd} → {new_odd}",
                            "az": f"KEF DƏYİŞDİ {direction}\nMatç: {mid}\n{market}: {last_odd} → {new_odd}",
                        }
                        await app.bot.send_message(chat_id=uid, text=msgs.get(lang, msgs["ru"]))
                        with con() as c:
                            c.execute(
                                "UPDATE odds_alerts SET last_odd=? WHERE user_id=? AND match_id=? AND market=?",
                                (new_odd, uid, mid, market))
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"check_odds_changes: {e}")


async def daily_push(app):
    _last_sent_date = None
    while True:
        await asyncio.sleep(60)
        now = datetime.now()
        today = now.date()
        if now.hour != 10 or _last_sent_date == today:
            continue
        _last_sent_date = today
        msgs = {
            "az": "Bugun maraqli oyunlar var! Proqnoz ucun yazin.",
            "ru": "Сегодня интересные матчи! Напишите для прогноза.",
            "en": "Interesting matches today! Write for a forecast.",
        }
        try:
            with con() as c:
                rows = c.execute(
                    "SELECT user_id,lang FROM users WHERE is_registered=1 AND is_blocked=0 "
                    "AND (last_active='' OR date(last_active) <= date('now', '-2 days'))"
                ).fetchall()
            for uid, lang in rows:
                try:
                    await app.bot.send_message(chat_id=uid, text=msgs.get(lang, msgs["ru"]))
                    await asyncio.sleep(0.1)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"daily_push: {e}")
