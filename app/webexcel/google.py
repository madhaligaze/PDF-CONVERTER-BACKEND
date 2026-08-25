"""Тонкая обёртка над Google для Web-Excel.

Самодостаточна намеренно: не импортирует ни `app.bbc.sheets`, ни `app.mcp.google`
(кроме кредов через настройки), так что удаление `app/webexcel/` не может ничего
сломать.

Ключевое отличие от соседних обёрток: здесь читается не «сетка значений», а
**полный грид с оформлением** — `spreadsheets.get?includeGridData=true`. Именно
оттуда берутся цвета заливки, шрифты, рамки, форматы чисел, объединения ячеек,
ширины колонок и закрепления. Без них раздел был бы «те же цифры в другой
таблице», а задача — чтобы разницы не было видно.

Цена этого — объём. Google отдаёт примерно килобайт JSON на ячейку, поэтому
диапазон всегда ограничен потолками из настроек, а не «весь лист».
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

from app.webexcel.config import GOOGLE_SCOPES, webexcel_settings

log = logging.getLogger(__name__)


class WebExcelError(Exception):
    """Отказ конфигурации / сети / Google с текстом для человека."""


def humanize(exc: Exception) -> str:
    text = str(exc)
    if "429" in text or "Quota exceeded" in text or "RATE_LIMIT" in text:
        return (
            "Google временно ограничил чтение — слишком много обращений подряд. "
            "Это проходит само за минуту"
        )
    if "403" in text and "PERMISSION" in text.upper():
        return (
            "У сервисного аккаунта нет доступа к этой книге — её нужно открыть "
            "на bbc-sheets@bbc-sheets.iam.gserviceaccount.com"
        )
    if "404" in text:
        return "Книга не найдена — проверьте ссылку или id"
    return text


# ── Креды и клиент ──────────────────────────────────────────────────────────


def _load_credentials() -> Credentials:
    raw = webexcel_settings.credentials_source
    if not raw:
        raise WebExcelError(
            "Web-Excel: не заданы креды Google "
            "(WEBEXCEL_SERVICE_ACCOUNT_JSON или BBC_SERVICE_ACCOUNT_JSON)"
        )
    try:
        if raw.startswith("{"):
            return Credentials.from_service_account_info(json.loads(raw), scopes=GOOGLE_SCOPES)
        path = webexcel_settings.credentials_path
        if path is None or not path.is_file():
            raise WebExcelError(f"Web-Excel: файл кредов не найден: {path or raw}")
        return Credentials.from_service_account_file(str(path), scopes=GOOGLE_SCOPES)
    except (ValueError, OSError) as exc:
        raise WebExcelError(f"Web-Excel: некорректные креды Google: {exc}") from exc


@lru_cache(maxsize=1)
def _client() -> gspread.Client:
    return gspread.authorize(_load_credentials())


# ── Кэш ─────────────────────────────────────────────────────────────────────
#
# Квота Google — 60 чтений в минуту на весь сервисный аккаунт, и этот же аккаунт
# обслуживает дашборд. Импорт книги с гридом — тяжёлый вызов; повторное открытие
# той же вкладки не должно ходить в Google заново.

_files_cache: tuple[float, list[dict[str, Any]]] | None = None
_meta_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_grid_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def _fresh(stamp: float) -> bool:
    return (time.monotonic() - stamp) < webexcel_settings.cache_ttl_seconds


def invalidate_cache() -> None:
    global _files_cache
    with _lock:
        _files_cache = None
        _meta_cache.clear()
        _grid_cache.clear()


# ── Перечисление книг ───────────────────────────────────────────────────────


def list_spreadsheets() -> list[dict[str, Any]]:
    """Все таблицы, расшаренные сервисному аккаунту."""
    global _files_cache
    with _lock:
        if _files_cache is not None and _fresh(_files_cache[0]):
            return _files_cache[1]

    try:
        raw = _client().list_spreadsheet_files()
    except Exception as exc:  # noqa: BLE001 — gspread бросает много типов
        raise WebExcelError(humanize(exc)) from exc

    allowed = webexcel_settings.allowed_ids
    files = [
        {
            "id": item.get("id", ""),
            "name": item.get("name", "") or "Без названия",
            "modified": item.get("modifiedTime", ""),
        }
        for item in raw
        if item.get("id") and (not allowed or item.get("id") in allowed)
    ]
    files.sort(key=lambda x: x["name"].lower())

    with _lock:
        _files_cache = (time.monotonic(), files)
    return files


def _open(spreadsheet_id: str) -> gspread.Spreadsheet:
    allowed = webexcel_settings.allowed_ids
    if allowed and spreadsheet_id not in allowed:
        raise WebExcelError("Эта книга не входит в список разрешённых")
    try:
        return _client().open_by_key(spreadsheet_id)
    except Exception as exc:  # noqa: BLE001
        raise WebExcelError(humanize(exc)) from exc


def spreadsheet_meta(spreadsheet_id: str) -> dict[str, Any]:
    """Название книги и метаданные вкладок — без грида, дёшево."""
    with _lock:
        hit = _meta_cache.get(spreadsheet_id)
        if hit and _fresh(hit[0]):
            return hit[1]

    spreadsheet = _open(spreadsheet_id)
    try:
        raw = spreadsheet.fetch_sheet_metadata(
            params={"fields": "properties.title,sheets.properties"}
        )
    except Exception as exc:  # noqa: BLE001
        raise WebExcelError(humanize(exc)) from exc

    tabs = []
    for sheet in raw.get("sheets", []):
        props = sheet.get("properties", {})
        grid = props.get("gridProperties", {})
        tabs.append(
            {
                "sheet_id": props.get("sheetId", 0),
                "title": props.get("title", ""),
                "index": props.get("index", 0),
                "hidden": bool(props.get("hidden")),
                "rows": grid.get("rowCount", 0),
                "cols": grid.get("columnCount", 0),
            }
        )

    meta = {
        "id": spreadsheet_id,
        "title": raw.get("properties", {}).get("title", ""),
        "tabs": tabs,
    }
    with _lock:
        _meta_cache[spreadsheet_id] = (time.monotonic(), meta)
    return meta


# ── Полный грид с оформлением ───────────────────────────────────────────────

# Поля запрашиваются поимённо, а не «всё». Полный ответ включает историю
# правок, защищённые диапазоны, сводные таблицы и картинки — на большой книге
# это десятки лишних мегабайт в каждом запросе.
_GRID_FIELDS = ",".join(
    (
        "properties.title",
        # Локаль книги решает, как читаются её же образцы форматов: «95 323,00»
        # против «95,323.00» и «пн» против «Mon» — это одна и та же строка
        # `#,##0.00` / `ddd`, разобранная по разным правилам.
        "properties.locale",
        "sheets.properties(sheetId,title,index,hidden,tabColor,gridProperties)",
        "sheets.merges",
        "sheets.data.startRow",
        "sheets.data.startColumn",
        "sheets.data.rowMetadata(pixelSize,hiddenByUser)",
        "sheets.data.columnMetadata(pixelSize,hiddenByUser)",
        "sheets.data.rowData.values("
        "formattedValue,effectiveValue,userEnteredValue,note,hyperlink,"
        "effectiveFormat("
        "numberFormat,backgroundColor,borders,horizontalAlignment,"
        "verticalAlignment,wrapStrategy,textRotation,"
        "textFormat(foregroundColor,fontFamily,fontSize,bold,italic,"
        "strikethrough,underline)))",
    )
)


def _a1_col(index_zero_based: int) -> str:
    """0→A, 25→Z, 26→AA."""
    letters = ""
    n = index_zero_based + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _quote_tab(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def fetch_tab_grid(spreadsheet_id: str, tab_title: str) -> dict[str, Any]:
    """Грид одной вкладки с оформлением, ограниченный потолками настроек.

    Возвращает сырой ответ Google (один элемент `sheets`), приведённый к
    словарю с ключами `properties`, `merges`, `data`. Разбор — в `univer.py`:
    здесь сеть, там формат.
    """
    key = (spreadsheet_id, tab_title)
    with _lock:
        hit = _grid_cache.get(key)
        if hit and _fresh(hit[0]):
            return hit[1]

    meta = spreadsheet_meta(spreadsheet_id)
    tab = next((t for t in meta["tabs"] if t["title"] == tab_title), None)
    if tab is None:
        known = ", ".join(f"«{t['title']}»" for t in meta["tabs"]) or "ни одной"
        raise WebExcelError(f"Вкладка «{tab_title}» не найдена. Есть: {known}")

    rows = max(1, min(int(tab["rows"] or 1), webexcel_settings.max_rows))
    cols = max(1, min(int(tab["cols"] or 1), webexcel_settings.max_cols))
    a1 = f"{_quote_tab(tab_title)}!A1:{_a1_col(cols - 1)}{rows}"

    spreadsheet = _open(spreadsheet_id)
    try:
        raw = spreadsheet.fetch_sheet_metadata(
            params={"includeGridData": True, "ranges": [a1], "fields": _GRID_FIELDS}
        )
    except Exception as exc:  # noqa: BLE001
        raise WebExcelError(humanize(exc)) from exc

    sheets = raw.get("sheets", [])
    if not sheets:
        raise WebExcelError(f"Google вернул пустой ответ для вкладки «{tab_title}»")

    result = {
        "spreadsheet_title": raw.get("properties", {}).get("title", meta["title"]),
        "spreadsheet_locale": raw.get("properties", {}).get("locale", ""),
        "sheet": sheets[0],
        "truncated_rows": int(tab["rows"] or 0) > rows,
        "truncated_cols": int(tab["cols"] or 0) > cols,
        "source_rows": int(tab["rows"] or 0),
        "source_cols": int(tab["cols"] or 0),
    }
    with _lock:
        _grid_cache[key] = (time.monotonic(), result)
    return result


__all__ = [
    "WebExcelError",
    "fetch_tab_grid",
    "humanize",
    "invalidate_cache",
    "list_spreadsheets",
    "spreadsheet_meta",
]
