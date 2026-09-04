"""Что нашлось в чужой книге: колонки, их типы и статистика.

Это первая половина «разговора двух сторон». Здесь книга рассказывает о себе:
какие в ней колонки, что в них лежит, насколько они заполнены. Про смысл для
приложения тут не знают ничего — смысл появляется позже, когда поле привяжут к
роли.

Модуль чистый: на входе грид строк, на выходе описания полей. Ни базы, ни сети.

Чего здесь принципиально нет
────────────────────────────
Догадок о смысле. Колонка «Сумма» не становится «суммой договора» оттого, что
называется похоже, — она становится ею, только когда человек подтвердит
привязку или когда совпадение окажется единственным. Определение типа — это
про «здесь лежат даты», а не про «здесь лежит дата оплаты».

Разница не формальная. Стоит один раз угадать смысл денежной колонки — и
дашборд покажет уверенные неверные цифры, а это худший из возможных исходов:
неверные цифры со знаком качества.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field as dc_field
from typing import Any, Sequence

from app.core.scalars import (
    SPACE_RE,
    clean,
    is_error,
    parse_bool,
    parse_date,
    parse_decimal,
)

#: Доля значений, при которой тип считается определённым. Ниже — колонка
#: смешанная, и честнее назвать её текстом, чем объявить датой лист, где даты
#: стоят в половине строк.
CONFIDENT_RATIO = 0.8

#: Сколько различных значений ещё считается списком, а не свободным текстом.
ENUM_MAX_DISTINCT = 24
#: ...и какую долю выборки они при этом должны занимать.
ENUM_MAX_SHARE = 0.35
#: Меньше этого числа значений — о списке говорить рано.
ENUM_MIN_SAMPLE = 20

#: Заголовки, по которым числовая колонка считается денежной.
#: Это подсказка о *типе* («тут деньги»), а не о роли («тут сумма договора»).
MONEY_HINTS = (
    "сумм", "оплат", "приход", "расход", "сальдо", "долг", "стоимост", "цена",
    "тариф", "платеж", "платёж", "аванс", "остаток", "итог", "ндс", "оклад",
    "фот", "выручк", "затрат", "начислен", "дебет", "кредит", "баланс",
)

#: Слова, по которым «да/нет» опознаётся именно как флажок, а не как 0/1.
#: Без этого колонка из нулей и единиц (номер месяца, признак) объявлялась бы
#: булевой, потому что «0» и «1» разбираются и как число, и как ложь/истина.
BOOL_TOKENS = frozenset(
    {"TRUE", "FALSE", "ДА", "НЕТ", "YES", "NO", "ИСТИНА", "ЛОЖЬ", "+", "V"}
)

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "№": "n",
}

_NOT_KEY = re.compile(r"[^a-z0-9]+")


def slugify(title: str) -> str:
    """Заголовок → машинный ключ: «№ Договора» → `n_dogovora`.

    Транслитерация, а не кириллица как есть: ключ уезжает в адреса, в имена
    полей формы и в jsonb, и латиница там читается везде одинаково. Заодно
    исчезает разнобой «ё/е», из-за которого два написания одного заголовка
    давали два разных ключа.
    """
    lowered = clean(title).lower()
    latin = "".join(_TRANSLIT.get(char, char) for char in lowered)
    key = _NOT_KEY.sub("_", latin).strip("_")
    return key


def unique_key(title: str, position: int, taken: set[str]) -> str:
    """Ключ, которого ещё нет среди занятых.

    Два случая, оба живые. Пустой или чисто служебный заголовок — в книгах это
    колонки-разделители, подписанные точкой; у них ключа не выходит вовсе, и
    они получают позиционный `col_15`. Одинаковые заголовки в разных блоках
    листа — второму достаётся `_2`.

    Позиция в имени `col_15` — не адрес, а последнее средство назвать
    безымянное. Искать по ней ничего нельзя.
    """
    base = slugify(title) or f"col_{position}"
    if base not in taken:
        taken.add(base)
        return base
    index = 2
    while f"{base}_{index}" in taken:
        index += 1
    key = f"{base}_{index}"
    taken.add(key)
    return key


@dataclass
class DiscoveredField:
    """Колонка, как её увидели в книге."""

    key: str
    title: str
    type: str
    position: int
    names: list[str] = dc_field(default_factory=list)
    stats: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "type": self.type,
            "position": self.position,
            "names": list(self.names),
            "stats": dict(self.stats),
        }


def _is_separator(title: str) -> bool:
    """Колонка-разделитель: подписана точкой, дефисом или ничем."""
    return not re.sub(r"[^0-9a-zA-Zа-яА-Я№]+", "", clean(title))


def detect_header_row(grid: Sequence[Sequence[Any]], limit: int = 10) -> int:
    """Номер строки с заголовками, считая с нуля.

    В чужих книгах шапка не всегда первая: сверху бывает название отчёта,
    период или пустая строка для красоты. Шапкой считается строка с наибольшим
    числом непустых, различных и коротких текстовых ячеек — заголовки короткие
    и не повторяются, а строка данных длинная и может повторяться.

    Возвращается именно догадка. Она пишется в `tables.header_row` и остаётся
    исправляемой человеком: угадать шапку — это не то же самое, что угадать
    смысл денежной колонки, но ошибиться тут тоже можно.
    """
    best_row, best_score = 0, -1.0
    for index, row in enumerate(grid[:limit]):
        cells = [clean(cell) for cell in row]
        filled = [cell for cell in cells if cell and not is_error(cell)]
        if not filled:
            continue
        distinct = len(set(filled))
        # Заголовок короткий: длинные ячейки — это уже данные (комментарии,
        # назначения платежей).
        short = sum(1 for cell in filled if len(cell) <= 40)
        # Числа и даты в шапке не стоят.
        texty = sum(
            1
            for cell in filled
            if parse_decimal(cell) is None and parse_date(cell) is None
        )
        score = distinct * 1.0 + short * 0.5 + texty * 1.0
        if score > best_score:
            best_row, best_score = index, score
    return best_row


def _title_of(raw: Any) -> str:
    """Заголовок без оформления: переносы строк и повторы пробелов схлопнуты.

    В книгах шапку верстают переносами — «Дробление\\n1 суммы», «Дата\\nДДС
    (дубль)». Перенос это оформление, а не часть названия: оставленный в
    заголовке, он ломает и показ на табло, и сравнение с синонимом роли,
    который человек напишет в одну строку.
    """
    return SPACE_RE.sub(" ", clean(raw)).strip()


def last_data_row(grid: Sequence[Sequence[Any]], start: int) -> int:
    """Индекс последней строки, где вообще что-то есть.

    Ниже данных в книгах тянутся сотни пустых строк — лист заводят с запасом.
    Считать их строками данных значит занижать заполненность каждой колонки в
    несколько раз и объявлять живые колонки пустыми.
    """
    for index in range(len(grid) - 1, start, -1):
        if any(clean(cell) for cell in grid[index]):
            return index
    return start


def sample_rows_of(
    grid: Sequence[Sequence[Any]], start: int, sample_rows: int
) -> list[Sequence[Any]]:
    """Строки данных для выборки — равномерно по всему листу, а не сверху.

    Первые N строк — плохая выборка, и это не теория. В пилотном журнале
    сверху лежит текущий месяц, а «Категория», «Проект» и «Вопросы» заполнены
    в более старых строках ниже. По окну из первых 400 строк все три выходили
    пустыми и получали тип `unknown` — то есть книга объявлялась беднее, чем
    она есть, а поля, которые надо было привязать, не показывались на табло
    вовсе.

    Равномерный шаг стоит столько же: грид уже прочитан целиком.
    """
    last = last_data_row(grid, start)
    first = start + 1
    total = last - start
    if total <= 0:
        return []
    if total <= sample_rows:
        return list(grid[first : last + 1])
    stride = total / sample_rows
    return [grid[first + int(step * stride)] for step in range(sample_rows)]


def _column_values(rows: Sequence[Sequence[Any]], index: int) -> list[str]:
    """Непустые значения колонки в выборке."""
    values: list[str] = []
    for row in rows:
        if index >= len(row):
            continue
        text = clean(row[index])
        if not text or is_error(text):
            continue
        values.append(text)
    return values


def _looks_like_money(title: str, values: Sequence[str]) -> bool:
    """Числовая колонка — денежная или просто числовая?

    Два признака, и любого достаточно: заголовок говорит о деньгах, либо сами
    значения оформлены как деньги — с разделителем разрядов или ровно двумя
    знаками после запятой. Год «2026» и номер месяца «6» ни того, ни другого
    не дают и остаются числами.
    """
    lowered = clean(title).lower()
    if any(hint in lowered for hint in MONEY_HINTS):
        return True

    formatted = 0
    for value in values:
        if re.search(r"[   ]\d{3}\b", value):  # разделитель разрядов
            formatted += 1
        elif re.search(r"[.,]\d{2}$", value):  # ровно две копейки
            formatted += 1
    return bool(values) and formatted / len(values) >= 0.3


def infer_type(title: str, values: Sequence[str]) -> tuple[str, dict[str, Any]]:
    """Тип колонки и то, чем он обоснован.

    Возвращается вместе со статистикой намеренно: на табло привязок человек
    видит не только вердикт, но и на чём он основан («дата, 97 % значений
    разбираются»). Вердикт без обоснования нечем оспорить.
    """
    total = len(values)
    if total == 0:
        return "unknown", {"filled": 0}

    dates = sum(1 for value in values if parse_date(value) is not None)
    numbers = sum(1 for value in values if parse_decimal(value) is not None)
    bools = sum(1 for value in values if parse_bool(value) is not None)
    tokens = sum(1 for value in values if value.strip().upper() in BOOL_TOKENS)
    distinct = len(set(values))

    stats: dict[str, Any] = {
        "sample": total,
        "distinct": distinct,
        "date_ratio": round(dates / total, 3),
        "number_ratio": round(numbers / total, 3),
        "bool_ratio": round(bools / total, 3),
        "examples": [value for value, _ in Counter(values).most_common(5)],
    }

    # Флажок — только если есть хоть одно словесное «да/нет». Колонка из нулей
    # и единиц разбирается и как булево, и как число; без этой оговорки номер
    # месяца объявлялся бы флажком.
    if bools / total >= CONFIDENT_RATIO and tokens > 0:
        return "bool", stats

    # Дата раньше числа: «01.06.2026» числом не разбирается, но проверять
    # порядок надёжнее, чем полагаться на это.
    if dates / total >= CONFIDENT_RATIO:
        return "date", stats

    if numbers / total >= CONFIDENT_RATIO:
        kind = "money" if _looks_like_money(title, values) else "number"
        return kind, stats

    if (
        total >= ENUM_MIN_SAMPLE
        and 2 <= distinct <= ENUM_MAX_DISTINCT
        and distinct / total <= ENUM_MAX_SHARE
    ):
        stats["options"] = sorted({value for value in values})
        return "enum", stats

    return "text", stats


def discover_fields(
    grid: Sequence[Sequence[Any]],
    *,
    header_row: int | None = None,
    sample_rows: int = 300,
    max_cols: int | None = None,
) -> tuple[list[DiscoveredField], int]:
    """Грид → описания колонок. Возвращает поля и номер использованной шапки.

    Колонки-разделители («.», пустые) не выбрасываются: в них тоже бывают
    данные, и человек должен видеть книгу такой, какая она есть. Они просто
    получают позиционный ключ и тип `unknown`, пока в них ничего не нашлось.
    """
    if not grid:
        return [], 0

    start = detect_header_row(grid) if header_row is None else header_row
    start = max(0, min(start, len(grid) - 1))
    header = list(grid[start])

    width = max((len(row) for row in grid[start:]), default=len(header))
    if max_cols is not None:
        width = min(width, max_cols)

    taken: set[str] = set()
    fields: list[DiscoveredField] = []
    sampled = sample_rows_of(grid, start, sample_rows)
    scanned = len(sampled)

    for index in range(width):
        title = _title_of(header[index]) if index < len(header) else ""
        values = _column_values(sampled, index)
        field_type, stats = infer_type(title, values)

        stats["separator"] = _is_separator(title)
        stats["filled"] = len(values)
        stats["scanned"] = scanned
        stats["fill_ratio"] = round(len(values) / scanned, 3) if scanned else 0.0

        fields.append(
            DiscoveredField(
                key=unique_key(title, index, taken),
                title=title,
                type=field_type,
                position=index,
                names=[title] if title else [],
                stats=stats,
            )
        )

    return fields, start


__all__ = [
    "CONFIDENT_RATIO",
    "DiscoveredField",
    "MONEY_HINTS",
    "detect_header_row",
    "discover_fields",
    "infer_type",
    "last_data_row",
    "sample_rows_of",
    "slugify",
    "unique_key",
]
