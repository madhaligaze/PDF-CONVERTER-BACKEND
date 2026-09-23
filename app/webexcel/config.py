"""Настройки «Таблиц» — отдельный BaseSettings, как у соседних модулей.

Переменные окружения (префикс WEBEXCEL_):
    WEBEXCEL_ENABLED       — "false" выключает раздел целиком (по умолчанию true)
    WEBEXCEL_MAX_TABLE_MB  — потолок одной таблицы на полке (по умолчанию 20)
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/webexcel/config.py -> корень репозитория
_REPO_ROOT = Path(__file__).resolve().parents[3]


class WebExcelSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(_REPO_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_prefix="WEBEXCEL_",
    )

    enabled: bool = True
    # Раздел открыт без входа, поэтому потолок — не экономия, а защита: полка
    # общая, и одна загрузка на сотни мегабайт не должна лечь в базу.
    max_table_mb: int = Field(default=20, ge=1)

    @property
    def max_table_bytes(self) -> int:
        return self.max_table_mb * 1024 * 1024


@lru_cache
def get_webexcel_settings() -> WebExcelSettings:
    return WebExcelSettings()


webexcel_settings = get_webexcel_settings()

__all__ = ["WebExcelSettings", "get_webexcel_settings", "webexcel_settings"]
