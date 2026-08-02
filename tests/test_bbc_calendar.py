"""Платёжный календарь: три версии прогноза (Часть 2, §5)."""
from __future__ import annotations

from datetime import date

from app.bbc.calendar import (
    CONTRACT,
    MIN_HISTORY,
    PREDICTIVE,
    STATISTICAL,
    build_calendar,
    client_lags,
    forecast_accuracy,
    payment_lags,
)
from app.bbc.dataset import SUBSCRIPTION, ContractRow, Payment

TODAY = date(2026, 7, 20)


def make_row(**overrides) -> ContractRow:
    base = dict(
        index=2,
        month=6,
        period_label="ИЮНЬ 2026",
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
        invoice_date=date(2026, 7, 1),
        paid=False,
        payments=[],
        avr_signed=False,
        avr_date=None,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        signed_at=date(2024, 2, 1),
    )
    base.update(overrides)
    return ContractRow(**base)


def paid_row(index: int, client: str, invoiced_on: date, paid_on: date, amount=100_000.0):
    """Закрытая строка — из таких считается статистика лагов."""
    return make_row(
        index=index,
        client=client,
        invoice_date=invoiced_on,
        paid=True,
        paid_amount=amount,
        payments=[Payment(paid_on, amount)],
    )


# ── Статистика лагов ─────────────────────────────────────────────────────────────


def test_lag_statistics_are_computed_from_closed_rows() -> None:
    rows = [
        paid_row(2, "А", date(2026, 6, 1), date(2026, 6, 4)),   # 3 дня
        paid_row(3, "Б", date(2026, 6, 1), date(2026, 6, 6)),   # 5 дней
        paid_row(4, "В", date(2026, 6, 1), date(2026, 6, 11)),  # 10 дней
    ]
    stats = payment_lags(rows)

    assert stats.sample == 3
    assert stats.median == 5
    assert stats.worst == 10


def test_rows_without_both_dates_are_ignored_in_statistics() -> None:
    rows = [make_row(), make_row(index=3, invoice_date=None)]
    assert payment_lags(rows).sample == 0


def test_early_payment_gives_a_negative_lag() -> None:
    rows = [paid_row(2, "А", date(2026, 6, 10), date(2026, 6, 4))]
    assert payment_lags(rows).median == -6


# ── Личная история клиента ───────────────────────────────────────────────────────


def test_client_needs_enough_history_to_get_personal_stats() -> None:
    rows = [
        paid_row(2, "Мало", date(2026, 6, 1), date(2026, 6, 3)),
        paid_row(3, "Мало", date(2026, 6, 5), date(2026, 6, 8)),
        *[
            paid_row(10 + i, "Достаточно", date(2026, 6, 1), date(2026, 6, 5))
            for i in range(MIN_HISTORY)
        ],
    ]
    stats = client_lags(rows)

    assert "Достаточно" in stats
    assert "Мало" not in stats


# ── Договорная версия ────────────────────────────────────────────────────────────


def test_contract_version_expects_money_on_the_invoice_date() -> None:
    row = make_row(invoice_date=date(2026, 7, 10))
    calendar = build_calendar([row], CONTRACT, today=TODAY)

    assert len(calendar.expectations) == 1
    assert calendar.expectations[0].expected_at == date(2026, 7, 10)
    assert calendar.expectations[0].basis == "plan"


def test_without_an_invoice_the_period_start_is_used() -> None:
    row = make_row(invoice_date=None, period_start=date(2026, 7, 5))
    expectation = build_calendar([row], CONTRACT, today=TODAY).expectations[0]

    assert expectation.expected_at == date(2026, 7, 5)
    assert expectation.anchor == "period"


def test_row_without_any_date_is_left_out() -> None:
    row = make_row(invoice_date=None, period_start=None)
    assert build_calendar([row], CONTRACT, today=TODAY).expectations == []


# ── Статистическая версия ────────────────────────────────────────────────────────


def test_statistical_version_shifts_by_the_global_median() -> None:
    history = [
        paid_row(10 + i, f"К{i}", date(2026, 6, 1), date(2026, 6, 6)) for i in range(3)
    ]  # медиана 5 дней
    pending = make_row(index=2, invoice_date=date(2026, 7, 10))

    expectation = build_calendar([*history, pending], STATISTICAL, today=TODAY).expectations[0]

    assert expectation.expected_at == date(2026, 7, 15)
    assert expectation.basis == "global"


# ── Предиктивная версия ──────────────────────────────────────────────────────────


def test_predictive_uses_the_clients_own_lag_when_history_allows() -> None:
    history = [
        paid_row(10 + i, "Медленный", date(2026, 6, 1), date(2026, 6, 21))
        for i in range(MIN_HISTORY)
    ]  # личный лаг 20 дней
    others = [paid_row(50 + i, f"Быстрый{i}", date(2026, 6, 1), date(2026, 6, 2)) for i in range(5)]
    pending = make_row(index=2, client="Медленный", invoice_date=date(2026, 7, 1))

    calendar = build_calendar([*history, *others, pending], PREDICTIVE, today=TODAY)
    expectation = next(item for item in calendar.expectations if item.row_index == 2)

    assert expectation.basis == "personal"
    assert expectation.expected_at == date(2026, 7, 21)
    assert calendar.personal_rows == 1


def test_predictive_falls_back_to_global_and_says_so() -> None:
    """Честность: для клиента без истории прогноз помечается как общий."""
    history = [paid_row(10 + i, f"К{i}", date(2026, 6, 1), date(2026, 6, 4)) for i in range(3)]
    pending = make_row(index=2, client="Новичок", invoice_date=date(2026, 7, 10))

    calendar = build_calendar([*history, pending], PREDICTIVE, today=TODAY)
    expectation = next(item for item in calendar.expectations if item.row_index == 2)

    assert expectation.basis == "global"
    assert calendar.personal_rows == 0


def test_unknown_method_degrades_to_predictive() -> None:
    assert build_calendar([make_row()], "нет такого", today=TODAY).method == PREDICTIVE


# ── Что попадает в календарь ─────────────────────────────────────────────────────


def test_paid_rows_are_not_expected_again() -> None:
    rows = [paid_row(2, "А", date(2026, 6, 1), date(2026, 6, 4))]
    assert build_calendar(rows, CONTRACT, today=TODAY).expectations == []


def test_row_without_an_amount_is_not_expected() -> None:
    row = make_row(contract_amount=None)
    assert build_calendar([row], CONTRACT, today=TODAY).expectations == []


def test_overdue_days_are_counted_from_today() -> None:
    row = make_row(invoice_date=date(2026, 7, 1))
    expectation = build_calendar([row], CONTRACT, today=TODAY).expectations[0]

    assert expectation.days_overdue == 19


def test_future_expectation_is_not_overdue() -> None:
    row = make_row(invoice_date=date(2026, 8, 1))
    assert build_calendar([row], CONTRACT, today=TODAY).expectations[0].days_overdue == 0


def test_expectations_come_back_in_date_order() -> None:
    rows = [
        make_row(index=2, invoice_date=date(2026, 8, 10)),
        make_row(index=3, invoice_date=date(2026, 7, 5)),
        make_row(index=4, invoice_date=date(2026, 7, 25)),
    ]
    dates = [item.expected_at for item in build_calendar(rows, CONTRACT, today=TODAY).expectations]

    assert dates == sorted(dates)


# ── Сериализация ─────────────────────────────────────────────────────────────────


def test_payload_groups_money_by_day() -> None:
    rows = [
        make_row(index=2, invoice_date=date(2026, 8, 3), contract_amount=100_000.0),
        make_row(index=3, invoice_date=date(2026, 8, 3), contract_amount=50_000.0),
        make_row(index=4, invoice_date=date(2026, 8, 5), contract_amount=70_000.0),
    ]
    payload = build_calendar(rows, CONTRACT, today=TODAY).to_dict()

    assert payload["by_day"]["2026-08-03"] == {"amount": 150_000.0, "count": 2, "overdue": 0}
    assert payload["total"] == 220_000.0


def test_payload_separates_overdue_money() -> None:
    rows = [
        make_row(index=2, invoice_date=date(2026, 6, 1), contract_amount=300_000.0),
        make_row(index=3, invoice_date=date(2026, 8, 1), contract_amount=100_000.0),
    ]
    payload = build_calendar(rows, CONTRACT, today=TODAY).to_dict()

    assert payload["overdue_count"] == 1
    assert payload["overdue_total"] == 300_000.0


# ── План против факта ────────────────────────────────────────────────────────────


def test_accuracy_compares_predicted_month_with_the_actual_one() -> None:
    rows = [
        paid_row(2, "А", date(2026, 6, 1), date(2026, 6, 4)),
        paid_row(3, "Б", date(2026, 6, 28), date(2026, 7, 15)),  # уехало в другой месяц
    ]
    accuracy = forecast_accuracy(rows, payment_lags(rows))

    assert accuracy
    assert {item["month"] for item in accuracy} >= {"2026-06", "2026-07"}
    assert 0.0 <= accuracy[0]["hit_rate"] <= 1.0


def test_accuracy_is_empty_without_closed_rows() -> None:
    assert forecast_accuracy([make_row()], payment_lags([make_row()])) == []
