"""Revenue recognition: the two variants and their allocation switches."""
from __future__ import annotations

from datetime import date

import pytest

from app.bbc.dataset import ContractRow
from app.bbc.recognition import (
    MODES,
    V1_AVRDATE,
    V1_PERIOD_PRORATA_WIP,
    V2_PRORATA_PREPAY,
    V2_PRORATA_WIP,
    V2_START_WIP,
    annotate,
    by_month,
    by_month_cycle,
    describe_mode,
    gap_by_month,
    recognized_total,
    wip_total,
)
from app.bbc.dataset import ONE_OFF, SUBSCRIPTION, Payment


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
    return annotate(ContractRow(**base))


# ── Shape of the annotation ──────────────────────────────────────────────────────


def test_every_mode_is_annotated() -> None:
    row = make_row()
    assert set(row.recognition) == set(MODES)


def test_annotation_is_idempotent() -> None:
    row = make_row()
    before = row.recognition[V2_PRORATA_WIP]
    annotate(row)
    assert row.recognition[V2_PRORATA_WIP] == before


# ── Variant 2: по периодам ───────────────────────────────────────────────────────


def test_subscription_prorata_splits_across_both_cycles() -> None:
    row = make_row()
    assert by_month_cycle([row], V2_PRORATA_WIP)["2026-06"] == {
        "1": 250_000.0,
        "2": 250_000.0,
        "total": 500_000.0,
    }


def test_subscription_by_start_date_lands_in_one_cycle() -> None:
    row = make_row()
    bucket = by_month_cycle([row], V2_START_WIP)["2026-06"]

    assert bucket["1"] == 500_000.0
    assert bucket["2"] == 0.0


def test_cycle_method_changes_distribution_but_not_the_total() -> None:
    """The core invariant: switching allocation never invents or loses money."""
    rows = [
        make_row(period_start=date(2026, 6, 1), period_end=date(2026, 7, 31)),
        make_row(period_start=date(2026, 6, 20), period_end=date(2026, 8, 15)),
    ]

    assert by_month(rows, V2_PRORATA_WIP) != by_month(rows, V2_START_WIP)
    assert recognized_total(rows, V2_PRORATA_WIP) == pytest.approx(
        recognized_total(rows, V2_START_WIP)
    )


def test_subscription_without_a_period_waits_in_wip() -> None:
    """No dates means the period model cannot place it — keep it visible."""
    row = make_row(period_start=None, period_end=None)

    assert recognized_total([row], V2_PRORATA_WIP) == 0
    assert wip_total([row], V2_PRORATA_WIP) == 500_000.0


def test_row_without_an_amount_recognises_nothing() -> None:
    row = make_row(contract_amount=None)

    assert recognized_total([row], V2_PRORATA_WIP) == 0
    assert wip_total([row], V2_PRORATA_WIP) == 0


# ── One-off services ─────────────────────────────────────────────────────────────


def test_one_off_without_completion_date_is_held_in_wip() -> None:
    """«Подвесить до завершения»: no «Период по» yet, so nothing is recognised."""
    row = make_row(
        service_kind=ONE_OFF,
        period_start=date(2026, 7, 2),
        period_end=None,
        payments=[Payment(date(2026, 7, 3), 280_000.0)],
    )

    assert recognized_total([row], V2_PRORATA_WIP) == 0
    assert wip_total([row], V2_PRORATA_WIP) == 500_000.0


def test_one_off_prepay_method_recognises_at_the_payment_month() -> None:
    row = make_row(
        service_kind=ONE_OFF,
        period_start=date(2026, 7, 2),
        period_end=None,
        payments=[Payment(date(2026, 7, 20), 280_000.0)],
    )

    assert wip_total([row], V2_PRORATA_PREPAY) == 0
    assert by_month_cycle([row], V2_PRORATA_PREPAY)["2026-07"]["2"] == 500_000.0


def test_one_off_prepay_falls_back_to_the_start_date_without_payments() -> None:
    row = make_row(service_kind=ONE_OFF, period_start=date(2026, 7, 2), period_end=None)

    assert by_month([row], V2_PRORATA_PREPAY) == {"2026-07": 500_000.0}


def test_one_off_with_completion_date_spreads_over_the_service_period() -> None:
    """Once «Период по» is filled in, the WIP rule starts distributing by days."""
    row = make_row(
        service_kind=ONE_OFF, period_start=date(2026, 7, 1), period_end=date(2026, 7, 31)
    )

    assert wip_total([row], V2_PRORATA_WIP) == 0
    assert recognized_total([row], V2_PRORATA_WIP) == pytest.approx(500_000.0)


def test_one_off_method_does_not_affect_subscriptions() -> None:
    row = make_row()
    assert by_month([row], V2_PRORATA_WIP) == by_month([row], V2_PRORATA_PREPAY)


# ── Variant 1: по документам ─────────────────────────────────────────────────────


def test_unsigned_act_recognises_nothing_in_v1() -> None:
    row = make_row(avr_signed=False, avr_amount=500_000.0)

    assert recognized_total([row], V1_AVRDATE) == 0
    assert recognized_total([row], V1_PERIOD_PRORATA_WIP) == 0


def test_v1_uses_the_act_amount_not_the_contract_amount() -> None:
    row = make_row(
        contract_amount=500_000.0,
        avr_signed=True,
        avr_amount=300_000.0,
        avr_date=date(2026, 6, 30),
    )

    assert recognized_total([row], V1_AVRDATE) == 300_000.0


def test_v1_by_act_date_uses_the_month_of_the_act() -> None:
    """The act was signed in July for a June service period."""
    row = make_row(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        avr_signed=True,
        avr_amount=500_000.0,
        avr_date=date(2026, 7, 17),
    )

    assert by_month([row], V1_AVRDATE) == {"2026-07": 500_000.0}


def test_v1_by_period_uses_the_service_month_instead() -> None:
    """Same row, allocation «по периоду»: the money belongs to June."""
    row = make_row(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        avr_signed=True,
        avr_amount=500_000.0,
        avr_date=date(2026, 7, 17),
    )

    assert by_month([row], V1_PERIOD_PRORATA_WIP) == {"2026-06": 500_000.0}


def test_signed_act_without_a_date_stays_visible_in_wip() -> None:
    """One of the 6 rows lacking a date must not vanish from the totals."""
    row = make_row(avr_signed=True, avr_amount=500_000.0, avr_date=None)

    assert recognized_total([row], V1_AVRDATE) == 0
    assert wip_total([row], V1_AVRDATE) == 500_000.0


# ── Gap ──────────────────────────────────────────────────────────────────────────


def test_gap_reports_earned_documented_and_share() -> None:
    rows = [
        make_row(),  # earned 500 000, no act
        make_row(avr_signed=True, avr_amount=500_000.0, avr_date=date(2026, 7, 5)),
    ]

    gap = gap_by_month(rows, V2_PRORATA_WIP, V1_PERIOD_PRORATA_WIP)["2026-06"]

    assert gap["earned"] == pytest.approx(1_000_000.0)
    assert gap["documented"] == pytest.approx(500_000.0)
    assert gap["gap"] == pytest.approx(500_000.0)
    assert gap["closed_share"] == pytest.approx(0.5)


def test_gap_share_is_zero_when_nothing_was_earned() -> None:
    row = make_row(contract_amount=None, avr_signed=True, avr_amount=100.0, avr_date=date(2026, 6, 5))
    assert gap_by_month([row], V2_PRORATA_WIP, V1_PERIOD_PRORATA_WIP)["2026-06"]["closed_share"] == 0


# ── Descriptions ─────────────────────────────────────────────────────────────────


def test_every_mode_has_a_readable_description() -> None:
    for mode in MODES:
        assert describe_mode(mode)


def test_description_names_the_variant() -> None:
    assert "по периодам услуг" in describe_mode(V2_PRORATA_WIP)
    assert "по подписанным актам" in describe_mode(V1_AVRDATE)
