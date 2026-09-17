"""Настройки раздела «Финансы» — отдельный BaseSettings, как у соседних модулей.

Переменные окружения (префикс FINANCE_):
    FINANCE_ENABLED           — "false" выключает раздел целиком (по умолчанию true)
    FINANCE_BASE_CURRENCY     — валюта компании, в которой сходятся отчёты (KZT)
    FINANCE_IMPORT_MAX_ROWS   — потолок строк в одном импорте (20000)
    FINANCE_IMPORT_MAX_MB     — потолок размера файла (15)
    FINANCE_GRID_MAX_ROWS     — сколько операций отдаётся в табличный вид (10000)

Про валюту компании
───────────────────
Она одна и живёт в настройках, а не в коде: `amount_base` считается в ней, и
смена валюты задним числом означает пересчёт каждой строки. Поэтому значение
берётся из окружения один раз и попадает в `workspaces.base_currency` при
создании пространства — дальше правда о валюте лежит в базе, а не здесь.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/finance/config.py -> корень репозитория
_REPO_ROOT = Path(__file__).resolve().parents[3]


class FinanceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(_REPO_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_prefix="FINANCE_",
    )

    enabled: bool = True
    base_currency: str = "KZT"
    import_max_rows: int = Field(default=20_000, ge=1)
    import_max_mb: int = Field(default=15, ge=1)
    grid_max_rows: int = Field(default=10_000, ge=1)

    @property
    def import_max_bytes(self) -> int:
        return self.import_max_mb * 1024 * 1024


@lru_cache
def get_finance_settings() -> FinanceSettings:
    return FinanceSettings()


finance_settings = get_finance_settings()

__all__ = ["FinanceSettings", "finance_settings", "get_finance_settings"]
