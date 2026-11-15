"""Spor AI Mini App backend: a service separate from the Telegram bot.

It never imports the bot's modules (config, db, handlers) and uses its own
SQLite file. See docs/miniapp/auth.md.
"""
