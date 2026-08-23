"""Настройки MCP-коннектора — отдельно от app.core.config и от app.bbc.config.

Свой BaseSettings означает, что удаление модуля не трогает ни один чужой файл:
папку убрали — переменные просто перестали читаться.

Переменные окружения (префикс MCP_):
    MCP_ENABLED              — "true", чтобы поднять эндпоинт (по умолчанию false)
    MCP_SECRET               — секрет в пути URL; пусто ⇒ эндпоинта нет
    MCP_SERVICE_ACCOUNT_JSON — путь к JSON сервис-аккаунта *или* сам JSON;
                               пусто ⇒ наследуется BBC_SERVICE_ACCOUNT_JSON
    MCP_ALLOWED_IDS          — белый список id таблиц через запятую; пусто = всё,
                               что расшарено сервисному аккаунту
    MCP_MAX_CELLS            — потолок ячеек на один вызов (по умолчанию 20000)
    MCP_CACHE_TTL_SECONDS    — сколько живёт прочитанная сетка (по умолчанию 60)
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/mcp/config.py -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Скоупы шире, чем у дашборда, и это осознанно.
#
# В app/bbc/config.py стоит `drive.file` — он даёт доступ только к файлам,
# созданным самим приложением, и физически не дотягивается до исходных таблиц.
# Тот список трогать нельзя: расширение незаметно расширило бы права дашборда.
#
# Коннектору нужен `drive.readonly`, чтобы перечислить расшаренные таблицы по
# названию — директор спрашивает «какие отчёты», не зная id. Запись невозможна
# на уровне самого токена: `spreadsheets.readonly` — это второй слой защиты под
# тем фактом, что пишущих инструментов в модуле нет ни одного.
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

SERVER_NAME = "bbc-sheets"
SERVER_VERSION = "1.0.0"


class McpSettings(BaseSettings):
    model_config = SettingsConfigDict(
        # Оба расположения, чтобы модуль работал и когда процесс стартует в
        # корне репозитория (docker compose), и когда в backend/ (local uvicorn).
        env_file=(str(_REPO_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # По умолчанию выключено намеренно: забытая переменная не должна означать
    # открытый в интернет эндпоинт со всей финансовой отчётностью.
    enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("MCP_ENABLED"),
    )
    secret: str = Field(
        default="",
        validation_alias=AliasChoices("MCP_SECRET"),
    )
    # Пусто ⇒ берём креды дашборда: на Railway они уже загружены, и второй копии
    # приватного ключа в переменных окружения быть не должно.
    service_account_json: str = Field(
        default="",
        validation_alias=AliasChoices("MCP_SERVICE_ACCOUNT_JSON"),
    )
    allowed_ids_raw: str = Field(
        default="",
        validation_alias=AliasChoices("MCP_ALLOWED_IDS"),
    )
    max_cells: int = Field(
        default=20000,
        validation_alias=AliasChoices("MCP_MAX_CELLS"),
    )
    cache_ttl_seconds: float = Field(
        default=60.0,
        validation_alias=AliasChoices("MCP_CACHE_TTL_SECONDS"),
    )

    @property
    def credentials_source(self) -> str:
        """Свои креды, иначе креды дашборда — сырое значение, как в env."""
        own = (self.service_account_json or "").strip()
        if own:
            return own
        from app.bbc.config import bbc_settings  # локальный импорт: модуль удаляем

        return (bbc_settings.service_account_json or "").strip()

    @property
    def credentials_path(self) -> Path | None:
        """Путь к файлу кредов, либо None когда JSON задан строкой."""
        raw = self.credentials_source
        if not raw or raw.startswith("{"):
            return None
        path = Path(raw)
        return path if path.is_absolute() else _REPO_ROOT / path

    @property
    def credentials_available(self) -> bool:
        raw = self.credentials_source
        if not raw:
            return False
        if raw.startswith("{"):  # inline JSON, путь Railway
            return True
        path = self.credentials_path
        return path is not None and path.is_file()

    @property
    def allowed_ids(self) -> frozenset[str]:
        """Белый список id таблиц; пустой = ограничения нет."""
        return frozenset(
            part.strip() for part in self.allowed_ids_raw.split(",") if part.strip()
        )

    @property
    def configured(self) -> bool:
        """True, когда эндпоинт можно поднимать.

        Секрет входит в условие: без него URL защищать нечем, а поднимать
        незащищённый маршрут с отчётностью нельзя даже по явному MCP_ENABLED.
        """
        return bool(self.enabled and self.secret and self.credentials_available)


@lru_cache
def get_mcp_settings() -> McpSettings:
    return McpSettings()


mcp_settings = get_mcp_settings()
