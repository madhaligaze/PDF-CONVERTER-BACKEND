"""Выгрузка по МСФО.

Главное требование — **структура строк и колонок на листах вариантов совпадает
построчно**: листы отличаются только цифрами, иначе сравнивать их глазами нельзя.
Здесь это и проверяется в первую очередь.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.bbc.dataset import SUBSCRIPTION, ContractRow
from app.bbc.msfo import (
    SHEET_DOCUMENTS,
    SHEET_PERIODS,
    SHEET_RECONCILIATION,
    all_months,
    build_reconciliation,
    build_sheet,
    export,
)
from app.bbc.recognition import V1_PERIOD_PRORATA_WIP, V2_PRORATA_WIP, annotate
from app.bbc.sheets import BbcError


def make_row(**overrides) -> ContractRow:
    base = dict(
        index=2,
        month=6,
        client="ТОО Тест",
        contract_no="№1",
        subject="Сопровождение",
        firm="BBC",
        firm_name="Big Business Consulting",
        departments=("НО",),
        employee="Айдос",
        service_kind=SUBSCRIPTION,
        status="Продление",
        contract_amount=500_000.0,
        paid_amount=None,
        avr_amount=None,
        saldo_start=None,
        saldo_end=None,
        diff_avr_paid=None,
        invoiced=True,
        invoice_no="1",
        invoice_date=date(2026, 6, 1),
        paid=False,
        payments=[],
        avr_signed=False,
        avr_date=None,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        signed_at=date(2024, 2, 1),
    )
    base.update(overrides)
    return annotate(ContractRow(**base))


@pytest.fixture
def rows() -> list[ContractRow]:
    return [
        make_row(index=2),
        # Закрыта актом — попадёт в документарный вариант.
        make_row(
            index=3,
            departments=("ЮО",),
            avr_signed=True,
            avr_amount=300_000.0,
            avr_date=date(2026, 7, 5),
        ),
        # Услуга сентября: по документам её не будет — акта нет.
        make_row(
            index=4,
            month=9,
            departments=("ОБО",),
            period_start=date(2026, 9, 1),
            period_end=date(2026, 9, 30),
        ),
        # Оплачена без акта — аванс, обязательство по договору.
        make_row(index=5, paid=True, paid_amount=200_000.0),
    ]


def labels(grid: list[list]) -> list[str]:
    """Подписи строк без шапки — она у листов намеренно разная."""
    return [str(row[0]) if row else "" for row in grid[4:]]


def label(row: list) -> str:
    """Подпись строки; для строк-разделителей — пустая."""
    return str(row[0]) if row else ""


def find(grid: list[list], prefix: str) -> list:
    return next(row for row in grid if label(row).strip().startswith(prefix))


def find_exact(grid: list[list], text: str) -> list:
    """Точное совпадение — подзаголовки листа начинаются с тех же слов."""
    return next(row for row in grid if label(row).strip() == text)


# ── Совпадение структуры ─────────────────────────────────────────────────────────


def test_variant_sheets_share_row_labels(rows) -> None:
    months = all_months(rows)
    periods = build_sheet(rows, V2_PRORATA_WIP, "A", "", months)
    documents = build_sheet(rows, V1_PERIOD_PRORATA_WIP, "B", "", months)

    assert labels(periods) == labels(documents)


def test_variant_sheets_share_column_count(rows) -> None:
    months = all_months(rows)
    periods = build_sheet(rows, V2_PRORATA_WIP, "A", "", months)
    documents = build_sheet(rows, V1_PERIOD_PRORATA_WIP, "B", "", months)

    assert [len(row) for row in periods] == [len(row) for row in documents]


def test_month_set_is_the_union_of_both_variants(rows) -> None:
    """По документам сентября нет — но колонка должна остаться, иначе листы
    разъедутся и построчное сравнение сломается."""
    months = all_months(rows)
    documents = build_sheet(rows, V1_PERIOD_PRORATA_WIP, "B", "", months)

    assert "2026-09" in months
    assert len(documents[4]) == len(months) + 3  # статья + стандарт + месяцы + итого


def test_sheets_differ_in_numbers_not_in_shape(rows) -> None:
    months = all_months(rows)
    periods = build_sheet(rows, V2_PRORATA_WIP, "A", "", months)
    documents = build_sheet(rows, V1_PERIOD_PRORATA_WIP, "B", "", months)

    assert periods[5:] != documents[5:]  # цифры расходятся
    assert labels(periods) == labels(documents)  # а разметка нет


# ── Содержание отчёта ────────────────────────────────────────────────────────────


def test_report_carries_ifrs_references(rows) -> None:
    """Отчёт должен читаться как отчёт: со ссылками на стандарты."""
    grid = build_sheet(rows, V2_PRORATA_WIP, "A", "", all_months(rows))
    standards = {str(row[1]) for row in grid[5:] if len(row) > 1}

    assert "IFRS 15" in standards
    assert any(item.startswith("IAS") for item in standards)


def test_revenue_line_is_present_and_totals(rows) -> None:
    grid = build_sheet(rows, V2_PRORATA_WIP, "A", "", all_months(rows))
    line = find(grid, "Выручка по договорам")
    months = all_months(rows)

    assert line[-1] == pytest.approx(sum(line[2:-1]))
    assert len(line) == len(months) + 3


def test_department_breakdown_sums_to_revenue(rows) -> None:
    grid = build_sheet(rows, V2_PRORATA_WIP, "A", "", all_months(rows))
    revenue = find(grid, "Выручка по договорам")
    parts = [row for row in grid if label(row).strip().startswith("в том числе")]

    assert parts
    assert sum(row[-1] for row in parts) == pytest.approx(revenue[-1])


def test_contract_liability_uses_ifrs_wording(rows) -> None:
    """«Доходы будущих периодов» в МСФО называются обязательствами по договорам."""
    grid = build_sheet(rows, V2_PRORATA_WIP, "A", "", all_months(rows))
    assert any("Обязательства по договорам" in label(row) for row in grid)


def test_advance_payment_lands_in_contract_liability(rows) -> None:
    grid = build_sheet(rows, V2_PRORATA_WIP, "A", "", all_months(rows))
    line = next(row for row in grid if "Обязательства по договорам" in label(row))

    assert line[-1] == pytest.approx(200_000.0)


# ── Лист сверки ──────────────────────────────────────────────────────────────────


def test_reconciliation_reports_the_gap(rows) -> None:
    grid = build_reconciliation(rows)
    earned = find_exact(grid, "Заработано (по периодам)")
    closed = find_exact(grid, "Закрыто документами (АВР)")
    gap = find_exact(grid, "Разрыв")

    assert gap[-1] == pytest.approx(earned[-1] - closed[-1])
    assert earned[-1] > closed[-1]


def test_reconciliation_share_is_between_zero_and_one(rows) -> None:
    grid = build_reconciliation(rows)
    share = find_exact(grid, "Доля закрытого")

    assert all(0.0 <= value <= 1.0 for value in share[1:-1] if isinstance(value, (int, float)))


def test_reconciliation_uses_the_same_months(rows) -> None:
    grid = build_reconciliation(rows)
    header = grid[3]

    assert len(header) == len(all_months(rows)) + 2  # показатель + месяцы + итого


# ── Выгрузка ─────────────────────────────────────────────────────────────────────


def test_export_without_a_target_explains_what_to_do(rows, monkeypatch) -> None:
    """Сервис-аккаунт не может создать таблицу сам — сообщение должно это объяснять."""
    import app.bbc.msfo as msfo_module

    monkeypatch.setattr(msfo_module.bbc_settings, "msfo_spreadsheet_id", "", raising=False)

    with pytest.raises(BbcError) as error:
        export(rows)

    message = str(error.value)
    assert "BBC_MSFO_SPREADSHEET_ID" in message
    assert "Редактор" in message


def test_sheet_names_are_stable() -> None:
    """Имена вкладок — часть контракта: на них ссылаются люди."""
    assert SHEET_PERIODS == "МСФО (по периодам)"
    assert SHEET_DOCUMENTS == "МСФО (по документам)"
    assert SHEET_RECONCILIATION == "Сверка"


def test_empty_dataset_still_produces_a_valid_grid() -> None:
    grid = build_sheet([], V2_PRORATA_WIP, "A", "", [])

    assert grid
    assert any("Выручка" in label(row) for row in grid)
