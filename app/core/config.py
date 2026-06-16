from functools import lru_cache
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _strip_wrapping_quotes(value: str) -> str:
    trimmed = value.strip()
    if (
        (trimmed.startswith('"') and trimmed.endswith('"'))
        or (trimmed.startswith("'") and trimmed.endswith("'"))
    ):
        return trimmed[1:-1].strip()
    return trimmed


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "PDF Converter API"
    environment: str = "development"
    api_v1_prefix: str = "/api/v1"
    log_level: str = "INFO"
    app_host: str = Field(default="0.0.0.0", validation_alias=AliasChoices("APP_HOST"))
    app_port: int = Field(default=8080, validation_alias=AliasChoices("PORT", "APP_PORT"))
    allowed_origins: Annotated[str, NoDecode] = Field(
        default="http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001",
        validation_alias=AliasChoices("ALLOWED_ORIGINS"),
    )
    database_url: str = Field(
        default="postgresql+psycopg://postgres:postgres@localhost:5433/pdf_converter",
        validation_alias=AliasChoices("DATABASE_URL"),
    )
    azure_document_intelligence_endpoint: str | None = None
    azure_document_intelligence_key: str | None = None

    # Smart NLP correction engine
    smart_nlp_enabled: bool = True
    smart_nlp_model_path: str = "app/data/nlp/rubert_tiny2.onnx"
    smart_nlp_confidence_threshold: float = 0.75
    smart_nlp_clarify_threshold: float = 0.45
    smart_nlp_cache_size: int = 256

    # Scanned document OCR
    scan_max_pages: int = 50
    scan_min_quality_score: float = 0.25

    # Currency rates — live from National Bank of Kazakhstan (auto-refreshed daily).
    fx_rates_enabled: bool = True
    fx_rates_url: str = Field(
        default="https://nationalbank.kz/rss/rates_all.xml",
        validation_alias=AliasChoices("FX_RATES_URL"),
    )
    fx_rates_timeout_seconds: float = 6.0

    @property
    def fx_fallback_rates(self) -> dict[str, float]:
        """Used only when NB RK is unreachable and nothing is cached."""
        return {"USD": 480.0, "EUR": 520.0, "RUB": 6.0}

    # Telegram bot (optional). When token is set, the bot starts in the app lifespan.
    telegram_bot_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TELEGRAM_BOT_TOKEN"),
    )
    # Optional comma-separated Telegram user IDs allowed to use the bot.
    # Empty (default) = open to everyone.
    telegram_allowed_users: str = Field(
        default="",
        validation_alias=AliasChoices("TELEGRAM_ALLOWED_USERS"),
    )

    @field_validator(
        "app_name",
        "environment",
        "api_v1_prefix",
        "log_level",
        "app_host",
        "allowed_origins",
        "database_url",
        "azure_document_intelligence_endpoint",
        "azure_document_intelligence_key",
        "smart_nlp_model_path",
        "telegram_bot_token",
        "telegram_allowed_users",
        mode="before",
    )
    @classmethod
    def normalize_env_string(cls, v: str | None) -> str | None:
        if isinstance(v, str):
            return _strip_wrapping_quotes(v)
        return v

    @property
    def telegram_allowed_user_ids(self) -> set[int]:
        ids: set[int] = set()
        for item in self.telegram_allowed_users.split(","):
            item = item.strip()
            if item.isdigit():
                ids.add(int(item))
        return ids

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, v: str) -> str:
        if isinstance(v, str):
            v = _strip_wrapping_quotes(v)
            if v.startswith("postgres://"):
                return v.replace("postgres://", "postgresql+psycopg://", 1)
            if v.startswith("postgresql://"):
                return v.replace("postgresql://", "postgresql+psycopg://", 1)
        return v

    @property
    def cors_origins(self) -> list[str]:
        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
