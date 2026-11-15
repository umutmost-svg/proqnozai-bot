import asyncio
import os
import threading
import time

from telegram.ext import ApplicationBuilder
from telegram.error import TelegramError

from config import BOT_LAUNCHER_MODE, BOT_OUTBOX_ENABLED, MOSTBET_CACHE_TTL, TELEGRAM_TOKEN
from db import db_init, db_restore_live_subs, db_all_uids, db_lang, db_flag_done, db_flag_mark
from mostbet import _mostbet_load_matches
from handlers import register_handlers
from handlers.live import poller, check_odds_changes, daily_push
from handlers.utils import main_menu
from translations import T, tr

import logging
import sys
logger = logging.getLogger(__name__)


def _deploy_version() -> str:
    """Best-effort build identifier for the startup banner, so a fresh deploy is
    identifiable in the logs: an explicit GIT_COMMIT env var if the platform set
    one, else the short git hash if the repo is present, else 'unknown'."""
    commit = os.environ.get("GIT_COMMIT")
    if commit:
        return commit[:12]
    try:
        import subprocess
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL, text=True, timeout=3).strip() or "unknown"
    except Exception:
        return "unknown"


# Bump this key whenever the main menu layout changes: the broadcast below runs
# once per key, not on every restart (a crash-loop must never spam the user base).
MENU_BROADCAST_KEY = "menu_broadcast_2026_08_express_removed"


async def _broadcast_menu_update(application):
    """Send updated menu keyboard to all registered users, once per menu version."""
    await asyncio.sleep(5)
    if db_flag_done(MENU_BROADCAST_KEY):
        return
    # Mark before sending so a crash mid-broadcast can't cause a repeat storm.
    db_flag_mark(MENU_BROADCAST_KEY)
    uids = db_all_uids()
    if not uids:
        return
    logger.info(f"Broadcasting menu update to {len(uids)} users...")
    sent = failed = 0
    for uid in uids:
        try:
            lang = db_lang(uid)
            text = T[lang].get("bot_updated", "Bot updated!")
            kb = main_menu(uid)
            await application.bot.send_message(chat_id=uid, text=text, reply_markup=kb)
            sent += 1
            await asyncio.sleep(0.05)  # 20 msg/sec to stay under Telegram limits
        except TelegramError as e:
            failed += 1
            if "bot was blocked" not in str(e).lower() and "chat not found" not in str(e).lower():
                logger.warning(f"broadcast uid={uid}: {e}")
    logger.info(f"Menu broadcast done: {sent} sent, {failed} failed")


# Retry delay after a failed refresh. Much shorter than the normal TTL: a failed
# fetch means the cached feed is already going stale, so we want back in sooner —
# but not so soon that a persistent outage turns into a hot retry loop.
PRELOAD_ERROR_BACKOFF = 60


async def _preload_mostbet():
    """Preload Mostbet matches at startup, then refresh every 15 min.

    One failed fetch must never kill the task: an exception escaping this loop
    would leave the bot serving a frozen match list until the next restart, and
    asyncio only surfaces a dead task's exception when it is garbage-collected —
    so the failure would also be invisible in the logs."""
    await asyncio.sleep(10)
    while True:
        try:
            logger.info("Loading Mostbet matches...")
            matches = await _mostbet_load_matches()
            logger.info(f"Mostbet loaded: {len(matches)} matches")
            delay = MOSTBET_CACHE_TTL
        except asyncio.CancelledError:
            raise  # shutdown, not a failure — never swallow it
        except Exception:
            logger.exception(
                f"Mostbet preload failed; retrying in {PRELOAD_ERROR_BACKOFF}s")
            delay = PRELOAD_ERROR_BACKOFF
        await asyncio.sleep(delay)


async def _error_handler(update, context):
    """Catch any unhandled exception so one bad update never crashes the bot
    or leaks a traceback to the user."""
    logger.error("Unhandled exception in handler", exc_info=context.error)
    try:
        if update and getattr(update, "effective_chat", None):
            user = getattr(update, "effective_user", None)
            text = tr(user.id, "api_error") if user else "⚠️ Error. Please try again."
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text="⚠️ " + text)
    except Exception:
        pass


def _log_db_location() -> None:
    """Log where bot.db resolves and its current size, so a deploy can confirm
    the SQLite file sits on a persistent volume (non-zero size that survives a
    redeploy = the mount is working; a fresh 0-byte DB every deploy = the volume
    is missing and user data is being wiped)."""
    from db import DB
    path = os.path.abspath(DB)
    if os.path.exists(DB):
        kb = os.path.getsize(DB) / 1024
        logger.info(f"DB: {path} (exists, {kb:.0f} KB) — verify this path is a persistent volume")
    else:
        logger.warning(f"DB: {path} (NEW/empty) — if this is not a mounted volume, data is lost on redeploy")


async def _launcher_menu(application) -> None:
    """Launcher mode: Telegram's menu button opens the Mini App and the "/" menu offers only /start."""
    from telegram import BotCommand, MenuButtonWebApp, WebAppInfo
    from telegram.error import TelegramError
    from config import MINIAPP_URL
    try:
        await application.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="ProqnozAI", web_app=WebAppInfo(url=MINIAPP_URL)))
        from translations import BOT_COMMANDS
        await application.bot.delete_my_commands()
        for lang in BOT_COMMANDS:  # the per-language lists the full bot published
            try:
                await application.bot.delete_my_commands(language_code={"kz": "kk"}.get(lang, lang))
            except TelegramError:
                pass
        await application.bot.set_my_commands([BotCommand("start", "ProqnozAI")])
    except TelegramError as e:
        logger.warning("launcher menu not set: %s", type(e).__name__)


async def _run_outbox(application) -> None:
    """Sends the admin panel's queued messages (bot_outbox.py: claim -> send -> record, at most once). About one
    batch a second (below Telegram's limits); texts are never logged."""
    from pathlib import Path
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    from bot_outbox import BotOutbox, Outcome
    from config import BOT_OUTBOX_DB, MINIAPP_URL
    from db import db_is_blocked

    path = Path(BOT_OUTBOX_DB)
    # The admin process creates the file (owned by its uid, 0660); the bot, running as root, never creates it — a
    # root-owned file would lock the admin out. Until it exists, look again every minute.
    while not path.is_file():
        logger.info("outbox file not there yet; waiting for the admin panel to create it")
        await asyncio.sleep(60)
    outbox = BotOutbox(path)
    while True:
        try:
            outbox.init_schema()
            break
        except Exception as e:  # noqa: BLE001 — an unreadable file must never stop the bot; retried every minute
            logger.error("outbox unavailable: %s", type(e).__name__)
            await asyncio.sleep(60)

    def markup(buttons: list[dict]):
        rows = []
        for b in buttons[:3]:
            if b.get("url") == "miniapp":
                rows.append([InlineKeyboardButton(str(b.get("text"))[:64], web_app=WebAppInfo(url=MINIAPP_URL))])
            elif str(b.get("url", "")).startswith("https://"):
                rows.append([InlineKeyboardButton(str(b.get("text"))[:64], url=str(b["url"]))])
        return InlineKeyboardMarkup(rows) if rows else None

    async def send(job) -> Outcome:
        msg = await application.bot.send_message(chat_id=job.user_id, text=job.text, reply_markup=markup(job.buttons))
        return Outcome("sent", message_id=msg.message_id)

    last_purge = 0
    while True:
        try:
            await outbox.run_once(send, lambda: int(time.time()), is_banned=db_is_blocked)
            if time.time() - last_purge > 86400:
                outbox.purge(int(time.time()))
                last_purge = time.time()
        except Exception as e:  # noqa: BLE001 — one failed run never ends the loop
            logger.warning("outbox run failed: %s", type(e).__name__)
        await asyncio.sleep(1)


async def _publish_commands(application) -> None:
    """Fill Telegram's own "/" menu. Without it the only discoverable command is
    /start, so /history and /tz existed but nobody could find them.
    Telegram picks the list by the CLIENT's language, so a per-language scope is
    published for each translation plus a default (ru) for everyone else."""
    from telegram import BotCommand
    from telegram.error import TelegramError
    from translations import BOT_COMMANDS

    def _cmds(lang: str):
        return [BotCommand(c, d) for c, d in BOT_COMMANDS[lang]]

    # Telegram scopes by ISO 639-1 LANGUAGE code, and our "kz" is a country
    # code — Kazakh is "kk". Publishing the wrong code is rejected, so map it.
    tg_code = {"kz": "kk"}

    # One try per call: an invalid/rejected scope must not abandon the scopes
    # after it in the loop. Cosmetic either way — the bot starts regardless.
    for lang in (None, *BOT_COMMANDS):
        try:
            if lang is None:
                await application.bot.set_my_commands(_cmds("ru"))   # default scope
            else:
                await application.bot.set_my_commands(
                    _cmds(lang), language_code=tg_code.get(lang, lang))
        except TelegramError as e:
            logger.warning(f"set_my_commands failed (lang={lang}): {e}")


def _start_odds_export() -> None:
    """Odds for the Mini App (signed push): off unless MINIAPP_ODDS_URL and its secret are set.
    Never allowed to stop the bot: any failure while setting it up only leaves the export off."""
    try:
        from mostbet_feed import from_env as odds_export_from_env, run_odds_export
        odds_export = odds_export_from_env()
        if odds_export is not None:
            asyncio.create_task(run_odds_export(odds_export))
    except Exception as exc:
        logger.error("odds_export.disabled reason=setup_failed type=%s", type(exc).__name__)


def main():
    logger.info(f"=== ProqnozAI worker booting | commit={_deploy_version()} | "
                f"python={sys.version.split()[0]} ===")
    _log_db_location()
    db_init()

    from stats_server import run_stats_server
    threading.Thread(target=run_stats_server, daemon=True, name="stats-server").start()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    register_handlers(app)
    app.add_error_handler(_error_handler)

    async def post_init(application):
        from stats_server import set_bot_app
        set_bot_app(application, asyncio.get_event_loop())
        if not BOT_LAUNCHER_MODE:
            # Launcher mode reads nothing from Mostbet: the Mini App's own source has the whole API budget.
            asyncio.create_task(_preload_mostbet())
            _start_odds_export()
        if BOT_OUTBOX_ENABLED:
            asyncio.create_task(_run_outbox(application))
        if BOT_LAUNCHER_MODE:
            # Only the app: no live polls, pushes, odds alerts or menu broadcasts; Telegram's menu button opens it.
            asyncio.create_task(_launcher_menu(application))
        else:
            db_restore_live_subs()
            asyncio.create_task(poller(application))
            asyncio.create_task(daily_push(application))
            asyncio.create_task(check_odds_changes(application))
            asyncio.create_task(_broadcast_menu_update(application))
            asyncio.create_task(_publish_commands(application))
        # Delayed broadcasts live in the DB, so anything scheduled before a
        # redeploy is still waiting here and starts at its time. Not in launcher mode: the admin panel's outbox is
        # the only sender then (an old scheduled broadcast would send the retired bot's content).
        if not BOT_LAUNCHER_MODE:
            from broadcast import scheduler as broadcast_scheduler
            asyncio.create_task(broadcast_scheduler(application))

    app.post_init = post_init

    WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
    PORT = int(os.environ.get("PORT", "8080"))

    if WEBHOOK_URL:
        logger.info(f"ProqnozAI started (webhook: {WEBHOOK_URL})")
        app.run_webhook(listen="0.0.0.0", port=PORT, webhook_url=WEBHOOK_URL)
    else:
        logger.info("ProqnozAI started (polling)")
        app.run_polling()


if __name__ == "__main__":
    main()
