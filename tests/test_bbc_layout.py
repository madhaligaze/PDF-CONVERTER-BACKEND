"""Привязка колонок по названиям — `app.bbc.layout`.

Проверяется ровно то, ради чего модуль написан: книгу правят руками, и парсер
обязан пережить вставку, удаление и перестановку колонок, но обязан отказаться
читать лист, в котором не может отличить одну колонку от другой. Второе не
менее важно первого: цена неверной догадки — деньги на экране начальника.
"""
from __future__ import annotations

import pytest

from app.bbc.journal import JOURNAL_COLUMNS, parse_journal, resolve_journal_layout
from app.bbc.layout import Column, LayoutError, norm, resolve_layout, squash

COLUMNS = (
    Column("date", "Дата", ("Дата",), hint=0),
    Column("amount", "Сумма", ("Сумма Договора",), hint=1),
    Column("note", "Коммент", ("Комментарий",), hint=2, required=False),
)


def resolve(header: list[str]):
    return resolve_layout("Тест", COLUMNS, header)


# ── Нормализация ────────────────────────────────────────────────────────────


def test_newlines_and_padding_are_not_meaning() -> None:
    """Шапки в книге свёрстаны, а не написаны: « Приход », «Сальдо\\nКонец»."""
    assert norm(" Приход ") == "приход"
    assert norm("Сальдо\nКонец") == "сальдо конец"
    assert norm("ЁЛКА") == "елка"


def test_squash_ignores_punctuation_and_spacing() -> None:
    assert squash("№ Договора") == squash("№Договора") == "№договора"


# ── Что должно переживаться ─────────────────────────────────────────────────


def test_a_column_inserted_at_the_front_shifts_nothing_that_matters() -> None:
    layout = resolve(["Новая", "Дата", "Сумма Договора", "Комментарий"])
    assert layout.at("date") == 1
    assert layout.at("amount") == 2
    assert layout.shifted


def test_reordered_columns_are_found_where_they_now_are() -> None:
    layout = resolve(["Сумма Договора", "Комментарий", "Дата"])
    assert layout.at("date") == 2
    assert layout.at("amount") == 0
    assert layout.cell(["1 000", "текст", "01.08.2026"], "date") == "01.08.2026"


def test_an_optional_column_may_simply_be_gone() -> None:
    layout = resolve(["Дата", "Сумма Договора"])
    assert layout.absent == ("note",)
    assert layout.at("note") is None
    assert layout.cell(["01.08.2026", "1 000"], "note") == ""


def test_a_row_shorter_than_the_header_reads_as_empty() -> None:
    """Google обрезает хвостовые пустые ячейки — строка бывает короче шапки."""
    layout = resolve(["Дата", "Сумма Договора", "Комментарий"])
    assert layout.cell(["01.08.2026"], "note") == ""


def test_drift_names_the_column_and_both_positions() -> None:
    layout = resolve(["Новая", "Дата", "Сумма Договора"])
    moved = {item.key: (item.was, item.now) for item in layout.drift}
    assert moved["date"] == (0, 1)
    assert "«Дата»" in layout.describe_drift()


def test_an_unchanged_book_reports_no_drift() -> None:
    layout = resolve(["Дата", "Сумма Договора", "Комментарий"])
    assert not layout.shifted
    assert layout.to_dict()["drift"] == []


def test_an_alias_covers_a_renamed_header() -> None:
    """«АВР (Реал.)» стало «АВР (наша)» — оба написания читаются."""
    columns = (Column("avr", "АВР", ("АВР (наша)", "АВР (Реал.)"), hint=0),)
    for spelling in ("АВР (наша)", "АВР (Реал.)"):
        assert resolve_layout("Тест", columns, [spelling]).at("avr") == 0


# ── Что должно останавливать чтение ─────────────────────────────────────────


def test_a_missing_required_column_is_refused() -> None:
    with pytest.raises(LayoutError) as caught:
        resolve(["Дата", "Комментарий"])
    assert "Сумма" in str(caught.value)


def test_the_error_shows_what_stands_near_the_old_position() -> None:
    """Чтобы было видно, чем колонку заменили, а не только что её нет."""
    with pytest.raises(LayoutError) as caught:
        resolve(["Дата", "Итого к оплате", "Комментарий"])
    assert "итого к оплате" in str(caught.value).lower()


def test_two_columns_may_not_claim_the_same_cell() -> None:
    columns = (
        Column("a", "Первая", ("Сумма",), hint=0),
        Column("b", "Вторая", ("Сумма",), hint=0),
    )
    with pytest.raises(LayoutError) as caught:
        resolve_layout("Тест", columns, ["Сумма"])
    assert "одну и ту же" in str(caught.value)


def test_a_duplicate_header_is_resolved_by_the_hint() -> None:
    """В книге две «Сальдо» — подсказка говорит, какая из них наша."""
    columns = (
        Column("start", "Сальдо Начало", ("Сальдо",), hint=1),
        Column("end", "Сальдо Конец", ("Сальдо",), hint=5),
    )
    layout = resolve_layout("Тест", columns, ["", "Сальдо", "", "", "", "Сальдо"])
    assert layout.at("start") == 1
    assert layout.at("end") == 5


def test_an_ambiguous_fuzzy_match_is_refused_rather_than_guessed() -> None:
    """Два похожих кандидата на денежную колонку — это отказ, а не монетка."""
    columns = (Column("amount", "Сумма", ("Сумма Договора",), hint=0),)
    with pytest.raises(LayoutError) as caught:
        resolve_layout("Тест", columns, ["Сумма Договора (план)", "Сумма Договора (факт)"])
    assert "похожих" in str(caught.value)


def test_separator_columns_are_never_candidates() -> None:
    """В книге полно колонок «.» — по ним ничего искаться не должно."""
    columns = (Column("note", "Коммент", ("Комментарий",), hint=0, required=False),)
    assert resolve_layout("Тест", columns, [".", ".", "."]).at("note") is None


# ── «Журнал»: та самая тихая поломка ────────────────────────────────────────


def journal_header(*, with_payroll_column: bool) -> list[str]:
    """Шапка «Журнала» до и после вставки колонки «ФОТ/Детали»."""
    header = [
        "ДДС Мес (цифра)", "ОПиУ период", "Дата", "Счет", " Приход ", " Расход ",
        " Контрагент ", " Дробление\n1 суммы ", " №Дог. ", " Подкатегория ",
    ]
    if with_payroll_column:
        header.append(" ФОТ/Детали ")
    header += [" Проект ", " Категория ", " Комментарии "]
    return header


def test_the_journal_column_insert_no_longer_shifts_the_category() -> None:
    """Настоящая тихая поломка: «Категория» читалась из пустого «Проекта».

    У «Журнала» проверки шапки не было вовсе, позиции были прибиты числами, и
    вставленная колонка «ФОТ/Детали» сдвинула «Проект», «Категорию» и
    «Комментарии» на +1. Сводка по категориям расходов молча опустела.
    """
    header = journal_header(with_payroll_column=True)
    row = [""] * 10 + ["ФОТ Июль", "Проект А", "Перемещения", "коммент"]
    row[2], row[4] = "01.07.2026", "100 000"

    rows = parse_journal([header, row])

    assert len(rows) == 1
    assert rows[0].category == "Перемещения"
    assert rows[0].project == "Проект А"
    assert rows[0].comment == "коммент"
    assert rows[0].inflow == 100_000


def test_the_journal_reads_the_book_from_before_the_insert_too() -> None:
    """Одна и та же сборка обязана читать обе редакции книги."""
    header = journal_header(with_payroll_column=False)
    row = [""] * 10 + ["Проект А", "Перемещения", "коммент"]
    row[2], row[5] = "01.07.2026", "40 000"

    rows = parse_journal([header, row])

    assert rows[0].category == "Перемещения"
    assert rows[0].outflow == 40_000
    assert "payroll_loan" in resolve_journal_layout(header).absent


def test_every_journal_column_has_a_distinct_header() -> None:
    """«Категория» и «Подкатегория» не должны ловить друг друга."""
    layout = resolve_journal_layout(journal_header(with_payroll_column=True))
    found = [layout.at(c.key) for c in JOURNAL_COLUMNS if layout.has(c.key)]
    assert len(found) == len(set(found))
