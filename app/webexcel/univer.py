"""Google Sheets → снимок книги Univer.

Здесь живёт весь перевод оформления: заливка, шрифт, цвет текста, начертание,
выравнивание, перенос, поворот, рамки, формат числа, объединения, ширины колонок,
высоты строк, закрепления, скрытые строки/колонки, цвет ярлыка вкладки.

Два решения, без которых модуль не работал бы вообще:

**Стили дедуплицируются.** Google повторяет полный `effectiveFormat` в каждой
ячейке — на вкладке 60×26 это 1,4 МБ JSON. Univer же умеет реестр:
`styles: {"1": {...}}` и `cellData[r][c].s = "1"`. Одинаковых оформлений в
финансовой книге считаные десятки, поэтому объём падает на два порядка.

**Самое частое оформление уезжает в `defaultStyle` вкладки.** В книге, где
99% ячеек — белый фон и Arial 10, ссылка `{"s":"3"}` в каждой пустой ячейке
стоит дороже самих данных. Ячейка, чьё оформление совпало с умолчанием и в
которой нет значения, не попадает в снимок вовсе.

Формулы переносятся не всегда, и это не упрощение. `IMPORTRANGE` тянет данные
из чужой книги, `ARRAYFORMULA` разливается по диапазону, `QUERY` — свой язык.
Пересчитать их у себя мы не можем; подставить формулу, которая вернёт ошибку,
означает показать финансисту `#REF!` там, где в Google стоит сумма. Поэтому:
формула переносится, если она посильна нашему движку, иначе переносится
посчитанное значение, а текст формулы кладётся в `custom.gsFormula` — ничего
не теряется.
"""
from __future__ import annotations

import json
import re
from typing import Any

# ── Перечисления Univer (дублируются числами: Python не импортирует TS) ─────

_H_ALIGN = {"LEFT": 1, "CENTER": 2, "RIGHT": 3, "JUSTIFY": 4}
_V_ALIGN = {"TOP": 1, "MIDDLE": 2, "BOTTOM": 3}
_WRAP = {"OVERFLOW_CELL": 1, "LEGACY_WRAP": 3, "CLIP": 2, "WRAP": 3}
_WRAP_OVERFLOW = 1
_WRAP_CLIP = 2

# BorderStyleTypes из @univerjs/core
_BORDER = {
    "NONE": 0,
    "DOTTED": 3,
    "DASHED": 4,
    "SOLID": 1,  # THIN
    "SOLID_MEDIUM": 8,  # MEDIUM
    "SOLID_THICK": 13,  # THICK
    "DOUBLE": 7,
}

# Сколько пустых строк оставить под последней заполненной: финансист дописывает
# в конец, и упереться в границу листа сразу после последней записи неудобно.
_EMPTY_TAIL_ROWS = 50

# CellValueType
_T_STRING = 1
_T_NUMBER = 2
_T_BOOLEAN = 3

# Функции, которые наш движок не воспроизведёт: формула таких ячеек не
# переносится, переносится её результат.
_UNPORTABLE = re.compile(
    r"\b(IMPORTRANGE|IMPORTDATA|IMPORTHTML|IMPORTXML|IMPORTFEED|ARRAYFORMULA|QUERY"
    r"|GOOGLEFINANCE|GOOGLETRANSLATE|DETECTLANGUAGE|SPARKLINE|IMAGE|FILTER|SORTN"
    r"|FLATTEN|LAMBDA|LET|BYROW|BYCOL|MAKEARRAY|REDUCE|SCAN|MAP)\s*\(",
    re.IGNORECASE,
)


# Локаль книги → код Excel, которым помечается образец формата.
#
# Это не украшательство. Образец `#,##0.00` сам по себе не содержит ни
# разделителя разрядов, ни десятичного знака — только их места. Правила берутся
# из локали, и без метки движок в браузере применяет английские: «95,323.00»
# там, где в Google стоит «95 323,00», и «Mon» там, где «пн». Цифры совпадают,
# а читаются иначе — тот же класс расхождения, что и колонка, съехавшая на
# соседнюю: ошибки не видно, потому что она выглядит как данные.
#
# Парная половина решения — регистрация этих локалей во фронтенде
# (`src/components/web-excel/numfmt-locale.ts`). Одна без другой не работает.
_LOCALE_TAGS = {
    "ru": "[$-419]",
    "kk": "[$-43F]",
    "uk": "[$-422]",
}


def _locale_tag(spreadsheet_locale: str) -> str:
    """«ru_RU» → «[$-419]». Незнакомая локаль — без метки (английские правила)."""
    language = (spreadsheet_locale or "").replace("-", "_").split("_")[0].lower()
    return _LOCALE_TAGS.get(language, "")


def _hex(color: dict[str, Any] | None) -> str | None:
    """Google `{red,green,blue}` (0..1, нули опущены) → `#RRGGBB`.

    Отсутствующий ключ канала означает 0, а не «не задан»: `{}` — это чёрный,
    а не «цвет не указан». Отличать «нет цвета» приходится по отсутствию
    самого поля, а не по его пустоте.
    """
    if color is None:
        return None
    r = int(round(float(color.get("red", 0.0)) * 255))
    g = int(round(float(color.get("green", 0.0)) * 255))
    b = int(round(float(color.get("blue", 0.0)) * 255))
    return f"#{r:02X}{g:02X}{b:02X}"


def _border_side(side: dict[str, Any] | None) -> dict[str, Any] | None:
    if not side:
        return None
    style = _BORDER.get(str(side.get("style", "")).upper())
    if not style:
        return None
    return {"s": style, "cl": {"rgb": _hex(side.get("color")) or "#000000"}}


def _style_of(cell_format: dict[str, Any] | None, locale_tag: str = "") -> dict[str, Any]:
    """`effectiveFormat` Google → `IStyleData` Univer. Пустые поля опускаются."""
    if not cell_format:
        return {}
    style: dict[str, Any] = {}

    bg = cell_format.get("backgroundColor")
    if bg is not None:
        style["bg"] = {"rgb": _hex(bg)}

    text = cell_format.get("textFormat") or {}
    fg = text.get("foregroundColor")
    if fg is not None:
        style["cl"] = {"rgb": _hex(fg)}
    if text.get("fontFamily"):
        style["ff"] = text["fontFamily"]
    if text.get("fontSize"):
        style["fs"] = int(text["fontSize"])
    if text.get("bold"):
        style["bl"] = 1
    if text.get("italic"):
        style["it"] = 1
    if text.get("underline"):
        style["ul"] = {"s": 1}
    if text.get("strikethrough"):
        style["st"] = {"s": 1}

    h = _H_ALIGN.get(str(cell_format.get("horizontalAlignment", "")).upper())
    if h:
        style["ht"] = h
    v = _V_ALIGN.get(str(cell_format.get("verticalAlignment", "")).upper())
    if v:
        style["vt"] = v
    rotation = cell_format.get("textRotation") or {}
    if rotation.get("angle"):
        style["tr"] = {"a": int(rotation["angle"])}
    elif rotation.get("vertical"):
        style["tr"] = {"a": 0, "v": 1}

    borders = cell_format.get("borders") or {}
    bd = {}
    for google_key, univer_key in (("top", "t"), ("bottom", "b"), ("left", "l"), ("right", "r")):
        side = _border_side(borders.get(google_key))
        if side:
            bd[univer_key] = side
    if bd:
        style["bd"] = bd

    # Перенос решается последним, потому что зависит от рамок.
    #
    # `OVERFLOW` у ячейки С РАМКОЙ заменяется на `CLIP`, и это не косметика.
    # Пара «рамка + перетекание» — самое дорогое, что есть в отрисовке: на
    # каждую видимую ячейку движок ищет, чей текст мог бы налезть на эту
    # границу, и обходит ради этого строки. В профиле прокрутки «Журнала» на
    # `renderBorderByCell → _getOverflowExclusion → forRow` уходило 36%
    # времени, 95-й процентиль кадра — 574 мс, то есть таблица заметно
    # дёргалась под рукой. Со снятым перетеканием у обрамлённых ячеек — 121 мс.
    #
    # Расхождение с Google при этом почти не видно: обрамлённая ячейка и так
    # выглядит замкнутой коробкой, а текст, переехавший через нарисованную
    # границу, в этих книгах смотрится ошибкой. Ячейки без рамок перетекание
    # сохраняют — там оно и заметно, и уместно.
    w = _WRAP.get(str(cell_format.get("wrapStrategy", "")).upper())
    if w == _WRAP_OVERFLOW and bd:
        w = _WRAP_CLIP
    if w:
        style["tb"] = w

    number = cell_format.get("numberFormat") or {}
    pattern = number.get("pattern")
    # `TEXT` без образца и `NUMBER` с пустым образцом — это «как есть»: свой
    # `n.pattern` там только помешает Univer выбрать представление самому.
    if pattern and str(number.get("type", "")).upper() != "TEXT":
        # Метка ставится только если её ещё нет: у образцов, набранных руками
        # в стиле Excel, свой префикс уже бывает, и второй сделал бы образец
        # неразбираемым.
        prefix = locale_tag if locale_tag and not pattern.startswith("[$-") else ""
        style["n"] = {"pattern": f"{prefix}{pattern}"}

    return style


def _merge_columns(cells: list[tuple[int, int]]) -> list[dict[str, int]]:
    """Клетки → вертикальные диапазоны по колонкам.

    Флажки в книге стоят колонками на всю таблицу: у «Сводки все ЮР лица» это
    900 ячеек в одном столбце. Отдавать их поштучно — 900 правил проверки
    данных на лист, и Univer честно создаст 900 объектов. Склейка в подряд
    идущие отрезки превращает это в одно правило.
    """
    by_column: dict[int, list[int]] = {}
    for row, column in cells:
        by_column.setdefault(column, []).append(row)

    ranges: list[dict[str, int]] = []
    for column, rows in sorted(by_column.items()):
        rows.sort()
        start = previous = rows[0]
        for row in rows[1:]:
            if row == previous + 1:
                previous = row
                continue
            ranges.append({"startRow": start, "endRow": previous, "startColumn": column, "endColumn": column})
            start = previous = row
        ranges.append({"startRow": start, "endRow": previous, "startColumn": column, "endColumn": column})
    return ranges


def _canonical(style: dict[str, Any]) -> str:
    return json.dumps(style, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _value_of(cell: dict[str, Any]) -> tuple[Any, int | None]:
    """`effectiveValue` → (значение, CellValueType). Ошибка → её текст строкой."""
    effective = cell.get("effectiveValue")
    if effective is None:
        return None, None
    if "numberValue" in effective:
        number = effective["numberValue"]
        # Целые числа отдаём int: 2026.0 в JSON занимает больше и в редакторе
        # показывается как «2026» только благодаря формату, которого может не быть.
        if isinstance(number, float) and number.is_integer():
            number = int(number)
        return number, _T_NUMBER
    if "stringValue" in effective:
        return effective["stringValue"], _T_STRING
    if "boolValue" in effective:
        return bool(effective["boolValue"]), _T_BOOLEAN
    if "errorValue" in effective:
        # Ошибка Google переносится тем, что видно на экране («#REF!»), а не
        # структурой: пересчитывать её нечем, а прятать нельзя — в исходнике
        # она есть, и финансист должен увидеть ровно то же.
        return cell.get("formattedValue", "#ERROR!"), _T_STRING
    return None, None


def convert_tab(payload: dict[str, Any]) -> dict[str, Any]:
    """Ответ `fetch_tab_grid` → `{sheet, styles, stats}` для сборки книги."""
    sheet = payload["sheet"]
    locale_tag = _locale_tag(payload.get("spreadsheet_locale", ""))
    props = sheet.get("properties", {})
    grid_props = props.get("gridProperties", {})
    data = (sheet.get("data") or [{}])[0]

    row_data = data.get("rowData") or []
    row_meta = data.get("rowMetadata") or []
    col_meta = data.get("columnMetadata") or []

    # Хвост из пустых строк — это чистый вес и ничего больше. Google отдаёт
    # оформление на каждую строку запрошенного диапазона, даже если данных там
    # нет с 2019 года: у «Журнала» это 500 строк по 32 ячейки, и каждая тащит
    # ссылку на стиль. Оформление пустого поля всё равно совпадает с
    # `defaultStyle` вкладки, так что на экране обрезка не видна.
    last_filled = 0
    for index, row in enumerate(row_data):
        if any(cell.get("effectiveValue") is not None for cell in (row.get("values") or [])):
            last_filled = index + 1
    if last_filled:
        row_data = row_data[: last_filled + _EMPTY_TAIL_ROWS]

    # ── Проход 1: стили и значения ──────────────────────────────────────────
    style_counts: dict[str, int] = {}
    fonts: set[str] = set()
    # Клетки-флажки, собираемые построчно и потом склеиваемые в диапазоны.
    checkbox_cells: list[tuple[int, int]] = []
    parsed: list[list[tuple[Any, int | None, str, str | None, str | None]]] = []
    max_col = 0

    for row in row_data:
        values = row.get("values") or []
        max_col = max(max_col, len(values))
        parsed_row: list[tuple[Any, int | None, str, str | None, str | None]] = []
        for column, cell in enumerate(values):
            condition = (cell.get("dataValidation") or {}).get("condition") or {}
            is_checkbox = str(condition.get("type", "")).upper() == "BOOLEAN"
            if is_checkbox:
                checkbox_cells.append((len(parsed), column))
            value, kind = _value_of(cell)
            if is_checkbox and kind == _T_BOOLEAN:
                # Флажок Univer рисуется, только если значение ячейки совпадает
                # с «отмечено»/«снято» его правила, а это 1 и 0. Булево True
                # строкой даёт «true», не совпадает ни с чем, и вместо галочки
                # на экране остаётся слово TRUE — ровно то, что и увидели на
                # первой импортированной книге. В Sheets и Excel ИСТИНА и так
                # равна единице, так что подмена не меняет смысла ячейки.
                value, kind = (1 if value else 0), _T_NUMBER
            style = _style_of(cell.get("effectiveFormat"), locale_tag)
            if style.get("ff"):
                fonts.add(style["ff"])
            style_key = _canonical(style)
            style_counts[style_key] = style_counts.get(style_key, 0) + 1

            formula = (cell.get("userEnteredValue") or {}).get("formulaValue")
            portable = None
            keep = None
            if formula:
                if _UNPORTABLE.search(formula) or "errorValue" in (cell.get("effectiveValue") or {}):
                    keep = formula
                else:
                    portable = formula
            parsed_row.append((value, kind, style_key, portable, keep))
        parsed.append(parsed_row)

    # ── Умолчание вкладки — самое частое оформление ─────────────────────────
    default_key = max(style_counts, key=lambda k: style_counts[k]) if style_counts else "{}"
    default_style = json.loads(default_key)

    styles: dict[str, dict[str, Any]] = {}
    style_ids: dict[str, str] = {default_key: ""}

    def style_id(key: str) -> str:
        existing = style_ids.get(key)
        if existing is not None:
            return existing
        assigned = str(len(styles) + 1)
        styles[assigned] = json.loads(key)
        style_ids[key] = assigned
        return assigned

    # ── Проход 2: сборка cellData ───────────────────────────────────────────
    cell_data: dict[str, dict[str, Any]] = {}
    for i, parsed_row in enumerate(parsed):
        row_cells: dict[str, Any] = {}
        for j, (value, kind, style_key, portable, keep) in enumerate(parsed_row):
            cell: dict[str, Any] = {}
            if value is not None and value != "":
                cell["v"] = value
                if kind:
                    cell["t"] = kind
            sid = style_id(style_key)
            if sid:
                cell["s"] = sid
            if portable:
                cell["f"] = portable
            if keep:
                cell["custom"] = {"gsFormula": keep}
            if cell:
                row_cells[str(j)] = cell
        if row_cells:
            cell_data[str(i)] = row_cells

    # ── Геометрия ───────────────────────────────────────────────────────────
    rows_out: dict[str, dict[str, Any]] = {}
    for i, meta in enumerate(row_meta):
        entry: dict[str, Any] = {}
        if meta.get("pixelSize"):
            entry["h"] = int(meta["pixelSize"])
            # `ia: 0` — «высота задана, мерить не надо». Без этого движок считает
            # строку самоподстраивающейся и на каждом кадре обходит её ячейки,
            # чтобы вычислить высоту по содержимому. В профиле прокрутки это
            # 35% времени в одной функции обхода строк. Высота у нас точная,
            # пришла из Google в пикселях, и пересчитывать её не по чему.
            entry["ia"] = 0
        if meta.get("hiddenByUser"):
            entry["hd"] = 1
        if entry:
            rows_out[str(i)] = entry

    cols_out: dict[str, dict[str, Any]] = {}
    for j, meta in enumerate(col_meta):
        entry = {}
        if meta.get("pixelSize"):
            entry["w"] = int(meta["pixelSize"])
        if meta.get("hiddenByUser"):
            entry["hd"] = 1
        if entry:
            cols_out[str(j)] = entry

    # Google отдаёт границы объединения полуоткрытыми (end исключается), Univer —
    # замкнутыми. Забыть про -1 значит растянуть каждое объединение на клетку
    # вправо и вниз: шапка наедет на первую строку данных.
    merges = []
    for merge in sheet.get("merges") or []:
        merges.append(
            {
                "startRow": int(merge.get("startRowIndex", 0)),
                "endRow": int(merge.get("endRowIndex", 1)) - 1,
                "startColumn": int(merge.get("startColumnIndex", 0)),
                "endColumn": int(merge.get("endColumnIndex", 1)) - 1,
            }
        )

    # Размер листа — ровно то, что мы привезли, и ни строкой больше.
    #
    # Раньше сюда шёл `gridProperties.rowCount` исходной книги. У «Журнала» это
    # 5837 строк при потолке импорта в 2000: лист объявлялся на 5000 строк, из
    # которых 3000 — пустая порода. И это не просто память: `defaultStyle`
    # вкладки несёт рамки со всех четырёх сторон, то есть движок обходил и
    # обрисовывал 5000×32 клеток вместо 2050×32. Замер прокрутки: 95-й
    # процентиль кадра 552 мс против 22 мс у вкладки, где объявленный размер
    # совпадал с привезённым. Это и была «таблица подвисает».
    row_count = max(len(row_data), 100)
    col_count = max(max_col, len(col_meta), 26)

    worksheet: dict[str, Any] = {
        "id": f"gs-{props.get('sheetId', 0)}",
        "name": props.get("title", "Лист"),
        "rowCount": min(row_count, 5000),
        "columnCount": min(col_count, 200),
        "cellData": cell_data,
        "rowData": rows_out,
        "columnData": cols_out,
        "mergeData": merges,
        "defaultStyle": default_style or None,
        "freeze": {
            "xSplit": int(grid_props.get("frozenColumnCount", 0) or 0),
            "ySplit": int(grid_props.get("frozenRowCount", 0) or 0),
            "startRow": int(grid_props.get("frozenRowCount", 0) or 0),
            "startColumn": int(grid_props.get("frozenColumnCount", 0) or 0),
        },
        "showGridlines": 0 if grid_props.get("hideGridlines") else 1,
        "hidden": 1 if props.get("hidden") else 0,
    }
    tab_color = _hex(props.get("tabColor")) if props.get("tabColor") is not None else None
    if tab_color:
        worksheet["tabColor"] = tab_color

    return {
        "sheet": worksheet,
        "styles": styles,
        "stats": {
            "rows": len(row_data),
            "cols": max_col,
            "styles": len(styles) + 1,
            "merges": len(merges),
            "truncated": bool(payload.get("truncated_rows") or payload.get("truncated_cols")),
            "source_rows": payload.get("source_rows", 0),
            "source_cols": payload.get("source_cols", 0),
        },
        # Диапазоны флажков — отдельным списком, потому что в снимке Univer они
        # живут не в ячейках, а в ресурсе плагина проверки данных.
        "checkboxes": _merge_columns(checkbox_cells),
        # Шрифты листа отдаются наружу, чтобы фронт подгрузил ровно их.
        # Univer рисует в canvas: незагруженное семейство там не «подменяется
        # похожим», а падает в засечковый шрифт по умолчанию — лист, набранный
        # Montserrat, приезжает Times New Roman.
        "fonts": sorted(fonts),
    }


def build_workbook(
    spreadsheet_id: str, title: str, tabs: list[dict[str, Any]]
) -> dict[str, Any]:
    """Собрать `IWorkbookData` из уже сконвертированных вкладок.

    Реестры стилей вкладок независимы, поэтому при сборке они переносятся в
    один общий с переименованием id. Склеить их «как есть» значило бы, что
    вторая вкладка перекрасит первую в свои цвета — молча и целиком.
    """
    styles: dict[str, Any] = {}
    sheets: dict[str, Any] = {}
    order: list[str] = []
    stats: list[dict[str, Any]] = []
    fonts: set[str] = set()

    for converted in tabs:
        fonts.update(converted.get("fonts", []))
        sheet = converted["sheet"]
        remap: dict[str, str] = {}
        for local_id, style in converted["styles"].items():
            global_id = str(len(styles) + 1)
            styles[global_id] = style
            remap[local_id] = global_id

        for row in sheet["cellData"].values():
            for cell in row.values():
                if "s" in cell:
                    cell["s"] = remap[cell["s"]]

        sheets[sheet["id"]] = sheet
        order.append(sheet["id"])
        stats.append({"name": sheet["name"], **converted["stats"]})

    return {
        "id": f"gs-{spreadsheet_id}",
        "name": title,
        "locale": "ruRU",
        "sheetOrder": order,
        "sheets": sheets,
        "styles": styles,
        "custom": {"source": "google-sheets", "spreadsheetId": spreadsheet_id},
        "_stats": stats,
        "_fonts": sorted(fonts),
    }


__all__ = ["build_workbook", "convert_tab"]
