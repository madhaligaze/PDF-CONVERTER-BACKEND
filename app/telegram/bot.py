"""Bot bootstrap: builds the aiogram Bot/Dispatcher and runs long-polling.

Started as a background task from the FastAPI lifespan (app.main) when
TELEGRAM_BOT_TOKEN is configured. Runs in the same process/event loop as the
web server, so it must never block the loop (handlers offload heavy work).
"""
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from app.core.config import settings
from app.telegram import handlers

log = logging.getLogger(__name__)

_DEFAULT_COMMANDS = [
    BotCommand(command="start", description="Начать / помощь"),
    BotCommand(command="history", description="Последние выписки"),
    BotCommand(command="help", description="Как пользоваться"),
]


def is_enabled() -> bool:
    return bool(settings.telegram_bot_token)


async def start_polling() -> None:
    """Run the bot until the surrounding task is cancelled (app shutdown)."""
    if not is_enabled():
        log.info("Telegram bot disabled (no TELEGRAM_BOT_TOKEN)")
        return

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(handlers.router)

    try:
        # Ensure polling works even if a webhook was previously registered.
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_my_commands(_DEFAULT_COMMANDS)
        me = await bot.get_me()
        log.info("Telegram bot started (polling) as @%s", me.username)
        # handle_signals=False: we run inside uvicorn, not as the main entrypoint.
        await dispatcher.start_polling(bot, handle_signals=False)
    except Exception:  # noqa: BLE001
        log.exception("Telegram bot polling stopped with an error")
    finally:
        await bot.session.close()
        log.info("Telegram bot session closed")
