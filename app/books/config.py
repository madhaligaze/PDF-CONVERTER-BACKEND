"""Настройки модуля «Книги» — отдельный BaseSettings, как у bbc и webexcel.

Переменные окружения (префикс BOOKS_):
    BOOKS_ENABLED                — "false" выключает раздел целиком (по умолчанию true)
    BOOKS_SERVICE_ACCOUNT_JSON   — путь к JSON сервис-аккаунта *или* сам JSON;
                                   пусто ⇒ берётся GOOGLE_… , затем BBC_…
    BOOKS_MAX_ROWS               — потолок строк на вкладку при импорте (5000)
    BOOKS_MAX_COLS               — потолок колонок на вкладку (100)
    BOOKS_CHANGE_LIMIT_RATIO     — доля изменённых строк, после которой импорт
                                   встаёт и спрашивает (0.3)
    BOOKS_CHANGE_LIMIT_ROWS      — то же в абсолютных строках (200)
    BOOKS_SAMPLE_ROWS            — сколько строк смотрит определение типа поля (300)

Про креды и про то, почему модуль не импортирует `bbc`
──────────────────────────────────────────────────────
Ключ у проекта один, и второй копии приватного ключа в переменных окружения
быть не должно. Но брать его вызовом `from app.bbc.config import bbc_settings`
нельзя: «Книги» обязаны собираться без BBC вообще — иначе через месяц окажется,
что модуль, задуманный как общий, знает про конкретную компанию.

Поэтому наследование сделано на уровне ИМЁН переменных, а не импорта модулей:
pydantic пробует `BOOKS_…`, потом `GOOGLE_…`, потом `BBC_…` и берёт первое
заданное. Кода, знающего про BBC, при этом не появляется — появляется строка в
списке псевдонимов.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/books/config.py -> корень репозитория
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Только чтение, и это не осторожность, а свойство продукта.
#
# Приложение не пишет в Google ни при каких обстоятельствах: работа переезжает
# во внутренние книги, а Google остаётся каналом импорта. Свойство «мы не можем
# испортить чужую книгу» должно быть доказуемым, а не обещанным — здесь оно
# обеспечено уровнем токена, а не дисциплиной вызывающего.
#
# `drive.readonly` нужен, чтобы ПЕРЕЧИСЛИТЬ расшаренные книги: человек выбирает
# книгу из списка по названию, а не вбивает id. `drive.file` на это неспособен —
# он видит только файлы, созданные самим приложением.
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


class BooksSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(_REPO_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("BOOKS_ENABLED"),
    )
    # Порядок важен и проверен на живом стенде.
    #
    # `GOOGLE_SERVICE_ACCOUNT_JSON` в этот список НЕ входит, хотя имя у неё
    # самое родовое из всех. В этом развёртывании она держит ключ автообзвона —
    # другого проекта Google, у которого доступа к книгам нет вовсе. Взяв её,
    # модуль молча авторизовался бы не тем аккаунтом и получил бы 403 на каждой
    # книге; искать причину пришлось бы в правах на книги, а не в настройках.
    service_account_json: str = Field(
        default="",
        validation_alias=AliasChoices(
            "BOOKS_SERVICE_ACCOUNT_JSON",
            "BBC_SERVICE_ACCOUNT_JSON",
        ),
    )

    # Потолки импорта.
    #
    # Выше, чем у раздела «Таблицы» (там 2000), и это не оплошность: тот тянет
    # грид с оформлением — Google отдаёт около килобайта на ячейку, и одна
    # вкладка «Журнала» весит 46 МБ. Здесь читаются только значения, строка
    # обходится в сотни байт, и потолок упирается не в память, а в здравый
    # смысл.
    #
    # 20000 выбрано по живым книгам: в «Журнале» пилота 5917 строк, в
    # «Тех.Журнале» — 11409. Прежние 5000 обрезали книгу на середине, и
    # спасал только флаг `truncated`.
    max_rows: int = Field(default=20000, validation_alias=AliasChoices("BOOKS_MAX_ROWS"))
    max_cols: int = Field(default=100, validation_alias=AliasChoices("BOOKS_MAX_COLS"))

    # Тормоз на массовое изменение: если очередной импорт хочет тронуть больше
    # этого, он встаёт и показывает предпросмотр вместо того, чтобы применять.
    # Ловит «пересортировали лист», «вставили поверх новую версию», «удалили
    # половину» — то есть ровно те случаи, когда таблицу использовали как
    # песочницу, а приложение приняло бы это за осмысленную правку.
    change_limit_ratio: float = Field(
        default=0.3,
        validation_alias=AliasChoices("BOOKS_CHANGE_LIMIT_RATIO"),
    )
    change_limit_rows: int = Field(
        default=200,
        validation_alias=AliasChoices("BOOKS_CHANGE_LIMIT_ROWS"),
    )

    # Сколько строк смотрит определение типа колонки. Больше — точнее и
    # медленнее; на 300 строках доля разбираемых значений уже устойчива.
    sample_rows: int = Field(
        default=300,
        validation_alias=AliasChoices("BOOKS_SAMPLE_ROWS"),
    )

    @property
    def credentials_path(self) -> Path | None:
        """Путь к файлу ключа. None, если ключ задан как JSON прямо в переменной."""
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
        if raw.startswith("{"):
            return True
        path = self.credentials_path
        return path is not None and path.is_file()

    @property
    def configured(self) -> bool:
        return self.enabled and self.credentials_available


@lru_cache(maxsize=1)
def _load() -> BooksSettings:
    return BooksSettings()


books_settings = _load()

__all__ = ["GOOGLE_SCOPES", "BooksSettings", "books_settings"]
