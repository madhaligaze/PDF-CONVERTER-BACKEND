"""Thin Google wrapper for the BBC Dashboard.

Self-contained on purpose: the module never imports the Autocall Google helpers,
so deleting `app/bbc/` cannot break that integration (and vice versa).

Credentials are read-only on the sources (`spreadsheets.readonly`); `drive.file`
covers only files this app creates itself.

**The read-only scope is a safety feature, not a limitation.** The master sheet is
generated end to end by one `=LET(… IMPORTRANGE(…))` array formula in A1: writing
a literal anywhere inside its output range stops the array from expanding and
collapses the whole sheet until that cell is cleared. Nothing here may ever write
to a source.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from functools import lru_cache
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from app.bbc.config import GOOGLE_SCOPES, bbc_settings

log = logging.getLogger(__name__)

# Logical source names, resolved to (spreadsheet id, worksheet) via settings.
SOURCE_MASTER = "master"
SOURCE_JOURNAL = "journal"
SOURCE_SALES = "sales"
SOURCE_OMIP = "omip"


class BbcError(Exception):
    """Configuration / network / Google failure carrying a user-facing message."""


def humanize(exc: Exception) -> str:
    """Отказ Google → фраза, которую можно показать человеку.

    Сырой текст API («APIError: [429]: Quota exceeded for quota metric 'Read
    requests' and limit 'Read requests per minute per user' of service
    sheets.googleapis.com for consumer 'project_number:992129259158'») попадал
    в баннер как есть. Человеку за экраном он не говорит ни что случилось, ни
    что делать, ни пройдёт ли само.
    """
    text = str(exc)
    if "429" in text or "Quota exceeded" in text or "RATE_LIMIT" in text:
        return (
            "Google временно ограничил чтение таблицы — слишком много обращений подряд. "
            "Это проходит само за минуту"
        )
    if "403" in text and "PERMISSION" in text.upper():
        return "У сервисного аккаунта нет доступа к таблице — проверьте, что она ему открыта"
    if "404" in text:
        return "Таблица не найдена — проверьте её id в настройках"
    return text


def _load_credentials() -> Credentials:
    raw = (bbc_settings.service_account_json or "").strip()
    if not raw:
        raise BbcError("BBC: не заданы креды Google (BBC_SERVICE_ACCOUNT_JSON)")
    try:
        if raw.startswith("{"):
            return Credentials.from_service_account_info(json.loads(raw), scopes=GOOGLE_SCOPES)
        path = bbc_settings.credentials_path
        if path is None or not path.is_file():
            raise BbcError(f"BBC: файл кредов не найден: {path or raw}")
        return Credentials.from_service_account_file(str(path), scopes=GOOGLE_SCOPES)
    except (ValueError, OSError) as exc:
        raise BbcError(f"BBC: некорректные креды Google Service Account: {exc}") from exc


@lru_cache(maxsize=1)
def _client() -> gspread.Client:
    return gspread.authorize(_load_credentials())


def source_ref(source: str) -> tuple[str, str]:
    """(spreadsheet id, worksheet name) for a logical source."""
    mapping = {
        SOURCE_MASTER: (bbc_settings.spreadsheet_id or "", bbc_settings.worksheet_name),
        SOURCE_JOURNAL: (
            bbc_settings.journal_spreadsheet_id,
            bbc_settings.journal_worksheet_name,
        ),
        SOURCE_SALES: (bbc_settings.sales_spreadsheet_id, bbc_settings.sales_worksheet_name),
        SOURCE_OMIP: (bbc_settings.omip_spreadsheet_id, ""),
    }
    if source not in mapping:
        raise BbcError(f"BBC: неизвестный источник {source!r}")
    spreadsheet_id, worksheet = mapping[source]
    if not spreadsheet_id:
        raise BbcError(f"BBC: не задан id таблицы для источника «{source}»")
    return spreadsheet_id, worksheet


def open_spreadsheet(spreadsheet_id: str | None = None) -> gspread.Spreadsheet:
    """Open a spreadsheet by id; None = the configured master sheet."""
    key = (spreadsheet_id or bbc_settings.spreadsheet_id or "").strip()
    if not key:
        raise BbcError("BBC: не задан BBC_SPREADSHEET_ID")
    try:
        return _client().open_by_key(key)
    except Exception as exc:  # noqa: BLE001 — gspread raises many types; wrap for the caller
        raise BbcError(f"BBC: не удалось открыть таблицу: {exc}") from exc


def open_worksheet(
    name: str | None = None, spreadsheet_id: str | None = None
) -> gspread.Worksheet:
    """Open a worksheet by name; empty/None = the configured one, else the first."""
    spreadsheet = open_spreadsheet(spreadsheet_id)
    target = (name if name is not None else bbc_settings.worksheet_name or "").strip()
    try:
        return spreadsheet.worksheet(target) if target else spreadsheet.get_worksheet(0)
    except Exception as exc:  # noqa: BLE001
        raise BbcError(f"BBC: лист «{target or 'первый'}» не найден: {exc}") from exc


def list_worksheets(spreadsheet_id: str | None = None) -> list[dict[str, Any]]:
    """Metadata for every tab — used by the dashboard's sheet picker."""
    return [
        {"title": ws.title, "index": ws.index, "rows": ws.row_count, "cols": ws.col_count}
        for ws in open_spreadsheet(spreadsheet_id).worksheets()
    ]


def read_values(name: str | None = None, spreadsheet_id: str | None = None) -> list[list[str]]:
    """Raw grid (list of rows) of the worksheet, exactly as displayed."""
    return open_worksheet(name, spreadsheet_id).get_all_values()


def read_source(source: str) -> list[list[str]]:
    """Raw grid of a logical source (master / journal / sales / omip)."""
    spreadsheet_id, worksheet = source_ref(source)
    return read_values(worksheet or None, spreadsheet_id)


# ── Кэш второстепенных источников ────────────────────────────────────────────────
#
# Мастер-таблицу читает один фоновый цикл (live.py), и запросов там ровно четыре
# в минуту независимо от числа открытых вкладок. А «Журнал операций» и «Отдел
# продаж» ходили в Google НА КАЖДЫЙ запрос, мимо всей этой конструкции: одно
# открытие «Продаж» — два обращения к API. Три человека, щёлкающих по вкладкам,
# выбирали квоту в 60 чтений в минуту за полминуты, и дашборд отвечал 429 всем,
# включая дебиторку.
#
# Настройка `BBC_CACHE_TTL_SECONDS` для этого и заведена — но нигде не читалась.
# Теперь читается здесь.
#
# Кэшируются только второстепенные источники. Мастер сюда не попадает намеренно:
# цикл живого обновления обязан делать настоящее чтение, иначе он перестанет
# замечать правки в таблице — ради чего и существует.

_cache: dict[tuple[str, str], tuple[float, list[list[str]]]] = {}
_cache_lock = threading.Lock()


def read_cached(name: str | None, spreadsheet_id: str | None) -> list[list[str]]:
    """Чтение с коротким кэшем в памяти. Для всего, кроме мастер-таблицы."""
    ttl = max(0.0, bbc_settings.cache_ttl_seconds)
    key = (spreadsheet_id or "", name or "")
    now = time.monotonic()

    if ttl > 0:
        with _cache_lock:
            hit = _cache.get(key)
            if hit is not None and now - hit[0] < ttl:
                return hit[1]

    values = read_values(name, spreadsheet_id)

    if ttl > 0:
        with _cache_lock:
            _cache[key] = (now, values)
    return values


def read_source_cached(source: str) -> list[list[str]]:
    spreadsheet_id, worksheet = source_ref(source)
    return read_cached(worksheet or None, spreadsheet_id)


_tabs_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def list_worksheets_cached(spreadsheet_id: str | None = None) -> list[dict[str, Any]]:
    """Список листов с тем же кэшем.

    «Отдел продаж» спрашивал его на каждый запрос, только чтобы выбрать самый
    свежий лист «Отчет …». Вместе с самим отчётом и реестром выходило три
    обращения к Google на одно открытие вкладки. Состав листов меняется раз в
    месяц — держать его свежим посекундно не за чем.
    """
    ttl = max(0.0, bbc_settings.cache_ttl_seconds)
    key = spreadsheet_id or ""
    now = time.monotonic()

    if ttl > 0:
        with _cache_lock:
            hit = _tabs_cache.get(key)
            if hit is not None and now - hit[0] < ttl:
                return hit[1]

    tabs = list_worksheets(spreadsheet_id)

    if ttl > 0:
        with _cache_lock:
            _tabs_cache[key] = (now, tabs)
    return tabs


def invalidate_read_cache() -> None:
    """Сбросить кэш — по кнопке «Обновить», когда человек просит свежее."""
    with _cache_lock:
        _cache.clear()
        _tabs_cache.clear()


def write_cells(
    updates: list[dict[str, Any]],
    name: str | None = None,
    spreadsheet_id: str | None = None,
) -> int:
    """Apply `[{"range": "B4", "value": "..."}, ...]` edits.

    Unused in v1: the sources are read-only and `POST /bbc/update` refuses before
    reaching here. Kept so the capability can be restored without reshaping this
    module.
    """
    if not updates:
        return 0
    worksheet = open_worksheet(name, spreadsheet_id)
    payload = [{"range": item["range"], "values": [[item["value"]]]} for item in updates]
    try:
        worksheet.batch_update(payload, value_input_option="USER_ENTERED")
    except Exception as exc:  # noqa: BLE001
        raise BbcError(f"BBC: не удалось записать в таблицу: {exc}") from exc
    return len(payload)


__all__ = [
    "SOURCE_JOURNAL",
    "SOURCE_MASTER",
    "SOURCE_OMIP",
    "SOURCE_SALES",
    "BbcError",
    "humanize",
    "list_worksheets",
    "open_spreadsheet",
    "open_worksheet",
    "invalidate_read_cache",
    "read_cached",
    "read_source",
    "read_source_cached",
    "read_values",
    "source_ref",
    "write_cells",
]
