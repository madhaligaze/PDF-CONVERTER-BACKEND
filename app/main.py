import asyncio
import contextlib
import logging
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.core.database import get_resolved_database_url, init_database

log = logging.getLogger(__name__)


def _run_migrations() -> None:
    """Apply pending Alembic migrations, fall back to create_all for SQLite."""
    log.info("Starting migrations...")
    db_url = get_resolved_database_url()
    db_driver = db_url.split("://")[0] if "://" in db_url else db_url
    log.info("DB URL resolved to driver: %s", db_driver)

    if db_url.startswith("sqlite"):
        log.info("SQLite detected, skipping alembic and running create_all")
        init_database()
        log.info("Database initialized with SQLite")
        return

    log.info("PostgreSQL detected, attempting alembic migrations")
    try:
        from alembic import command
        from alembic.config import Config

        alembic_cfg = Config("alembic.ini")
        alembic_cfg.set_main_option("sqlalchemy.url", db_url)
        log.info("Running alembic upgrade to head...")
        command.upgrade(alembic_cfg, "head")
        log.info("Alembic migrations applied successfully")
    except Exception as exc:
        log.warning("Alembic failed (%s: %s), falling back to create_all", type(exc).__name__, exc)
        log.debug(traceback.format_exc())
        try:
            log.info("Attempting create_all fallback...")
            init_database()
            log.info("create_all fallback succeeded")
        except Exception as exc2:
            log.error("create_all fallback also failed: %s: %s", type(exc2).__name__, exc2)
            log.error(traceback.format_exc())
            raise


async def _autocall_sync_loop() -> None:
    """Background daily sync of AutoCall.kz campaigns into Google Sheets.

    A single failure (network, Google, etc.) is logged and never crashes the app —
    we just wait for the next interval and retry.
    """
    from app.services import autocall_service

    interval = max(0.25, settings.autocall_auto_sync_interval_hours) * 3600
    while True:
        try:
            result = await autocall_service.sync_autocalls()
            log.info("autocall auto-sync: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — best-effort daily job
            log.warning("autocall auto-sync failed: %s", exc)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Warm up Smart NLP Correction Engine (fails silently if models unavailable)
    try:
        from app.services import smart_correction_service
        smart_correction_service.warmup()
    except Exception as exc:
        log.warning("smart_correction_service warmup skipped: %s", exc)

    # Start the Telegram bot in-process (only if TELEGRAM_BOT_TOKEN is set).
    bot_task: asyncio.Task | None = None
    try:
        from app.telegram import bot as telegram_bot
        if telegram_bot.is_enabled():
            bot_task = asyncio.create_task(telegram_bot.start_polling())
    except Exception as exc:
        log.warning("Telegram bot failed to start: %s", exc)

    # Start the daily AutoCall.kz → Google Sheets sync (only when enabled + configured).
    autocall_task: asyncio.Task | None = None
    if settings.autocall_auto_sync_enabled and settings.autocall_configured:
        autocall_task = asyncio.create_task(_autocall_sync_loop())

    log.info("Application startup complete")
    try:
        yield
    finally:
        for task in (bot_task, autocall_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.api_v1_prefix)


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "name": settings.app_name,
        "docs": "/docs",
        "health": f"{settings.api_v1_prefix}/health",
    }
