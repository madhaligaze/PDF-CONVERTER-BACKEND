"""BBC Dashboard settings — deliberately separate from app.core.config.Settings.

Keeping our own BaseSettings means removing the feature never touches the core
config file: delete the package and the env vars simply stop being read.

Env vars (all optional, prefix BBC_):
    BBC_DASHBOARD_ENABLED   — "true" to expose the API (default: true)
    BBC_SPREADSHEET_ID      — id of the Google Sheet (the part between /d/ and /edit)
    BBC_SERVICE_ACCOUNT_JSON— path to the service-account JSON *or* the JSON itself
                              (default: bbc-sheets.json at the repo root)
    BBC_WORKSHEET_NAME      — worksheet to read; empty = first sheet
    BBC_CACHE_TTL_SECONDS   — how long a fetched snapshot is reused (default: 60)
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/bbc/config.py -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CREDENTIALS_PATH = str(_REPO_ROOT / "bbc-sheets.json")

# Sources are strictly read-only: the master sheet is produced by one array
# formula, so any write into it collapses the whole sheet. `drive.file` grants
# access ONLY to files this app itself creates — what the МСФО export needs —
# and cannot reach the source spreadsheets.
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.file",
]


class BbcSettings(BaseSettings):
    model_config = SettingsConfigDict(
        # Both locations, so the module works whether the process starts in the
        # repo root (docker compose) or in backend/ (local uvicorn). The later
        # entry wins, keeping backend/.env as the local override.
        env_file=(str(_REPO_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("BBC_DASHBOARD_ENABLED"),
    )
    spreadsheet_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("BBC_SPREADSHEET_ID"),
    )
    service_account_json: str = Field(
        default=DEFAULT_CREDENTIALS_PATH,
        validation_alias=AliasChoices("BBC_SERVICE_ACCOUNT_JSON"),
    )
    worksheet_name: str = Field(
        default="",
        validation_alias=AliasChoices("BBC_WORKSHEET_NAME"),
    )
    cache_ttl_seconds: float = Field(
        default=60.0,
        validation_alias=AliasChoices("BBC_CACHE_TTL_SECONDS"),
    )

    # ── Extra data sources ───────────────────────────────────────────────────────
    journal_spreadsheet_id: str = Field(
        default="1-BkyBUSHtWBzl2gqGKTeWYe7trYvTs1-NYNaNecB_X8",
        validation_alias=AliasChoices("BBC_JOURNAL_SPREADSHEET_ID"),
    )
    journal_worksheet_name: str = Field(
        default="Журнал",
        validation_alias=AliasChoices("BBC_JOURNAL_WORKSHEET_NAME"),
    )
    sales_spreadsheet_id: str = Field(
        default="1h5-zZkwuZ3hiKHl0CW22GAJGBdxBl8BybwyJkYU7egc",
        validation_alias=AliasChoices("BBC_SALES_SPREADSHEET_ID"),
    )
    sales_worksheet_name: str = Field(
        default="Общий Реестр Продаж",
        validation_alias=AliasChoices("BBC_SALES_WORKSHEET_NAME"),
    )
    # «расчет плана ОМиП» — план/факт, ФОТ фикс+бонус, рекламный бюджет (Блок 6).
    omip_spreadsheet_id: str = Field(
        default="1nbkblcdtBjHvqj3KBA5774kNxd5Npk9gNp7QqN9Hy7I",
        validation_alias=AliasChoices("BBC_OMIP_SPREADSHEET_ID"),
    )

    # ── Live refresh ─────────────────────────────────────────────────────────────
    # The backend polls Sheets so the request count stays constant no matter how
    # many browser tabs are open. 0 disables the loop entirely.
    poll_interval_seconds: float = Field(
        default=15.0,
        validation_alias=AliasChoices("BBC_POLL_INTERVAL_SECONDS"),
    )

    # ── Access ───────────────────────────────────────────────────────────────────
    # Base for generated referral links; empty = derive from the incoming request.
    public_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("BBC_PUBLIC_BASE_URL"),
    )
    # Таблица-приёмник для выгрузки МСФО. Сервис-аккаунт не может создать свою
    # (нет квоты в Drive), поэтому её создаёт человек и делится доступом.
    msfo_spreadsheet_id: str = Field(
        default="",
        validation_alias=AliasChoices("BBC_MSFO_SPREADSHEET_ID"),
    )
    session_ttl_hours: float = Field(
        default=12.0,
        validation_alias=AliasChoices("BBC_SESSION_TTL_HOURS"),
    )
    # Read once, only while bbc.users is empty; ignored afterwards.
    bootstrap_admin: str = Field(
        default="",
        validation_alias=AliasChoices("BBC_BOOTSTRAP_ADMIN"),
    )
    bootstrap_password: str = Field(
        default="",
        validation_alias=AliasChoices("BBC_BOOTSTRAP_PASSWORD"),
    )

    @property
    def credentials_path(self) -> Path | None:
        """Resolved path to the service-account file, or None when inlined/absent.

        A relative value (`bbc-sheets.json`, as documented in .env.example) is
        resolved against the repo root, so it works no matter whether the process
        starts there or in backend/.
        """
        raw = (self.service_account_json or "").strip()
        if not raw or raw.startswith("{"):
            return None
        path = Path(raw)
        return path if path.is_absolute() else _REPO_ROOT / path

    @property
    def credentials_available(self) -> bool:
        raw = (self.service_account_json or "").strip()
        if not raw:
            return False
        if raw.startswith("{"):  # inline JSON, e.g. on Railway
            return True
        path = self.credentials_path
        return path is not None and path.is_file()

    @property
    def configured(self) -> bool:
        """True when the module can actually talk to the sheet."""
        return bool(self.enabled and self.spreadsheet_id and self.credentials_available)


@lru_cache
def get_bbc_settings() -> BbcSettings:
    return BbcSettings()


bbc_settings = get_bbc_settings()
