import asyncio
import contextlib
import logging
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.core.database import get_resolved_database_url, init_database
from app.mcp.config import mcp_settings  # MCP-коннектор (удаляемый модуль)

log = logging.getLogger(__name__)


def _init_bbc() -> None:
    """Create the `bbc` schema/tables. BBC Dashboard (removable module).

    Best-effort: the dashboard failing to initialise must never stop the rest of
    the API from booting.
    """
    try:
        from app.bbc.db import init_bbc_database

        init_bbc_database()
        log.info("BBC schema initialized")
    except Exception as exc:  # noqa: BLE001 — optional feature, never fatal
        log.warning("BBC schema init failed (%s: %s)", type(exc).__name__, exc)


#: Пути к alembic считаются от каталога `backend`, а не от рабочего каталога
#: процесса. Раньше конфиг открывался как `Config("alembic.ini")`, и миграции
#: молча не находились, если приложение запускали не из `backend/`.
_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _run_migrations() -> None:
    """Привести схему в соответствие с кодом. Не смогли — не поднимаемся.

    Здесь раньше стоял молчаливый откат на `create_all`: если alembic падал,
    таблицы всё равно создавались, `alembic_version` оставалась на старой
    ревизии, приложение отвечало 200 — и схема расходилась с миграциями
    навсегда. Следующая ревизия падала уже на «таблица существует» и снова
    уходила в откат. Наружу это не выходило ничем: ни ошибки, ни симптома.

    Пока Postgres был кэшем прочитанного из Google, цена была невелика — книгу
    всегда можно перечитать. Теперь в нём живут данные, которых больше нигде
    нет, и приложение, не сумевшее привести схему в порядок, обязано не
    подняться, а не делать вид, что всё хорошо.

    Для SQLite alembic не гоняется намеренно: ревизии написаны под Postgres со
    схемами, которых у SQLite нет. Там схема собирается через `create_all` — и
    это законный путь, а не запасной.
    """
    db_url = get_resolved_database_url()
    driver = db_url.split("://")[0] if "://" in db_url else db_url

    if db_url.startswith("sqlite"):
        log.info("Схема: SQLite (%s) — create_all вместо ревизий", driver)
        init_database()
        _init_bbc()
        return

    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(_BACKEND_DIR / "app" / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    # Не трогать логирование процесса: секции [logger_*] из alembic.ini
    # предназначены для запуска из командной строки, а здесь они затирали
    # настройки uvicorn — после миграций пропадал журнал запросов.
    alembic_cfg.attributes["configure_logger"] = False

    try:
        command.upgrade(alembic_cfg, "head")
    except Exception as exc:
        log.error(
            "Миграции не применились (%s: %s). Приложение не поднимется: схема "
            "не соответствует коду, а работать на расходящейся схеме опаснее, "
            "чем не работать вовсе.",
            type(exc).__name__,
            exc,
        )
        log.error(traceback.format_exc())
        raise

    log.info("Схема приведена к последней ревизии")


async def _autocall_sync_loop() -> None:
    """Background daily sync of AutoCall.kz campaigns into Google Sheets.

    A single failure (network, Google, etc.) is logged and never crashes the app —
    we just wait for the next interval and retry.
    """
    from app.services import autocall_payments_service, autocall_service

    interval = max(0.25, settings.autocall_auto_sync_interval_hours) * 3600
    while True:
        try:
            result = await autocall_service.sync_autocalls()
            log.info("autocall auto-sync: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — best-effort daily job
            log.warning("autocall auto-sync failed: %s", exc)
        # Balance top-ups (blocking httpx/gspread) — run off the event loop.
        if settings.autocall_session_configured:
            try:
                result = await asyncio.to_thread(autocall_payments_service.sync_ledger)
                log.info("autocall ledger auto-sync: %s", result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("autocall topups auto-sync failed: %s", exc)
        await asyncio.sleep(interval)


async def _bbc_refresh_loop() -> None:
    """Background live refresh of the BBC dashboard. Removable module.

    The backend — not the browsers — polls Google, so the request count stays
    constant no matter how many tabs are open. Each pass first checks Drive's
    `modifiedTime` and only re-reads the sheet when it actually moved.
    """
    from app.bbc import live
    from app.bbc.config import bbc_settings

    interval = max(5.0, bbc_settings.poll_interval_seconds)
    # Потолок отступа: пять минут. Дальше растягивать нечего — квота Google
    # считается поминутно и к этому моменту давно восстановилась.
    max_backoff = 300.0
    delay = interval

    # Warm the snapshot once so the first request is served from memory.
    try:
        await asyncio.to_thread(live.refresh, force=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("BBC: initial refresh failed: %s", exc)

    while True:
        await asyncio.sleep(delay)
        try:
            # gspread/httpx are blocking — keep them off the event loop.
            await asyncio.to_thread(live.refresh)
            delay = interval
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — best-effort poll, never fatal
            # Отступ при отказе, и в первую очередь при 429.
            #
            # Раньше цикл продолжал ходить каждые 15 секунд, что бы Google ни
            # отвечал. На исчерпанной квоте это значило, что цикл сам же её и
            # держал исчерпанной: минута не успевала «остыть», потому что в неё
            # снова прилетали четыре запроса. Выход из состояния зависел от
            # того, перестанут ли люди пользоваться дашбордом.
            delay = min(max_backoff, max(delay, interval) * 2)
            log.warning(
                "BBC: live refresh failed (%s), следующая попытка через %.0f с", exc, delay
            )


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Схема — первым делом, до всего остального.
    #
    # В контейнере миграции гоняет ENTRYPOINT (backend/Dockerfile), и там этот
    # вызов — повторный и почти бесплатный: `upgrade head` на актуальной базе
    # стоит один запрос. А вот локально их не гонял никто: разработчик
    # запускает `uv run python main.py`, минуя ENTRYPOINT. Из-за этого рабочая
    # база отставала от кода на две ревизии, и таблиц журнала касаний в ней
    # просто не было. Один вызов здесь избавляет от целого класса «у меня не
    # воспроизводится».
    _run_migrations()

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

    # Start the BBC Dashboard live refresh (removable module).
    bbc_task: asyncio.Task | None = None
    try:
        from app.bbc.config import bbc_settings

        if bbc_settings.configured and bbc_settings.poll_interval_seconds > 0:
            bbc_task = asyncio.create_task(_bbc_refresh_loop())
    except Exception as exc:  # noqa: BLE001
        log.warning("BBC live refresh failed to start: %s", exc)

    log.info("Application startup complete")
    try:
        yield
    finally:
        for task in (bot_task, autocall_task, bbc_task):
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

# MCP-коннектор для Claude (удаляемый модуль). Подключается на уровне
# приложения, а не в api_router: адрес /mcp/<секрет> не версионируется вместе
# с API — его вставляют руками в настройки коннектора на claude.ai.
if mcp_settings.configured:
    from app.mcp.routes import router as mcp_router

    app.include_router(mcp_router)
    log.info("MCP connector enabled at /mcp/<secret>")


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "name": settings.app_name,
        "docs": "/docs",
        "health": f"{settings.api_v1_prefix}/health",
    }
