"""Инструменты, которые коннектор объявляет Клоду.

Все пять — только чтение. Пишущих нет и не должно появиться: токен Google
выпущен с `spreadsheets.readonly`, так что запись не пройдёт даже при ошибке
здесь, но полагаться на это как на единственный барьер нельзя.

Описания (`description`) написаны по-русски и с примерами вопросов не для
красоты: Клод выбирает инструмент по тексту описания и никогда не видит этот
файл. Если он читает таблицу целиком вместо `search_rows` — править надо здесь,
а не в логике.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from app.mcp import google
from app.mcp.config import mcp_settings
from app.mcp.google import McpError

# Сколько строк отдаём по умолчанию, когда человек не просил конкретный кусок.
DEFAULT_PEEK_ROWS = 15
DEFAULT_SEARCH_LIMIT = 50


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=1)


def _cap(rows: list[list[str]], max_rows: int | None) -> tuple[list[list[str]], bool]:
    """Обрезать по числу строк и по потолку ячеек. Второе значение — «обрезано»."""
    truncated = False
    if max_rows is not None and max_rows > 0 and len(rows) > max_rows:
        rows, truncated = rows[:max_rows], True

    width = max((len(r) for r in rows), default=0) or 1
    limit = max(mcp_settings.max_cells // width, 1)
    if len(rows) > limit:
        rows, truncated = rows[:limit], True
    return rows, truncated


def _numbered(rows: list[list[str]], row_offset: int) -> list[dict[str, Any]]:
    """Строки с их настоящими номерами в Google Sheets."""
    return [{"row": row_offset + i + 1, "values": values} for i, values in enumerate(rows)]


# ── Реализации ──────────────────────────────────────────────────────────────


def list_spreadsheets() -> str:
    files = google.list_spreadsheet_files()
    return _dump(
        {
            "count": len(files),
            "spreadsheets": files,
            "подсказка": (
                "Дальше вызывайте describe_spreadsheet, чтобы увидеть вкладки, "
                "или сразу peek_sheet, чтобы понять раскладку колонок."
            ),
        }
    )


def describe_spreadsheet(spreadsheet: str) -> str:
    spreadsheet_id = google.resolve_spreadsheet_id(spreadsheet)
    meta = google.spreadsheet_meta(spreadsheet_id)
    return _dump(meta)


def peek_sheet(spreadsheet: str, tab: str | None = None, rows: int | None = None) -> str:
    spreadsheet_id = google.resolve_spreadsheet_id(spreadsheet)
    tab_title = google.resolve_tab(spreadsheet_id, tab)
    grid = google.read_grid(spreadsheet_id, tab_title)

    wanted = rows if rows and rows > 0 else DEFAULT_PEEK_ROWS
    head, truncated = _cap(grid[:wanted], wanted)
    return _dump(
        {
            "spreadsheet": google.spreadsheet_meta(spreadsheet_id)["title"],
            "spreadsheet_id": spreadsheet_id,
            "tab": tab_title,
            "total_rows": len(grid),
            "showing_rows": len(head),
            "truncated": truncated,
            "rows": _numbered(head, 0),
        }
    )


def read_range(
    spreadsheet: str, range: str | None = None, max_rows: int | None = None  # noqa: A002
) -> str:
    spreadsheet_id = google.resolve_spreadsheet_id(spreadsheet)
    tab_from_range, cells = google.split_range(range or "")
    tab_title = google.resolve_tab(spreadsheet_id, tab_from_range)
    grid = google.read_grid(spreadsheet_id, tab_title)

    sliced, row_offset, col_offset = google.slice_grid(grid, cells)
    capped, truncated = _cap(sliced, max_rows)
    return _dump(
        {
            "spreadsheet": google.spreadsheet_meta(spreadsheet_id)["title"],
            "spreadsheet_id": spreadsheet_id,
            "tab": tab_title,
            "first_column_index": col_offset + 1,
            "total_rows_in_range": len(sliced),
            "returned_rows": len(capped),
            "truncated": truncated,
            "truncation_note": (
                "Показана только часть диапазона. Сузьте range или используйте "
                "search_rows, чтобы получить нужные строки."
                if truncated
                else ""
            ),
            "rows": _numbered(capped, row_offset),
        }
    )


def search_rows(
    spreadsheet: str,
    query: str,
    tab: str | None = None,
    column: str | None = None,
    limit: int | None = None,
) -> str:
    if not (query or "").strip():
        raise McpError("search_rows: не задано, что искать")

    spreadsheet_id = google.resolve_spreadsheet_id(spreadsheet)
    tab_title = google.resolve_tab(spreadsheet_id, tab)
    grid = google.read_grid(spreadsheet_id, tab_title)
    if not grid:
        raise McpError(f"Вкладка «{tab_title}» пуста")

    header = grid[0]
    needle = query.strip().lower()

    col_index: int | None = None
    if (column or "").strip():
        wanted = column.strip().lower()
        for i, name in enumerate(header):
            if name.strip().lower() == wanted:
                col_index = i
                break
        if col_index is None:
            raise McpError(
                f"Колонка «{column}» не найдена. В заголовке: "
                + ", ".join(f"«{c}»" for c in header if c.strip())
            )

    cap = limit if limit and limit > 0 else DEFAULT_SEARCH_LIMIT
    matches: list[dict[str, Any]] = []
    total = 0
    for i, row in enumerate(grid[1:], start=2):  # 1-based номера строк, как в Sheets
        cells = [row[col_index]] if col_index is not None and col_index < len(row) else row
        if any(needle in (cell or "").lower() for cell in cells):
            total += 1
            if len(matches) < cap:
                matches.append({"row": i, "values": row})

    return _dump(
        {
            "spreadsheet": google.spreadsheet_meta(spreadsheet_id)["title"],
            "spreadsheet_id": spreadsheet_id,
            "tab": tab_title,
            "header": header,
            "query": query,
            "column": column or "",
            "total_matches": total,
            "returned": len(matches),
            "truncated": total > len(matches),
            "rows": matches,
        }
    )


# ── Объявления для tools/list ───────────────────────────────────────────────

_SPREADSHEET_ARG = {
    "type": "string",
    "description": (
        "Таблица: её название («Реестр продаж»), id или ссылка на неё. "
        "Название можно указывать частично."
    ),
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_spreadsheets",
        "description": (
            "Перечислить все Google-таблицы, к которым у компании открыт доступ: "
            "название, id, дата последнего изменения. Начинайте с этого инструмента, "
            "когда не знаете, какие отчёты вообще существуют — например на вопрос "
            "«какие отчёты есть» или «что у нас за этот месяц»."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "describe_spreadsheet",
        "description": (
            "Показать вкладки таблицы и их размеры. Нужен, чтобы понять, из какой "
            "вкладки читать: в одной таблице обычно лежат и сводка, и помесячные листы."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"spreadsheet": _SPREADSHEET_ARG},
            "required": ["spreadsheet"],
            "additionalProperties": False,
        },
    },
    {
        "name": "peek_sheet",
        "description": (
            "Показать первые строки вкладки вместе с заголовком колонок и общим числом "
            "строк. Вызывайте ЭТО перед read_range: так вы узнаете раскладку колонок, "
            "не вычитывая всю таблицу. Финансовые таблицы здесь на тысячи строк."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "spreadsheet": _SPREADSHEET_ARG,
                "tab": {
                    "type": "string",
                    "description": "Название вкладки. Не указано — первая вкладка.",
                },
                "rows": {
                    "type": "integer",
                    "description": f"Сколько строк показать (по умолчанию {DEFAULT_PEEK_ROWS}).",
                },
            },
            "required": ["spreadsheet"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_range",
        "description": (
            "Прочитать конкретный диапазон вкладки в нотации A1 — например "
            "«Журнал!A1:H200» или «B2:F40». Пользуйтесь этим, когда уже знаете, где "
            "лежат нужные данные (обычно после peek_sheet). Для поиска по значению "
            "берите search_rows: он дешевле и не съедает контекст."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "spreadsheet": _SPREADSHEET_ARG,
                "range": {
                    "type": "string",
                    "description": (
                        "Диапазон A1, можно с названием вкладки: «Лист!A1:H50». "
                        "Не указан — вся первая вкладка."
                    ),
                },
                "max_rows": {
                    "type": "integer",
                    "description": "Потолок числа строк в ответе.",
                },
            },
            "required": ["spreadsheet"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_rows",
        "description": (
            "Найти во вкладке строки, где встречается искомый текст — название клиента, "
            "месяц, ФИО менеджера, номер договора. Регистр не важен, совпадение по "
            "вхождению. Возвращает заголовок таблицы и найденные строки с их настоящими "
            "номерами. Это основной инструмент для вопросов вида «сколько заплатил "
            "такой-то клиент» или «что было в июле»."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "spreadsheet": _SPREADSHEET_ARG,
                "query": {"type": "string", "description": "Что искать."},
                "tab": {
                    "type": "string",
                    "description": "Название вкладки. Не указано — первая вкладка.",
                },
                "column": {
                    "type": "string",
                    "description": (
                        "Искать только в этой колонке — по её названию из строки "
                        "заголовков. Не указано — по всей строке."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": f"Сколько строк вернуть (по умолчанию {DEFAULT_SEARCH_LIMIT}).",
                },
            },
            "required": ["spreadsheet", "query"],
            "additionalProperties": False,
        },
    },
]

HANDLERS: dict[str, Callable[..., str]] = {
    "list_spreadsheets": list_spreadsheets,
    "describe_spreadsheet": describe_spreadsheet,
    "peek_sheet": peek_sheet,
    "read_range": read_range,
    "search_rows": search_rows,
}
