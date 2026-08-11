import asyncio
import logging
from datetime import datetime, timedelta, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import live_subs, ht_sent
from db import (
    db_lang, db_is_reg, db_user_lsubs, db_add_lsub, db_del_lsub, con,
    db_filter_new_live_events, db_clear_live_events, db_purge_stale_live_events,
    db_lsub_name,
)
from translations import T, tr
from football_api import get_status, get_events
from mostbet import mostbet_get_odds
from claude_client import live_tip
from handlers.utils import nav_guard

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
    q = update.callback_query
    # watch_ does a Mostbet odds fetch + DB writes, so throttle it like menu
    # navigation (generous budget, no auto-block) instead of leaving it
    # completely unguarded against rapid watch/unwatch spam.
    if not await nav_guard(update):
        return
    await q.answer(); uid = q.from_user.id
    if q.data.startswith("watch_"):
        mid = q.data[6:]; mname = context.user_data.get(f"mn_{mid}", mid)
        mostbet_line_id = context.user_data.get(f"mb_line_{mid}")
        live_subs[mid].add(uid); db_add_lsub(uid, mid, mname)
        try:
            if not mostbet_line_id:
                raise ValueError("missing Mostbet line id")
            odds = await mostbet_get_odds(int(mostbet_line_id))
            with con() as c:
                for market, odd in [("w1", odds["w1"]), ("over25", odds["over25"])]:
                    if odd:
                        c.execute(
                            "INSERT OR REPLACE INTO odds_alerts "
                            "(user_id, match_id, market, last_odd, created_at, fixture_id, match_name) "
                            "VALUES (?,?,?,?,datetime('now'),?,?)",
                            (uid, str(mostbet_line_id), market, odd, mid, mname))
        except Exception:
            pass
        await q.edit_message_text((q.message.text or "") + "\n\n" + tr(uid, "watch_started", match=mname))
    elif q.data.startswith("unwatch_"):
        mid = q.data[8:]
        mname = next((s["match_name"] for s in db_user_lsubs(uid) if s["match_id"] == mid), mid)
        live_subs[mid].discard(uid); db_del_lsub(uid, mid)
        with con() as c:
            c.execute("DELETE FROM odds_alerts WHERE user_id=? AND fixture_id=?", (uid, mid))
        await q.edit_message_text(tr(uid, "watch_stopped", match=mname))


# Card colour per language. All 7 languages are covered (CLAUDE.md: a local
# dict either covers every language or falls back to ru) — the previous version
# had az/ru/en only and showed tr/kz/uz/ar users the bare English word "Card".
_CARD_COLOURS: dict[str, tuple[str, str]] = {   # lang -> (red, yellow)
    "az": ("Qırmızı", "Sarı"),
    "ru": ("Красная", "Жёлтая"),
    "en": ("Red", "Yellow"),
    "tr": ("Kırmızı", "Sarı"),
    "kz": ("Қызыл", "Сары"),
    "uz": ("Qizil", "Sariq"),
    "ar": ("حمراء", "صفراء"),
}


def _card_colour(lang: str, detail: str) -> str:
    red, yellow = _CARD_COLOURS.get(lang, _CARD_COLOURS["ru"])
    return red if "Red" in detail else yellow


def _event_key(ev: dict) -> str:
    """Stable identity for one live event.

    api-football's /fixtures/events entries carry no event id, so identity is
    derived from the fields that don't move once an event has happened: minute
    (incl. stoppage-time offset), type/detail, team, player. Deriving the key
    from content rather than list position is what makes the de-duplication
    survive a reordered or shortened provider response — position-based
    comparison treated both as "new events"."""
    provider_id = ev.get("id") or ev.get("event_id")
    if provider_id:
        return f"id:{provider_id}"
    t = ev.get("time") or {}
    parts = (
        str(ev.get("type") or ""),
        str(ev.get("detail") or ""),
        str((ev.get("team") or {}).get("name") or ""),
        str((ev.get("player") or {}).get("name") or ""),
        str(t.get("elapsed") if t.get("elapsed") is not None else ""),
        str(t.get("extra") or 0),
    )
    return "|".join(parts)


async def poller(app):
    while True:
        await asyncio.sleep(60)
        if not live_subs: continue
        for mid, uids in list(live_subs.items()):
            if not uids: continue
            try:
                st = await get_status(mid)
                if not st: continue
                score = st["score"]; minute = st["minute"] or 0; status = st["status"]
                # One lookup for the match, not one per subscriber: the old loop
                # ran a query per uid every minute just to read a name that is
                # the same for all of them.
                match_name = db_lsub_name(mid) or f"{st['home']} vs {st['away']}"

                evs = await get_events(mid)
                # Persisted, content-keyed de-duplication: only events this
                # match has never notified about survive, and the record of
                # them survives a restart.
                keys = {}
                for ev in evs:
                    keys.setdefault(_event_key(ev), ev)
                fresh = db_filter_new_live_events(mid, list(keys))
                new_evs = [keys[k] for k in fresh]

                for ev in new_evs:
                    etype = ev.get("type", ""); detail = ev.get("detail", "")
                    team  = ev.get("team", {}).get("name", "")
                    player = ev.get("player", {}).get("name", "")
                    ev_min = ev.get("time", {}).get("elapsed", minute)
                    # One tip per language, so every subscriber gets it in their own.
                    tips: dict[str, str] = {}
                    for uid in list(uids):
                        lang = db_lang(uid)
                        if lang not in tips:
                            tips[lang] = await live_tip(uid, match_name, ev_min, score,
                                                        f"{etype}-{detail}-{team}")
                        tip = tips[lang]
                        try:
                            if etype == "Goal":
                                msg = T[lang]["live_goal"].format(
                                    match=match_name, minute=ev_min, team=team,
                                    score=score, tip=tip)
                            elif etype == "Card":
                                card = _card_colour(lang, detail)
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
                    tips = {}
                    for uid in list(uids):
                        lang = db_lang(uid)
                        if lang not in tips:
                            tips[lang] = await live_tip(uid, match_name, 45, score, "Half time")
                        tip = tips[lang]
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
                        db_del_lsub(uid, mid)
                    # Match over: drop every trace so nothing leaks or keeps alerting.
                    with con() as c:
                        c.execute("DELETE FROM odds_alerts WHERE fixture_id=?", (mid,))
                    db_clear_live_events(mid)
                    live_subs.pop(mid, None)
                    ht_sent.discard(mid)
            except Exception as e:
                logger.error(f"poller mid={mid}: {e}")


async def check_odds_changes(app):
    """Background: check if odds changed significantly for subscribed matches."""
    while True:
        await asyncio.sleep(300)
        try:
            with con() as c:
                # Safety net: drop alerts whose match is long over (covers rows
                # created before fixture_id existed and matches that never hit FT).
                c.execute("DELETE FROM odds_alerts WHERE created_at < datetime('now','-7 days')")
            db_purge_stale_live_events()
            with con() as c:
                alerts = c.execute(
                    "SELECT user_id, match_id, market, last_odd, match_name FROM odds_alerts"
                ).fetchall()
            # Fetch each match's odds ONCE per cycle. Alert rows are per
            # (user × market), so ten subscribers on one match used to mean ten
            # identical fetches; only the TTL cache kept that from being ten
            # round-trips. Group first, then fan the result out.
            odds_by_match: dict[str, dict] = {}
            for mid in {a[1] for a in alerts}:
                try:
                    odds_by_match[mid] = await mostbet_get_odds(int(mid))
                except Exception:
                    continue  # this match is unavailable this cycle

            for uid, mid, market, last_odd, mname in alerts:
                try:
                    odds = odds_by_match.get(mid)
                    if not odds:
                        continue
                    market_map = {
                        "w1": odds["w1"], "x": odds["x"], "w2": odds["w2"],
                        "over25": odds["over25"], "under25": odds["under25"],
                    }
                    new_odd = market_map.get(market)
                    if new_odd and last_odd and abs(new_odd - last_odd) >= 0.3:
                        lang = db_lang(uid)
                        direction = "↑" if new_odd > last_odd else "↓"
                        label = mname or mid
                        msgs = {
                            "ru": f"ИЗМЕНЕНИЕ КОЭФФИЦИЕНТА {direction}\nМатч: {label}\nРынок: {market}\nБыло: {last_odd} → Стало: {new_odd}\nРазница: {abs(new_odd-last_odd):.2f}",
                            "en": f"ODDS CHANGE {direction}\nMatch: {label}\n{market}: {last_odd} → {new_odd}",
                            "az": f"ƏMSAL DƏYİŞDİ {direction}\nMatç: {label}\n{market}: {last_odd} → {new_odd}",
                            "tr": f"ORAN DEĞİŞTİ {direction}\nMaç: {label}\n{market}: {last_odd} → {new_odd}",
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
    """Nudge inactive users at 10:00 in THEIR timezone (users.tz_offset)."""
    msgs = {
        "az": "Bugun maraqli oyunlar var! Proqnoz ucun yazin.",
        "ru": "Сегодня интересные матчи! Напишите для прогноза.",
        "en": "Interesting matches today! Write for a forecast.",
    }
    sent: set[tuple[str, int]] = set()  # (local date iso, uid) already pushed
    while True:
        await asyncio.sleep(60)
        now_utc = datetime.now(timezone.utc)
        try:
            with con() as c:
                rows = c.execute(
                    "SELECT user_id,lang,tz_offset FROM users WHERE is_registered=1 AND is_blocked=0 "
                    "AND (last_active='' OR date(last_active) <= date('now', '-2 days'))"
                ).fetchall()
            for uid, lang, tz in rows:
                local = now_utc + timedelta(hours=tz or 0)
                if local.hour != 10:
                    continue
                key = (local.date().isoformat(), uid)
                if key in sent:
                    continue
                sent.add(key)
                try:
                    await app.bot.send_message(chat_id=uid, text=msgs.get(lang, msgs["ru"]))
                    await asyncio.sleep(0.1)
                except Exception:
                    pass
            # Keep the dedup set from growing across days.
            cutoff = (now_utc - timedelta(days=2)).date().isoformat()
            sent = {k for k in sent if k[0] >= cutoff}
        except Exception as e:
            logger.error(f"daily_push: {e}")
