"""Блок «Отдел продаж»: разбор листа ОМиП и отдача на маркетинговые каналы.

Сетка в фикстуре повторяет живой лист «Отчет Июль»: слева план (B–H), справа
факт (M–S), выручка отдела в J/K, ниже в колонке факта — пары «менеджер / сумма».
"""
from __future__ import annotations

import pytest

from app.bbc.sales import (
    canonical_channel,
    channel_results,
    parse_sales_registry,
    parse_sales_report,
)


def cell(row: dict[int, str], width: int = 20) -> list[str]:
    out = [""] * width
    for index, value in row.items():
        out[index] = value
    return out


@pytest.fixture
def grid() -> list[list[str]]:
    """Уменьшенная копия реального листа, с теми же подписями и колонками."""
    return [
        cell({}),
        cell({1: "ТАБЛИЦА РАСХОД/ДОХОД ОМиП ВВС"}),
        cell({1: "Расходы на текущий месяц"}),
        cell({1: "ФОТ", 9: "ПЛАН", 10: "ФАКТ", 12: "ФОТ"}),
        cell({
            3: "Фикс", 4: "бонус", 5: "на руки", 6: "налоги", 7: "всего",
            9: "7 000 000", 10: "5 379 000",
            14: "Фикс", 15: "бонус", 16: "на руки", 17: "налоги", 18: "всего",
        }),
        cell({
            1: "Кумисбаев Б.", 2: "РОП", 3: "500 000,0 ₸", 4: "700 000,0 ₸",
            5: "1 200 000,0 ₸", 6: "315 151,5 ₸", 7: "1 515 151,5 ₸",
            10: "Усман",
            12: "Кумисбаев Б.", 13: "РОП", 14: "500 000,0 ₸", 15: "537 900,0 ₸",
            16: "1 037 900,0 ₸", 17: "272 579,8 ₸", 18: "1 310 479,8 ₸",
        }),
        cell({
            1: "Усман", 2: "МОП", 3: "250 000,0 ₸", 4: "245 000,0 ₸",
            5: "495 000,0 ₸", 6: "146 382,6 ₸", 7: "641 382,6 ₸",
            10: "2 079 000",
            12: "Усман", 13: "МОП", 14: "250 000,0 ₸", 15: "145 530,0 ₸",
            16: "395 530,0 ₸", 17: "120 259,1 ₸", 18: "515 789,1 ₸",
        }),
        cell({
            1: "Дамир", 2: "Таргет", 3: "250 000,0 ₸", 4: "0,0 ₸",
            5: "250 000,0 ₸", 6: "82 039,1 ₸", 7: "332 039,1 ₸",
            10: "Акежан",
            12: "Дамир", 13: "Таргет", 14: "250 000,0 ₸", 15: "0,0 ₸",
            16: "250 000,0 ₸", 17: "82 039,1 ₸", 18: "332 039,1 ₸",
        }),
        cell({10: "3 300 000"}),
        cell({
            1: "ИТОГО", 3: "1 000 000,0 ₸", 7: "2 488 572,8 ₸",
            12: "ИТОГО", 14: "1 000 000,0 ₸", 18: "2 158 308,0 ₸",
        }),
        cell({}),
        cell({1: "Подрядчики", 12: "Подрядчики"}),
        cell({
            1: "ИП We Make", 2: "Контекст", 3: "770 000,0 ₸", 7: "770 000,0 ₸",
            12: "ИП We Make", 13: "Контекст", 14: "770 000,0 ₸", 18: "770 000,0 ₸",
        }),
        cell({1: "ИТОГО", 3: "770 000,0 ₸", 12: "ИТОГО", 14: "770 000,0 ₸"}),
        cell({}),
        cell({1: "Бюджет на рекламу", 12: "Бюджет на рекламу"}),
        cell({1: "Таргет", 3: "650 000,0 ₸", 12: "Таргет", 14: "650 000,0 ₸"}),
        cell({1: "Контекс", 3: "1 040 000,0 ₸", 12: "Контекс", 14: "1 040 000,0 ₸"}),
        cell({1: "ИТОГО", 7: "1 690 000,0 ₸", 12: "ИТОГО", 18: "1 690 000,0 ₸"}),
        cell({}),
        cell({1: "ИТОГО РАСХОДЫ", 7: "5 708 705,8 ₸", 12: "ИТОГО РАСХОДЫ", 18: "5 360 763,9 ₸"}),
    ]


# ── Выручка ──────────────────────────────────────────────────────────────────────


def test_department_plan_and_fact_are_read(grid) -> None:
    report = parse_sales_report(grid, "Отчет Июль")

    assert report.revenue_plan == 7_000_000
    assert report.revenue_fact == 5_379_000


def test_plan_completion_is_derived(grid) -> None:
    assert parse_sales_report(grid).plan_completion == pytest.approx(5_379_000 / 7_000_000)


def test_revenue_is_split_per_manager(grid) -> None:
    per_mop = parse_sales_report(grid).per_mop

    assert {item["name"] for item in per_mop} == {"Усман", "Акежан"}
    assert sum(item["revenue"] for item in per_mop) == 5_379_000


def test_manager_split_matches_the_department_total(grid) -> None:
    """Сумма по менеджерам должна сходиться с фактом отдела."""
    report = parse_sales_report(grid)
    assert sum(item["revenue"] for item in report.per_mop) == report.revenue_fact


# ── ФОТ ──────────────────────────────────────────────────────────────────────────


def test_payroll_is_read_for_plan_and_fact_separately(grid) -> None:
    report = parse_sales_report(grid)

    assert len(report.payroll_plan) == 3
    assert len(report.payroll_fact) == 3


def test_bonus_differs_between_plan_and_fact(grid) -> None:
    """Смысл блока: премия по плану и по факту — разные числа."""
    report = parse_sales_report(grid)
    plan = next(line for line in report.payroll_plan if line.name == "Кумисбаев Б.")
    fact = next(line for line in report.payroll_fact if line.name == "Кумисбаев Б.")

    assert plan.bonus == 700_000
    assert fact.bonus == 537_900


def test_roles_are_kept(grid) -> None:
    roles = {line.name: line.role for line in parse_sales_report(grid).payroll_fact}
    assert roles == {"Кумисбаев Б.": "РОП", "Усман": "МОП", "Дамир": "Таргет"}


def test_payroll_stops_at_the_total_row(grid) -> None:
    assert all(line.name != "ИТОГО" for line in parse_sales_report(grid).payroll_fact)


# ── Расходы ──────────────────────────────────────────────────────────────────────


def test_contractors_are_read(grid) -> None:
    contractors = parse_sales_report(grid).contractors

    assert len(contractors) == 1
    assert contractors[0].name == "ИП We Make"
    assert contractors[0].channel == "Контекст"
    assert contractors[0].fact == 770_000


def test_ad_budget_is_read(grid) -> None:
    budget = {line.name: line.fact for line in parse_sales_report(grid).ad_budget}
    assert budget == {"Таргет": 650_000, "Контекс": 1_040_000}


def test_grand_total_expense_is_read(grid) -> None:
    report = parse_sales_report(grid)

    assert report.expense_plan == pytest.approx(5_708_705.8)
    assert report.expense_fact == pytest.approx(5_360_763.9)


def test_margin_is_revenue_minus_expense(grid) -> None:
    report = parse_sales_report(grid)
    assert report.margin_fact == pytest.approx(5_379_000 - 5_360_763.9)


# ── Устойчивость разбора ─────────────────────────────────────────────────────────


def test_parsing_survives_an_inserted_row(grid) -> None:
    """Лист ведут руками: вставка строки не должна ломать разбор."""
    shifted = [*grid[:11], cell({}), *grid[11:]]
    report = parse_sales_report(shifted)

    assert report.contractors and report.contractors[0].name == "ИП We Make"
    assert report.expense_fact == pytest.approx(5_360_763.9)


def test_empty_grid_yields_an_empty_report() -> None:
    report = parse_sales_report([])

    assert report.revenue_plan == 0
    assert report.payroll_fact == []


# ── Каналы ───────────────────────────────────────────────────────────────────────


def test_channel_spelling_is_normalised() -> None:
    assert canonical_channel("Контекс") == canonical_channel("Контекст") == "Контекст"


def test_channel_roi_combines_spend_and_revenue(grid) -> None:
    report = parse_sales_report(grid)
    sales = [
        {"source": "Контекст", "amount": 4_187_000},
        {"source": "Таргет", "amount": 2_982_000},
    ]
    results = {item.channel: item for item in channel_results(report, sales)}

    # Контекст: реклама 1 040 000 + подрядчик 770 000 = 1 810 000.
    assert results["Контекст"].spend == 1_810_000
    assert results["Контекст"].roi == pytest.approx(4_187_000 / 1_810_000)
    assert results["Таргет"].roi == pytest.approx(2_982_000 / 650_000)


def test_channel_without_spend_has_no_roi(grid) -> None:
    results = {
        item.channel: item
        for item in channel_results(parse_sales_report(grid), [{"source": "Без метки", "amount": 50_000}])
    }

    assert results["Без метки"].revenue == 50_000
    assert results["Без метки"].roi is None


def test_channels_are_sorted_by_revenue(grid) -> None:
    sales = [
        {"source": "Таргет", "amount": 3_000_000},
        {"source": "Контекст", "amount": 1_000_000},
    ]
    channels = channel_results(parse_sales_report(grid), sales)
    assert channels[0].channel == "Таргет"


def test_deals_are_counted_per_channel(grid) -> None:
    sales = [{"source": "Таргет", "amount": 100} for _ in range(4)]
    results = {item.channel: item for item in channel_results(parse_sales_report(grid), sales)}
    assert results["Таргет"].deals == 4


# ── Реестр продаж ────────────────────────────────────────────────────────────────


def test_registry_rows_are_parsed() -> None:
    grid = [
        cell({0: "Мес", 5: "Клиент"}),
        cell({0: "7", 2: "01.07.2026", 3: "Акежан", 4: "ТОО BBC LEGAL SUPPORT",
              5: "ImpExtrans", 6: "№ЮО/103", 7: "Юр. разовое", 9: "75 000",
              10: "75 000", 13: "Контекст"}),
    ]
    rows = parse_sales_registry(grid)

    assert len(rows) == 1
    assert rows[0]["client"] == "ImpExtrans"
    assert rows[0]["amount"] == 75_000
    assert rows[0]["source"] == "Контекст"
    assert rows[0]["mop"] == "Акежан"


def test_repeated_header_rows_are_skipped() -> None:
    """Лист содержит повторяющиеся шапки — они не должны стать «продажами»."""
    grid = [
        cell({5: "Клиент"}),
        cell({3: "МОП", 5: "Клиент"}),
        cell({0: "7", 3: "Усман", 5: "ТОО Реальный", 9: "50 000"}),
    ]
    rows = parse_sales_registry(grid)

    assert len(rows) == 1
    assert rows[0]["client"] == "ТОО Реальный"
