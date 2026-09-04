"""Типы значений полей: разбор, проекция, показ.

Все проверки на чистых функциях — ни базы, ни сети. Поэтому таблица поведения
проверяется целиком, а не по одному случаю на удачу.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.books.types import (
    THIN_NBSP,
    Fact,
    coerce_for_storage,
    parse,
    project,
    render,
)


# ── Разбор ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("100 000,00", Decimal("100000.00")),  # NBSP как разделитель разрядов
        ("1 200 000,00", Decimal("1200000.00")),
        ("190,00", Decimal("190.00")),
        ("-2 500", Decimal("-2500")),
        ("(2 500)", Decimal("-2500")),  # скобки — отрицательное
        ("500000", Decimal("500000")),
        ("95 323,00 ₸", Decimal("95323.00")),
        ("1,234.56", Decimal("1234.56")),  # точка последняя ⇒ она десятичная
        ("1.234,56", Decimal("1234.56")),  # запятая последняя ⇒ она десятичная
    ],
)
def test_money_parses_the_dirt_that_actually_exists(raw: str, expected: Decimal) -> None:
    assert parse("money", raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "#REF!", "#N/A", "уточнить у Айгуль", "—"])
def test_money_refuses_rather_than_returns_zero(raw: str) -> None:
    """Неразобранная сумма — `None`, а не ноль.

    Ноль складывается в сумму и выглядит как факт: строка «оплата 0»
    неотличима от «оплату не смогли прочитать». В книгах, которые ведут
    руками, второе встречается регулярно.
    """
    assert parse("money", raw) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("21.05.2026", date(2026, 5, 21)),
        ("пн 01.06.26", date(2026, 6, 1)),  # так выглядит колонка «Дата» в журнале
        ("01/06/2026", date(2026, 6, 1)),
        ("1.6.26", date(2026, 6, 1)),
    ],
)
def test_date_parses_all_three_shapes(raw: str, expected: date) -> None:
    assert parse("date", raw) == expected


@pytest.mark.parametrize("raw", ["", "#REF!", "не помню", "32.13.2026"])
def test_date_refuses_nonsense(raw: str) -> None:
    assert parse("date", raw) is None


def test_text_keeps_formula_errors_visible() -> None:
    """В текстовой колонке `#REF!` остаётся как есть.

    Для денег это пустота — считать ошибку формулы нулём нельзя. А в тексте её
    надо показать человеку: он и починит формулу, увидев её в гриде.
    """
    assert parse("text", "#REF!") == "#REF!"
    assert parse("money", "#REF!") is None


# ── Проекция в row_facts ─────────────────────────────────────────────────────


def test_money_projects_into_the_numeric_column() -> None:
    fact = project("money", "95 323,00")
    assert fact.num_value == Decimal("95323.00")
    assert fact.date_value is None
    assert fact.text_value  # текст заполняется всегда — по нему идёт поиск


def test_date_projects_iso_text_for_sorting() -> None:
    fact = project("date", "пн 01.06.26")
    assert fact.date_value == date(2026, 6, 1)
    assert fact.text_value == "2026-06-01"


def test_unparsed_projects_to_nothing() -> None:
    assert project("money", "уточнить").empty
    assert Fact().empty


def test_bool_projects_both_ways() -> None:
    assert project("bool", "ДА").bool_value is True
    assert project("bool", "НЕТ").bool_value is False
    # Свободный текст в колонке-флажке — не «нет», а «не разобрали».
    assert project("bool", "Еще рано").bool_value is None


# ── Деньги считаются точно ───────────────────────────────────────────────────


def test_thousand_kopecks_sum_exactly() -> None:
    """Тысяча строк по копейке даёт ровно десять тенге.

    На `float` этот тест не проходит — накапливается ошибка представления. Он
    и написан затем, чтобы попытка «упростить» `numeric` до `float` падала
    сразу, а не всплывала расхождением в контрольной сумме на экране.
    """
    total = sum(
        (project("money", "0,01").num_value for _ in range(1000)),
        start=Decimal("0"),
    )
    assert total == Decimal("10.00")


# ── Хранение ─────────────────────────────────────────────────────────────────


def test_storage_keeps_money_exact_as_string() -> None:
    """В jsonb сумма уезжает строкой, а не числом.

    JSON-число — это double: `1234567.89` при обратном чтении перестаёт быть
    собой. Строка сохраняет копейки дословно.
    """
    stored = coerce_for_storage("money", "1 234 567,89")
    assert stored == "1234567.89"
    assert isinstance(stored, str)


def test_storage_keeps_date_iso() -> None:
    assert coerce_for_storage("date", "пн 01.06.26") == "2026-06-01"


def test_storage_keeps_unparsed_text_verbatim() -> None:
    """Неразобранное сохраняется как написано, а не выбрасывается.

    Человек должен увидеть в гриде то, что стоит в книге, даже если это
    «уточнить у Айгуль» в колонке с суммой. Выбросить значение — значит
    молча потерять данные, которые кто-то ввёл осознанно.
    """
    assert coerce_for_storage("money", "уточнить у Айгуль") == "уточнить у Айгуль"
    assert coerce_for_storage("money", "") is None


# ── Показ ────────────────────────────────────────────────────────────────────


def test_money_renders_the_way_the_book_writes_it() -> None:
    """Разряды — узким неразрывным пробелом, копейки — запятой.

    Ожидание записано escape-последовательностью, а не самим символом: U+202F
    в исходнике неотличим от обычного пробела, и при расхождении по глазам
    понять было бы нечего.
    """
    assert render("money", Decimal("1234567.89")) == f"1{THIN_NBSP}234{THIN_NBSP}567,89"
    assert render("money", Decimal("0")) == "0,00"


def test_date_renders_in_russian_order() -> None:
    assert render("date", date(2026, 6, 1)) == "01.06.2026"


def test_render_of_nothing_is_empty_not_none() -> None:
    assert render("money", None) == ""
    assert render("text", None) == ""


# ── ISO-даты ─────────────────────────────────────────────────────────────────


def test_iso_date_is_not_read_as_day_month_year() -> None:
    """«2026-06-01» — первое июня 2026-го, а не 26 июня 2001-го.

    Разборщик дат ищет шаблон «дд.мм.гг» в любом месте строки и внутри ISO-даты
    находит «26-06-01», начиная с третьего символа. День и год меняются
    местами, и ошибки не видно ниоткуда.

    Поймано на живых данных: значения хранятся в ISO, и вся проекция дат
    оказалась сдвинутой на четверть века — 3632 строки с уверенно неверными
    датами. Поэтому ISO проверяется первым.
    """
    assert parse("date", "2026-06-01") == date(2026, 6, 1)
    assert parse("date", "2026-12-31") == date(2026, 12, 31)


def test_storage_round_trip_keeps_the_date() -> None:
    """Дата, сохранённая и прочитанная обратно, остаётся собой.

    Именно этот круг и был разорван: `coerce_for_storage` писал ISO, а `project`
    читал его как день-месяц-год.
    """
    stored = coerce_for_storage("date", "пн 01.06.26")
    assert stored == "2026-06-01"
    assert project("date", stored).date_value == date(2026, 6, 1)
