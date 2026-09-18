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
from collections.abc import Callable
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
#
# Кэшируется готовый ответ вкладки байтами, а не сырой ответ Google. Раньше
# было наоборот, и это была главная утечка процесса: грид «Журнала» с
# оформлением — 240 МБ объектов Python, готовый ответ из него — 2 МБ JSON. К
# тому же записи только добавлялись: просроченная лежала до следующего чтения
# той же вкладки, то есть обычно до перезапуска. Три журнала одной книги —
# гигабайт, пока процесс не перезапустят.
#
# Почему байты, а не та же вкладка объектами (16 МБ). Замер на «Журнале»:
# объекты создаются, пока сырой грид ещё жив, и ложатся на те же страницы
# памяти. Грид освобождён, но страницы вернуть системе нельзя — на каждой
# осталось по несколько живых объектов кэша. Итог: 480 МБ процесса против
# 185 МБ, когда в кэше лежит одна строка байт.

_files_cache: tuple[float, list[dict[str, Any]]] | None = None
_meta_cache: dict[str, tuple[float, dict[str, Any]]] = {}
#: Готовые ответы вкладок — ровно те байты, что уходят на фронт. См. `cached_tab`.
_tab_cache: dict[tuple[str, str], tuple[float, bytes]] = {}
#: Значения вкладки строками — для переноса книги в учёт.
_values_cache: dict[tuple[str, str], tuple[float, list[list[str]]]] = {}
#: Справочники выпадающих списков: диапазон → его значения.
_ref_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}
_lock = threading.Lock()

#: Потолок кэша готовых вкладок в байтах. Считается по объёму, а не по числу
#: вкладок: «Справочник» и «Журнал» различаются в двадцать раз. Большая вкладка —
#: около 2 МБ, так что книга целиком помещается с запасом.
_TAB_CACHE_MAX_BYTES = 32 * 1024 * 1024
#: Потолок записей для остальных кэшей: там не гриды с оформлением, а строки.
_MAX_ENTRIES = 32


def _fresh(stamp: float) -> bool:
    return (time.monotonic() - stamp) < webexcel_settings.cache_ttl_seconds


def _remember(
    cache: dict[Any, tuple[float, Any]],
    key: Any,
    value: Any,
    *,
    weight: Callable[[Any], int] = lambda _: 1,
    limit: int | None = None,
) -> None:
    """Положить в кэш, заодно выбросив просроченное и лишнее. Под `_lock`.

    Лишнее — самое старое сверх `limit` (по умолчанию `_MAX_ENTRIES`), считая
    по `weight` (по умолчанию — просто записи). Последняя положенная запись
    остаётся всегда.
    """
    limit = _MAX_ENTRIES if limit is None else limit
    now = time.monotonic()
    ttl = webexcel_settings.cache_ttl_seconds
    for stale in [k for k, (stamp, _) in cache.items() if now - stamp >= ttl]:
        del cache[stale]
    cache.pop(key, None)  # переложить в конец: порядок словаря — порядок записи
    cache[key] = (now, value)
    total = sum(weight(held) for _, held in cache.values())
    while total > limit and len(cache) > 1:
        total -= weight(cache.pop(next(iter(cache)))[1])


def invalidate_cache() -> None:
    global _files_cache
    with _lock:
        _files_cache = None
        _meta_cache.clear()
        _tab_cache.clear()
        _values_cache.clear()
        _ref_cache.clear()


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
        _remember(_meta_cache, spreadsheet_id, meta)
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
        # dataValidation — это флажки и выпадающие списки. Флажок в Google не
        # «значение TRUE», а ячейка с условием BOOLEAN: без условия колонка
        # «Счет» приезжает столбцом слова TRUE вместо галочек. Списки живут там
        # же: ONE_OF_LIST держит значения при себе, ONE_OF_RANGE — ссылку на
        # диапазон-справочник, поэтому `values` берётся вместе с типом.
        "sheets.data.rowData.values("
        "formattedValue,effectiveValue,userEnteredValue,note,hyperlink,"
        "dataValidation.condition(type,values),"
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

    Не кэшируется намеренно: ответ огромен и нужен ровно на время разбора.
    Повторное открытие вкладки обслуживает `cached_tab` — готовой вкладкой.
    """
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

    return {
        "spreadsheet_title": raw.get("properties", {}).get("title", meta["title"]),
        "spreadsheet_locale": raw.get("properties", {}).get("locale", ""),
        "sheet": sheets[0],
        "truncated_rows": int(tab["rows"] or 0) > rows,
        "truncated_cols": int(tab["cols"] or 0) > cols,
        "source_rows": int(tab["rows"] or 0),
        "source_cols": int(tab["cols"] or 0),
    }


def cached_tab(
    spreadsheet_id: str,
    tab_title: str,
    build: Callable[[dict[str, Any]], bytes],
) -> bytes:
    """Готовый ответ вкладки: из кэша, иначе грид из Google → `build` → в кэш.

    Сырой грид живёт ровно столько, сколько `build` его разбирает, и в кэш не
    попадает. `build` возвращает готовые байты ответа — почему байты, а не
    объекты, см. комментарий у `_tab_cache`. Отказ Google не кэшируется:
    исключение просто уходит наружу.
    """
    key = (spreadsheet_id, tab_title)
    with _lock:
        hit = _tab_cache.get(key)
        if hit and _fresh(hit[0]):
            return hit[1]

    body = build(fetch_tab_grid(spreadsheet_id, tab_title))

    with _lock:
        _remember(_tab_cache, key, body, weight=len, limit=_TAB_CACHE_MAX_BYTES)
    return body


#: Потолок на справочник выпадающего списка. Список в тысячу строк — это уже не
#: выбор, а поиск; Univer рисует его целиком, и вкладка встаёт.
_LIST_LIMIT = 500


def values_of_ref(spreadsheet_id: str, ref: str) -> list[str]:
    """Значения диапазона-справочника из условия ONE_OF_RANGE.

    В книгах BBC списки заданы не перечислением, а ссылкой вида
    `='Справочник'!$I$2:$I`. Без этого разрешения колонка приезжает без списка:
    правило есть, выбирать не из чего — а выглядит это как «списки не
    поддерживаются».

    Пустые ячейки и повторы выбрасываются: конец столбца-справочника почти
    всегда пустой, и без чистки в списке оказывались бы сотни пустых строк.
    """
    cleaned = ref.strip().lstrip("=").strip()
    if not cleaned:
        return []
    key = (spreadsheet_id, cleaned)
    with _lock:
        hit = _ref_cache.get(key)
        if hit and _fresh(hit[0]):
            return hit[1]

    spreadsheet = _open(spreadsheet_id)
    try:
        raw = spreadsheet.values_get(cleaned, params={"valueRenderOption": "FORMATTED_VALUE"})
    except Exception as exc:  # noqa: BLE001
        raise WebExcelError(humanize(exc)) from exc

    seen: list[str] = []
    known: set[str] = set()
    for row in raw.get("values", []):
        for cell in row:
            text = str(cell).strip()
            if not text or text in known:
                continue
            known.add(text)
            seen.append(text)
            if len(seen) >= _LIST_LIMIT:
                break
        if len(seen) >= _LIST_LIMIT:
            break

    with _lock:
        _remember(_ref_cache, key, seen)
    return seen


def fetch_tab_values(spreadsheet_id: str, tab_title: str) -> list[list[str]]:
    """Значения вкладки строками — без оформления, как их видит человек.

    Отдельно от `fetch_tab_grid` по цене: грид стоит примерно килобайт на
    ячейку, потому что несёт цвета, рамки и шрифты. Тому, кто переносит книгу в
    учёт, оформление не нужно — нужны дата, сумма и комментарий.

    Значения запрашиваются **форматированные**: «18.09.2026» и «95 323,00», а не
    46 271 и 95323. Разбор в «Финансах» читает человеческий текст — он для того и
    написан, чтобы понимать выписки. Сырые значения пришлось бы переводить
    обратно через эпоху дат, и на этом переводе теряется день.
    """
    key = (spreadsheet_id, tab_title)
    with _lock:
        hit = _values_cache.get(key)
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
        raw = spreadsheet.values_get(
            a1,
            params={
                "valueRenderOption": "FORMATTED_VALUE",
                "dateTimeRenderOption": "FORMATTED_STRING",
            },
        )
    except Exception as exc:  # noqa: BLE001
        raise WebExcelError(humanize(exc)) from exc

    values = [[str(cell) for cell in row] for row in raw.get("values", [])]
    with _lock:
        _remember(_values_cache, key, values)
    return values


__all__ = [
    "WebExcelError",
    "cached_tab",
    "fetch_tab_grid",
    "fetch_tab_values",
    "humanize",
    "invalidate_cache",
    "list_spreadsheets",
    "spreadsheet_meta",
    "values_of_ref",
]
