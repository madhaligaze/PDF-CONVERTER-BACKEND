"""Разбор ячейки таблицы в типизированное значение.

Почему это лежит в `core`, а не внутри модуля
─────────────────────────────────────────────
Раньше всё это жило в `app/bbc/normalize.py`. Оно и сейчас доступно оттуда —
`normalize` реэкспортирует эти имена, и ни один вызывающий не заметил переезда.

Но грязь в ячейках — не свойство BBC, а свойство таблиц, которые ведут руками.
Разделу «Книги» нужны ровно те же разборщики, а импортировать `app.bbc` он не
имеет права: модуль, задуманный общим, не должен знать про конкретную компанию.
Копия же разъехалась бы с оригиналом на первой правке — и хуже всего, что молча:
две функции с одним именем, по-разному читающие «1 200,00».

Здесь только то, что верно для любой таблицы. Справочники вида «BBC → Big
Business Consulting» остались в `bbc/normalize.py`, где им и место.

Каждый разборщик возвращает `None`, а не подставляет ноль. Пустая ячейка и
неразобранная ячейка — разные вещи, и на деньгах путать их нельзя: ноль
складывается в сумму и выглядит как факт.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

#: Ошибки формул и заглушки, которые нельзя читать как данные.
ERRORS = frozenset(
    {"#N/A", "#VALUE!", "#REF!", "#DIV/0!", "#NAME?", "#NULL!", "#NUM!", "#ERROR!"}
)

#: NBSP, узкий NBSP, тонкий пробел, пробел нулевой ширины — в книгах
#: встречаются все четыре, и все как разделитель разрядов.
SPACES = "   ​"
SPACE_RE = re.compile(f"[{SPACES}\\s]+")

#: Год-месяц-день. Проверяется ПЕРВЫМ, и это не вкусовщина.
#:
#: `DATE_DMY` ищет свой шаблон в любом месте строки, и в «2026-06-01» он
#: находит «26-06-01», начиная с третьего символа: получается 26 июня 2001
#: года. День и год меняются местами молча.
#:
#: Поймано на живых данных: значения хранятся в ISO (иначе при следующем
#: чтении их формат потеряется), и вся проекция дат в `row_facts` оказалась
#: сдвинутой на четверть века — 3632 строки с уверенно неверными датами.
DATE_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")

DATE_DMY = re.compile(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})")

_TRUE = frozenset({"TRUE", "ДА", "YES", "1", "+", "V", "ИСТИНА"})
_FALSE = frozenset({"FALSE", "НЕТ", "NO", "0", "-", "ЛОЖЬ", ""})


def clean(value: object) -> str:
    """Ячейка без обрамления и невидимых разделителей."""
    if value is None:
        return ""
    text = str(value)
    for char in SPACES:
        text = text.replace(char, " ")
    return text.strip()


def is_error(value: object) -> bool:
    return clean(value).upper() in ERRORS


def parse_decimal(value: object) -> Decimal | None:
    """Сумма как `Decimal`. None для пустого, ошибок формул и мусора.

    Понимает `50 000` (с NBSP), `1 200 000,00`, `190,00`, `-2 500`, `(2 500)`
    как отрицательное, `500000`, а также `₸`/`KZT` рядом с числом.

    `Decimal`, а не `float`: на двоичной плавающей точке сумма тысячи строк по
    копейкам не сходится с той же суммой, посчитанной в таблице, — и
    расхождение всплывает не там, где возникло, а в контрольной сумме на
    экране начальника.
    """
    text = clean(value)
    if not text or text.upper() in ERRORS:
        return None

    text = SPACE_RE.sub("", text)
    text = text.replace("₸", "").replace("KZT", "").replace("kzt", "")
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]

    if "," in text and "." in text:
        # Разделитель, стоящий последним, и есть десятичный.
        text = (
            text.replace(".", "").replace(",", ".")
            if text.rfind(",") > text.rfind(".")
            else text.replace(",", "")
        )
    elif "," in text:
        text = text.replace(",", ".")

    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return -amount if negative else amount


def parse_money(value: object) -> float | None:
    """То же, что `parse_decimal`, но во `float`.

    Оставлено для расчётов, которые живут в памяти и уже написаны под `float`.
    Для хранения и для сумм по книге брать `parse_decimal`.
    """
    amount = parse_decimal(value)
    return None if amount is None else float(amount)


def parse_date(value: object) -> date | None:
    """Разбирает `2026-05-21`, `21.05.2026` и `пн 18.05.26`.

    Двузначный год — двухтысячные. Формат с днём недели не выдуман: так
    выглядит колонка «Дата» в журнале — с сокращением дня недели впереди и
    узким пробелом после него.

    Порядок проверок значим. ISO идёт первым, потому что `DATE_DMY` находит
    свой шаблон и внутри ISO-строки: в «2026-06-01» он видит «26-06-01» с
    третьего символа и читает как 26 июня 2001 года. Молча.
    """
    text = clean(value)
    if not text or text.upper() in ERRORS:
        return None

    iso = DATE_ISO.search(text)
    if iso is not None:
        year, month, day = (int(part) for part in iso.groups())
        return _date_or_none(year, month, day)

    match = DATE_DMY.search(text)
    if match is None:
        return None

    day, month, year = (int(part) for part in match.groups())
    if year < 100:
        year += 2000
    return _date_or_none(year, month, day)


def _date_or_none(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_bool(value: object) -> bool | None:
    """Разбирает ячейку-флажок.

    Возвращает `None` для свободного текста, который тоже живёт в таких
    колонках («Часть», «Еще рано»): что означает промежуточное состояние —
    решает вызывающий, а не разборщик.
    """
    text = clean(value).upper()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


__all__ = [
    "DATE_DMY",
    "DATE_ISO",
    "ERRORS",
    "SPACES",
    "SPACE_RE",
    "clean",
    "is_error",
    "parse_bool",
    "parse_date",
    "parse_decimal",
    "parse_money",
]
