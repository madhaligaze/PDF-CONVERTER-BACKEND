"""Разбор листа «Сводка все ЮР лица»: раскладка и долг.

Эти два предмета проверяются вместе не случайно. Позиции колонок прибиты
числами, а долг читается из колонки 47 — и если книга сдвинется, парсер
продолжит читать соседнюю ячейку и выдаст правдоподобную, но неверную сумму
денег. Тест на раскладку здесь — не про аккуратность, а про то, чтобы такой
сдвиг остановил сборку, а не доехал до экрана.
"""
from __future__ import annotations

import pytest

from app.bbc.dataset import (
    Col,
    LayoutError,
    parse_contract_row,
    parse_contract_rows,
    verify_layout,
)

WIDTH = 57


def header() -> list[str]:
    """Заголовок книги в тех местах, по которым сверяется раскладка."""
    row = [""] * WIDTH
    row[Col.MONTH] = "Мес"
    row[Col.CLIENT] = "Заказчик\n(Название Фирмы)"
    row[Col.CONTRACT_AMOUNT] = "Сумма\nДоговора"
    row[Col.DEPARTMENT] = "Отдел"
    row[Col.CONTRACT_NO] = "№\nДоговора"
    row[Col.SALDO_START] = "Сальдо\nНачало"
    row[Col.AVR_FLAG] = "АВР\n(Реал.)"
    row[Col.SALDO_END] = "Сальдо\nКонец"
    row[Col.DEBIT_CREDIT] = "Дебет / Кредит \n(в т.ч без АВР)"
    return row


def sheet_row(**cells) -> list[str]:
    row = [""] * WIDTH
    for name, value in cells.items():
        row[getattr(Col, name.upper())] = value
    return row


# ── Раскладка ───────────────────────────────────────────────────────────────


def test_matching_header_passes() -> None:
    verify_layout(header())


def test_a_column_inserted_at_the_front_is_caught() -> None:
    """Ровно та поломка, которая уже случилась при переезде на новую книгу."""
    shifted = [""] + header()
    with pytest.raises(LayoutError):
        verify_layout(shifted)


def test_the_error_names_the_columns_that_did_not_match() -> None:
    broken = header()
    broken[Col.DEPARTMENT] = "Направление"
    with pytest.raises(LayoutError) as caught:
        verify_layout(broken)
    assert "Отдел" in str(caught.value)
    assert "Направление" in str(caught.value)


def test_parsing_refuses_a_sheet_with_a_foreign_layout() -> None:
    with pytest.raises(LayoutError):
        parse_contract_rows([[""] * WIDTH, sheet_row(client="ТОО Тест")])


def test_an_empty_grid_is_not_an_error() -> None:
    assert parse_contract_rows([]) == []


# ── Долг ────────────────────────────────────────────────────────────────────


def test_debt_is_read_from_the_book() -> None:
    row = parse_contract_row(2, sheet_row(client="ИП Vector", debit_credit="85 000"))
    assert row.debt == 85_000
    assert row.debt_pending is False
    assert row.debt_broken is False
    assert row.total_debt == 85_000


def test_a_period_that_has_not_started_is_not_debt() -> None:
    """«Еще рано» — книга сама решает, когда долг становится долгом."""
    row = parse_contract_row(2, sheet_row(contract_amount="85 000", debit_credit="Еще рано"))
    assert row.debt is None
    assert row.debt_pending is True
    assert row.total_debt == 0


def test_a_formula_error_is_marked_rather_than_silently_zeroed() -> None:
    row = parse_contract_row(2, sheet_row(debit_credit="#VALUE!"))
    assert row.debt is None
    assert row.debt_broken is True
    assert row.debt_pending is False


# ── Входящий остаток ────────────────────────────────────────────────────────


def contract(*periods: tuple[str, int, str, str]) -> list:
    """Строки одного договора: (период, мес, сальдо начало, долг)."""
    return [
        [""] * WIDTH,  # шапку parse_contract_rows пропускает
        *[
            sheet_row(
                client="ИП Тест",
                contract_no="№1",
                period_label=label,
                month=str(month),
                saldo_start=saldo,
                debit_credit=debt,
            )
            for label, month, saldo, debt in periods
        ],
    ]


def parse(grid: list) -> list:
    grid[0] = header()
    return parse_contract_rows(grid)


def test_carry_in_comes_from_the_first_period_of_the_contract() -> None:
    """ИП ПЕН Н.И.: в июне сальдо −66 000, и книга показывает 66 000 + 99 000."""
    rows = parse(
        contract(
            ("ИЮНЬ 2026", 6, "-66 000", "33 000"),
            ("ИЮЛЬ 2026", 7, "-99 000", "33 000"),
            ("АВГУСТ 2026", 8, "-132 000", "33 000"),
        )
    )
    assert [r.carry_in for r in rows] == [66_000, None, None]
    assert sum(r.total_debt for r in rows) == 165_000


def test_later_periods_never_contribute_their_saldo() -> None:
    """ИП Vector: в июле сальдо −85 000 при долге 85 000 — сложение удвоило бы долг."""
    rows = parse(
        contract(
            ("ИЮНЬ 2026", 6, "", "85 000"),
            ("ИЮЛЬ 2026", 7, "-85 000", "85 000"),
            ("АВГУСТ 2026", 8, "-85 000", "Еще рано"),
        )
    )
    assert [r.carry_in for r in rows] == [None, None, None]
    assert sum(r.total_debt for r in rows) == 170_000


def test_the_old_debt_row_sorts_before_the_months() -> None:
    """У строки «Старые…» месяца нет, и остаток должен достаться именно ей."""
    rows = parse(
        contract(
            ("ИЮНЬ 2026", 6, "-450 000", "0"),
            ("Старые (до Мая/Июня 2026)", 0, "-1 600 000", "450 000"),
        )
    )
    old = next(r for r in rows if r.period_label.startswith("Старые"))
    assert old.carry_in == 1_600_000
    assert sum(r.total_debt for r in rows) == 2_050_000


def test_a_contract_without_an_opening_saldo_carries_nothing() -> None:
    rows = parse(contract(("ИЮНЬ 2026", 6, "", "400 000")))
    assert rows[0].carry_in is None
    assert sum(r.total_debt for r in rows) == 400_000


def test_carry_in_is_counted_once_per_contract_not_per_client() -> None:
    """У клиента два договора — у каждого свой входящий остаток."""
    grid = [header()]
    for contract_no, saldo in (("№1", "-100 000"), ("№2", "-50 000")):
        for month, debt in ((6, "10 000"), (7, "10 000")):
            grid.append(
                sheet_row(
                    client="ИП Тест",
                    contract_no=contract_no,
                    period_label=f"МЕСЯЦ {month}",
                    month=str(month),
                    saldo_start=saldo if month == 6 else "-999 999",
                    debit_credit=debt,
                )
            )
    rows = parse_contract_rows(grid)
    assert sum(r.carry_in or 0 for r in rows) == 150_000
    assert sum(r.total_debt for r in rows) == 190_000
