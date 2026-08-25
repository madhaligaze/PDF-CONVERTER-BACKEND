"""Настройки Web-Excel — отдельный BaseSettings, как у bbc и mcp.

Переменные окружения (префикс WEBEXCEL_):
    WEBEXCEL_ENABLED            — "false" выключает раздел целиком (по умолчанию true)
    WEBEXCEL_SERVICE_ACCOUNT_JSON — путь к JSON сервис-аккаунта *или* сам JSON;
                                  пусто ⇒ наследуется BBC_SERVICE_ACCOUNT_JSON
    WEBEXCEL_ALLOWED_IDS        — белый список id таблиц через запятую;
                                  пусто = всё, что расшарено сервисному аккаунту
    WEBEXCEL_MAX_ROWS           — потолок строк на вкладку (по умолчанию 2000)
    WEBEXCEL_MAX_COLS           — потолок колонок на вкладку (по умолчанию 60)
    WEBEXCEL_CACHE_TTL_SECONDS  — сколько живёт импортированный снимок (600)
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/webexcel/config.py -> корень репозитория
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Скоупы те же, что у MCP-коннектора, и по той же причине.
#
# `drive.readonly` нужен, чтобы ПЕРЕЧИСЛИТЬ расшаренные таблицы: финансист
# выбирает книгу из списка по названию, а не вбивает id. `drive.file` из
# app/bbc/config.py на это неспособен — он видит только файлы, созданные самим
# приложением, а исходные книги созданы людьми.
#
# Запись невозможна на уровне токена: `spreadsheets.readonly`. Это не
# ограничение, а требование — мастер-книга собирается одной формулой-массивом,
# и любая запись внутрь её вывода схлопывает лист целиком. Правки финансиста
# живут в нашей БД, а не уезжают обратно в Google.
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


class WebExcelSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(_REPO_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("WEBEXCEL_ENABLED"),
    )
    # Пусто ⇒ креды дашборда. Второй копии приватного ключа в переменных
    # окружения быть не должно.
    service_account_json: str = Field(
        default="",
        validation_alias=AliasChoices("WEBEXCEL_SERVICE_ACCOUNT_JSON"),
    )
    allowed_ids_raw: str = Field(
        default="",
        validation_alias=AliasChoices("WEBEXCEL_ALLOWED_IDS"),
    )
    # Потолки существуют не ради экономии, а потому что Google отдаёт грид с
    # оформлением примерно по килобайту на ячейку. Вкладка «Осн.Реестр» — 11409
    # строк на 37 колонок: без потолка это 400 МБ JSON на один запрос.
    max_rows: int = Field(
        default=2000,
        validation_alias=AliasChoices("WEBEXCEL_MAX_ROWS"),
    )
    max_cols: int = Field(
        default=60,
        validation_alias=AliasChoices("WEBEXCEL_MAX_COLS"),
    )
    cache_ttl_seconds: float = Field(
        default=600.0,
        validation_alias=AliasChoices("WEBEXCEL_CACHE_TTL_SECONDS"),
    )

    @property
    def credentials_source(self) -> str:
        """Свои креды, иначе кредыBBC. Пусто ⇒ модуль не настроен."""
        own = (self.service_account_json or "").strip()
        if own:
            return own
        from app.bbc.config import bbc_settings

        return (bbc_settings.service_account_json or "").strip()

    @property
    def credentials_path(self) -> Path | None:
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
        if raw.startswith("{"):
            return True
        path = self.credentials_path
        return path is not None and path.is_file()

    @property
    def allowed_ids(self) -> set[str]:
        return {p.strip() for p in self.allowed_ids_raw.split(",") if p.strip()}

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.credentials_available)


@lru_cache
def get_webexcel_settings() -> WebExcelSettings:
    return WebExcelSettings()


webexcel_settings = get_webexcel_settings()
