"""Anomaly detection for the «Предупреждения» block.

The dashboard sits on a living spreadsheet, so its job is not only to report
numbers but to point at the places where the data contradicts itself. Each rule
below corresponds to a class of mistake actually present in the source.
"""
from __future__ import annotations

from datetime import date

from app.bbc.dataset import ONE_OFF, SUBSCRIPTION, ContractRow, Payment
from app.bbc.validators import CRITICAL, IMPORTANT, INFO, analyze, summarize


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
        invoice_date=date(2026, 5, 30),
        paid=False,
        payments=[],
        avr_signed=False,
        avr_date=None,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        signed_at=date(2024, 2, 1),
    )
    base.update(overrides)
    return ContractRow(**base)


def codes(rows: list[ContractRow]) -> set[str]:
    return {finding.code for finding in analyze(rows)}


def find(rows: list[ContractRow], code: str):
    return next(finding for finding in analyze(rows) if finding.code == code)


# ── A clean row raises nothing ───────────────────────────────────────────────────


def test_a_well_formed_row_produces_no_findings() -> None:
    assert analyze([make_row()]) == []


# ── Critical: the row cannot be placed in time ───────────────────────────────────


def test_missing_period_start_is_critical() -> None:
    finding = find([make_row(period_start=None)], "no_period_start")

    assert finding.severity == CRITICAL
    assert finding.count == 1
    assert finding.amount == 500_000.0
    assert finding.rows == [2]


def test_inverted_period_is_critical() -> None:
    rows = [make_row(period_start=date(2026, 6, 30), period_end=date(2026, 6, 1))]
    assert find(rows, "inverted_period").severity == CRITICAL


def test_amount_without_client_or_contract_is_critical() -> None:
    rows = [make_row(client="", contract_no="", period_start=None)]
    assert find(rows, "orphan_row").severity == CRITICAL


def test_row_without_an_amount_is_not_reported_as_missing_period() -> None:
    """No money at stake means no placement problem to flag."""
    assert "no_period_start" not in codes([make_row(contract_amount=None, period_start=None)])


# ── Important: figures compute but contradict each other ─────────────────────────


def test_one_off_without_completion_date_is_flagged() -> None:
    rows = [make_row(service_kind=ONE_OFF, period_end=None)]
    finding = find(rows, "one_off_without_end")

    assert finding.severity == IMPORTANT
    assert "Период по" in finding.hint


def test_subscription_without_end_date_is_not_a_one_off_finding() -> None:
    assert "one_off_without_end" not in codes([make_row(period_end=None)])


def test_missing_department_is_flagged_as_an_access_problem() -> None:
    finding = find([make_row(departments=())], "no_department")

    assert finding.severity == IMPORTANT
    assert "руководитель" in finding.detail


def test_duplicate_contract_and_month_is_flagged() -> None:
    rows = [make_row(index=10, contract_no="№7"), make_row(index=11, contract_no="№7")]
    finding = find(rows, "duplicate_contract_month")

    assert finding.count == 2
    assert finding.rows == [10, 11]


def test_same_contract_in_different_months_is_normal() -> None:
    rows = [make_row(index=10, month=6), make_row(index=11, month=7)]
    assert "duplicate_contract_month" not in codes(rows)


def test_payment_without_invoice_is_flagged() -> None:
    assert "paid_without_invoice" in codes([make_row(paid=True, invoiced=False)])


def test_signed_act_without_invoice_is_flagged() -> None:
    assert "avr_without_invoice" in codes([make_row(avr_signed=True, invoiced=False)])


def test_overpayment_is_flagged() -> None:
    rows = [make_row(contract_amount=100_000.0, paid_amount=150_000.0)]
    assert "overpaid" in codes(rows)


def test_payment_equal_to_the_contract_is_not_an_overpayment() -> None:
    rows = [make_row(contract_amount=100_000.0, paid_amount=100_000.0)]
    assert "overpaid" not in codes(rows)


def test_rounding_noise_does_not_trigger_overpayment() -> None:
    """A one-percent tolerance keeps kopeck-level noise out of the block."""
    rows = [make_row(contract_amount=100_000.0, paid_amount=100_500.0)]
    assert "overpaid" not in codes(rows)


def test_act_larger_than_the_contract_is_flagged() -> None:
    rows = [make_row(contract_amount=100_000.0, avr_amount=200_000.0)]
    assert "avr_exceeds_contract" in codes(rows)


def test_payment_parts_that_do_not_add_up_are_flagged() -> None:
    rows = [
        make_row(
            paid_amount=300_000.0,
            payments=[Payment(date(2026, 6, 5), 100_000.0), Payment(date(2026, 6, 20), 50_000.0)],
        )
    ]
    assert "payment_parts_mismatch" in codes(rows)


def test_payment_parts_that_add_up_are_silent() -> None:
    rows = [
        make_row(
            paid_amount=150_000.0,
            payments=[Payment(date(2026, 6, 5), 100_000.0), Payment(date(2026, 6, 20), 50_000.0)],
        )
    ]
    assert "payment_parts_mismatch" not in codes(rows)


def test_act_signed_before_the_service_started_is_flagged() -> None:
    rows = [make_row(avr_date=date(2026, 5, 1), period_start=date(2026, 6, 1))]
    assert "avr_before_period" in codes(rows)


# ── Info ─────────────────────────────────────────────────────────────────────────


def test_payment_before_invoice_is_informational() -> None:
    rows = [make_row(invoice_date=date(2026, 6, 10), payments=[Payment(date(2026, 6, 1), 1.0)])]
    assert find(rows, "payment_before_invoice").severity == INFO


def test_missing_amount_is_informational() -> None:
    assert find([make_row(contract_amount=None)], "missing_amount").severity == INFO


def test_missing_employee_is_informational() -> None:
    assert find([make_row(employee="")], "no_employee").severity == INFO


# ── Ordering and summary ─────────────────────────────────────────────────────────


def test_findings_are_ordered_by_severity_then_money() -> None:
    rows = [
        make_row(index=2, employee=""),  # info
        make_row(index=3, period_start=None, contract_amount=9_000_000.0),  # critical
        make_row(index=4, paid=True, invoiced=False),  # important
    ]
    severities = [finding.severity for finding in analyze(rows)]

    assert severities.index(CRITICAL) < severities.index(IMPORTANT) < severities.index(INFO)


def test_summary_counts_by_severity_and_money_at_risk() -> None:
    rows = [
        make_row(index=2, period_start=None, contract_amount=1_000_000.0),
        make_row(index=3, departments=()),
    ]
    summary = summarize(analyze(rows))

    assert summary["critical"] == 1
    assert summary["important"] >= 1
    assert summary["amount_at_risk"] == 1_000_000.0
    assert summary["rows_affected"] == 2


def test_summary_of_clean_data_is_empty() -> None:
    summary = summarize(analyze([make_row()]))

    assert summary["total"] == 0
    assert summary["amount_at_risk"] == 0


def test_finding_serialises_with_row_numbers_and_hint() -> None:
    payload = find([make_row(period_start=None)], "no_period_start").to_dict()

    assert payload["rows"] == [2]
    assert payload["truncated"] is False
    assert payload["hint"]
    assert payload["severity"] == CRITICAL


def test_long_row_lists_are_truncated_for_the_wire() -> None:
    rows = [make_row(index=i, period_start=None) for i in range(2, 120)]
    payload = find(rows, "no_period_start").to_dict()

    assert payload["count"] == 118
    assert len(payload["rows"]) == 50
    assert payload["truncated"] is True
