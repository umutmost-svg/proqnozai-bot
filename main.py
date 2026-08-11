import asyncio
import os
import threading

from telegram.ext import ApplicationBuilder
from telegram.error import TelegramError

from config import TELEGRAM_TOKEN, MOSTBET_CACHE_TTL
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
MENU_BROADCAST_KEY = "menu_broadcast_2026_08_partners_menu_v2"


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
        db_restore_live_subs()
        asyncio.create_task(poller(application))
        asyncio.create_task(daily_push(application))
        asyncio.create_task(_preload_mostbet())
        asyncio.create_task(check_odds_changes(application))
        asyncio.create_task(_broadcast_menu_update(application))

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
