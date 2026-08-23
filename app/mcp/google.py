"""Тонкая обёртка над Google для MCP-коннектора.

Самодостаточна намеренно: не импортирует ни хелперы Autocall, ни `app.bbc.sheets`
(кроме кредов через настройки), так что удаление `app/mcp/` не может ничего сломать.

Ключевое решение — **любое чтение идёт через полную сетку вкладки, положенную
в TTL-кэш**. Диапазон вида «Лист!B2:F40» не превращается в отдельный запрос к
Google: сетка режется в Python. Причина не в элегантности, а в квоте — Google
даёт 60 чтений в минуту на весь сервисный аккаунт, и этот же аккаунт обслуживает
дашборд. Клод на один вопрос директора делает пять-шесть вызовов подряд; без
кэша коннектор выел бы квоту у прода.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from functools import lru_cache
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from app.mcp.config import GOOGLE_SCOPES, mcp_settings

log = logging.getLogger(__name__)


class McpError(Exception):
    """Отказ конфигурации / сети / Google с текстом, который можно показать человеку."""


def humanize(exc: Exception) -> str:
    """Отказ Google → фраза, которую поймёт директор, а не трассировка APIError."""
    text = str(exc)
    if "429" in text or "Quota exceeded" in text or "RATE_LIMIT" in text:
        return (
            "Google временно ограничил чтение — слишком много обращений подряд. "
            "Это проходит само за минуту, попробуйте ещё раз"
        )
    if "403" in text and "PERMISSION" in text.upper():
        return (
            "У сервисного аккаунта нет доступа к этой таблице — её нужно открыть "
            "на bbc-sheets@bbc-sheets.iam.gserviceaccount.com"
        )
    if "404" in text:
        return "Таблица не найдена — проверьте название или ссылку"
    return text


# ── Креды и клиент ──────────────────────────────────────────────────────────


def _load_credentials() -> Credentials:
    raw = mcp_settings.credentials_source
    if not raw:
        raise McpError("MCP: не заданы креды Google (MCP_SERVICE_ACCOUNT_JSON или BBC_SERVICE_ACCOUNT_JSON)")
    try:
        if raw.startswith("{"):
            return Credentials.from_service_account_info(json.loads(raw), scopes=GOOGLE_SCOPES)
        path = mcp_settings.credentials_path
        if path is None or not path.is_file():
            raise McpError(f"MCP: файл кредов не найден: {path or raw}")
        return Credentials.from_service_account_file(str(path), scopes=GOOGLE_SCOPES)
    except (ValueError, OSError) as exc:
        raise McpError(f"MCP: некорректные креды Google Service Account: {exc}") from exc


@lru_cache(maxsize=1)
def _client() -> gspread.Client:
    return gspread.authorize(_load_credentials())


# ── Кэш ─────────────────────────────────────────────────────────────────────

_grid_cache: dict[tuple[str, str], tuple[float, list[list[str]]]] = {}
_meta_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_files_cache: tuple[float, list[dict[str, Any]]] | None = None
_lock = threading.Lock()


def _fresh(stamp: float) -> bool:
    return (time.monotonic() - stamp) < mcp_settings.cache_ttl_seconds


def invalidate_cache() -> None:
    """Сбросить всё — для тестов и ручной перепроверки."""
    global _files_cache
    with _lock:
        _grid_cache.clear()
        _meta_cache.clear()
        _files_cache = None


# ── Перечисление таблиц ─────────────────────────────────────────────────────


def list_spreadsheet_files() -> list[dict[str, Any]]:
    """Все таблицы, доступные сервисному аккаунту в Drive.

    `gspread.Client.list_spreadsheet_files()` сам ходит в Drive `files.list`,
    так что `google-api-python-client` не нужен — в проде это была бы новая
    зависимость сразу в двух файлах (requirements-prod.txt и pyproject.toml).
    """
    global _files_cache
    with _lock:
        if _files_cache is not None and _fresh(_files_cache[0]):
            return _files_cache[1]

    try:
        raw = _client().list_spreadsheet_files()
    except Exception as exc:  # noqa: BLE001 — gspread бросает много типов
        raise McpError(humanize(exc)) from exc

    allowed = mcp_settings.allowed_ids
    files = [
        {
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "modified": item.get("modifiedTime", ""),
        }
        for item in raw
        if item.get("id") and (not allowed or item.get("id") in allowed)
    ]
    files.sort(key=lambda x: x["name"].lower())

    with _lock:
        _files_cache = (time.monotonic(), files)
    return files


_URL_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{20,}$")


def resolve_spreadsheet_id(ref: str) -> str:
    """id таблицы из id, ссылки или названия.

    Директор пишет «в реестре продаж», а не «в 1h5-zZkw…». Поиск по названию
    идёт по списку из Drive: сначала точное совпадение, потом вхождение
    подстроки — и неоднозначность возвращается как ошибка со списком вариантов,
    а не молча берёт первый. Взять не ту таблицу здесь означает показать
    директору чужие цифры как свои.
    """
    ref = (ref or "").strip()
    if not ref:
        raise McpError("Не указана таблица")

    match = _URL_ID.search(ref)
    if match:
        candidate = match.group(1)
    elif _BARE_ID.match(ref):
        candidate = ref
    else:
        candidate = ""

    allowed = mcp_settings.allowed_ids
    if candidate:
        if allowed and candidate not in allowed:
            raise McpError("Эта таблица не входит в список разрешённых для коннектора")
        return candidate

    files = list_spreadsheet_files()
    lowered = ref.lower()
    exact = [f for f in files if f["name"].lower() == lowered]
    if len(exact) == 1:
        return exact[0]["id"]
    if len(exact) > 1:
        raise McpError(
            f"Таблиц с названием «{ref}» несколько — уточните по id: "
            + ", ".join(f["id"] for f in exact)
        )

    partial = [f for f in files if lowered in f["name"].lower()]
    if len(partial) == 1:
        return partial[0]["id"]
    if len(partial) > 1:
        raise McpError(
            f"Под «{ref}» подходит несколько таблиц: "
            + "; ".join(f"«{f['name']}»" for f in partial)
            + ". Уточните название"
        )

    known = ", ".join(f"«{f['name']}»" for f in files) or "ни одной"
    raise McpError(f"Таблица «{ref}» не найдена. Доступны: {known}")


# ── Метаданные и сетки ──────────────────────────────────────────────────────


def _open(spreadsheet_id: str) -> gspread.Spreadsheet:
    try:
        return _client().open_by_key(spreadsheet_id)
    except Exception as exc:  # noqa: BLE001
        raise McpError(humanize(exc)) from exc


def spreadsheet_meta(spreadsheet_id: str) -> dict[str, Any]:
    """Название таблицы и метаданные её вкладок."""
    with _lock:
        hit = _meta_cache.get(spreadsheet_id)
        if hit and _fresh(hit[0]):
            return hit[1]

    spreadsheet = _open(spreadsheet_id)
    try:
        tabs = spreadsheet.worksheets()
        meta = {
            "id": spreadsheet_id,
            "title": spreadsheet.title,
            "tabs": [
                {
                    "title": ws.title,
                    "index": ws.index,
                    "rows": ws.row_count,
                    "cols": ws.col_count,
                }
                for ws in tabs
            ],
        }
    except Exception as exc:  # noqa: BLE001
        raise McpError(humanize(exc)) from exc

    with _lock:
        _meta_cache[spreadsheet_id] = (time.monotonic(), meta)
    return meta


def resolve_tab(spreadsheet_id: str, tab: str | None) -> str:
    """Название вкладки; пусто/None = первая."""
    meta = spreadsheet_meta(spreadsheet_id)
    titles = [t["title"] for t in meta["tabs"]]
    if not titles:
        raise McpError(f"В таблице «{meta['title']}» нет ни одной вкладки")

    wanted = (tab or "").strip()
    if not wanted:
        return titles[0]
    if wanted in titles:
        return wanted

    lowered = wanted.lower()
    for title in titles:
        if title.lower() == lowered:
            return title
    partial = [t for t in titles if lowered in t.lower()]
    if len(partial) == 1:
        return partial[0]

    raise McpError(
        f"Вкладка «{wanted}» не найдена. В таблице «{meta['title']}» есть: "
        + ", ".join(f"«{t}»" for t in titles)
    )


def read_grid(spreadsheet_id: str, tab: str) -> list[list[str]]:
    """Полная сетка вкладки, как она видна на экране. Кэшируется по TTL."""
    key = (spreadsheet_id, tab)
    with _lock:
        hit = _grid_cache.get(key)
        if hit and _fresh(hit[0]):
            return hit[1]

    spreadsheet = _open(spreadsheet_id)
    try:
        worksheet = spreadsheet.worksheet(tab)
        grid = worksheet.get_all_values()
    except Exception as exc:  # noqa: BLE001
        raise McpError(humanize(exc)) from exc

    with _lock:
        _grid_cache[key] = (time.monotonic(), grid)
    return grid


# ── Разбор A1 ───────────────────────────────────────────────────────────────

_A1 = re.compile(r"^(?:([A-Z]+))?(?:([0-9]+))?$", re.IGNORECASE)


def _col_index(letters: str) -> int:
    """A→0, B→1, …, Z→25, AA→26."""
    value = 0
    for char in letters.upper():
        value = value * 26 + (ord(char) - 64)
    return value - 1


def split_range(a1: str) -> tuple[str | None, str]:
    """«Лист!A1:H50» → («Лист», «A1:H50»). Кавычки вокруг названия снимаются."""
    a1 = (a1 or "").strip()
    if "!" not in a1:
        # Без «!» это либо диапазон, либо название вкладки. Диапазон всегда
        # состоит из букв, цифр и двоеточия — всё прочее считаем вкладкой.
        if a1 and re.fullmatch(r"[A-Za-z]*[0-9]*(?::[A-Za-z]*[0-9]*)?", a1):
            return None, a1
        return (a1 or None), ""
    tab, _, rng = a1.rpartition("!")
    tab = tab.strip()
    if len(tab) >= 2 and tab[0] == "'" and tab[-1] == "'":
        tab = tab[1:-1].replace("''", "'")
    return (tab or None), rng.strip()


def slice_grid(grid: list[list[str]], a1_range: str) -> tuple[list[list[str]], int, int]:
    """Вырезать диапазон из сетки. Возвращает (строки, смещение строк, смещение колонок).

    Смещения нужны, чтобы наружу отдавать настоящие номера строк таблицы:
    Клод пересказывает их директору, и «строка 4» должна означать строку 4
    в Google Sheets, а не четвёртую строку в вырезанном куске.
    """
    a1_range = (a1_range or "").strip()
    if not a1_range:
        return grid, 0, 0

    start, _, end = a1_range.partition(":")
    start_match = _A1.match(start.strip())
    end_match = _A1.match(end.strip()) if end.strip() else None
    if not start_match or (end.strip() and not end_match):
        raise McpError(f"Не понимаю диапазон «{a1_range}» — ожидается вид A1:H50")

    row_from = int(start_match.group(2)) - 1 if start_match.group(2) else 0
    col_from = _col_index(start_match.group(1)) if start_match.group(1) else 0

    if end_match:
        row_to = int(end_match.group(2)) if end_match.group(2) else len(grid)
        col_to = (
            _col_index(end_match.group(1)) + 1
            if end_match.group(1)
            else max((len(r) for r in grid), default=0)
        )
    else:
        row_to, col_to = row_from + 1, col_from + 1

    row_from, col_from = max(row_from, 0), max(col_from, 0)
    rows = [row[col_from:col_to] for row in grid[row_from:row_to]]
    return rows, row_from, col_from
