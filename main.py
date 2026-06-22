import asyncio
import os

from telegram.ext import ApplicationBuilder
from telegram.error import TelegramError

from config import TELEGRAM_TOKEN, MOSTBET_CACHE_TTL
from db import db_init, db_restore_live_subs, db_all_uids, db_lang
from mostbet import _mostbet_load_matches
from handlers import register_handlers
from handlers.live import poller, check_odds_changes, daily_push
from handlers.utils import main_menu
from translations import T

import logging
logger = logging.getLogger(__name__)


async def _broadcast_menu_update(application):
    """Send updated menu keyboard to all registered users on bot start."""
    await asyncio.sleep(5)
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


async def _preload_mostbet():
    """Preload Mostbet matches at startup, then refresh every 15 min."""
    await asyncio.sleep(10)
    while True:
        logger.info("Loading Mostbet matches...")
        matches = await _mostbet_load_matches()
        logger.info(f"Mostbet loaded: {len(matches)} matches")
        await asyncio.sleep(MOSTBET_CACHE_TTL)


def main():
    db_init()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    register_handlers(app)

    async def post_init(application):
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
