"""Telegram bot package — exposes the statement analyzer through Telegram.

Runs in-process inside the FastAPI app (see app.main lifespan) when
TELEGRAM_BOT_TOKEN is set. Reuses the existing analyzer services directly.
"""
