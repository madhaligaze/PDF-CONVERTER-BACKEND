"""The two-cycle accrual model from spec part 1, §4."""
from __future__ import annotations

from datetime import date

import pytest

from app.bbc.periods import (
    CYCLE_1,
    CYCLE_2,
    CycleAmount,
    accrual_month,
    cycle_bounds,
    cycle_day_counts,
    cycle_of,
    merge_cycle_amounts,
    split_calendar_month,
    split_prorata,
    whole_to_cycle,
)


# ── Cycle boundaries ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("day", [1, 2, 14, 15])
def test_first_half_of_the_month_is_cycle_1(day) -> None:
    assert cycle_of(date(2026, 6, day)) == CYCLE_1


@pytest.mark.parametrize("day", [16, 17, 30])
def test_second_half_of_the_month_is_cycle_2(day) -> None:
    assert cycle_of(date(2026, 6, day)) == CYCLE_2


@pytest.mark.parametrize(
    ("year", "month", "expected"),
    [
        (2026, 1, (15, 16)),  # 31 days → cycle 2 gets 16, per §4
        (2026, 4, (15, 15)),  # 30 days
        (2026, 2, (15, 13)),  # 28 days
        (2028, 2, (15, 14)),  # leap February
    ],
)
def test_cycle_day_counts_follow_the_calendar(year, month, expected) -> None:
    assert cycle_day_counts(year, month) == expected


def test_cycle_bounds_cover_the_whole_month_without_gaps() -> None:
    first_start, first_end = cycle_bounds(2026, 7, CYCLE_1)
    second_start, second_end = cycle_bounds(2026, 7, CYCLE_2)

    assert (first_start, first_end) == (date(2026, 7, 1), date(2026, 7, 15))
    assert (second_start, second_end) == (date(2026, 7, 16), date(2026, 7, 31))
    assert (second_start - first_end).days == 1


# ── Accrual month ────────────────────────────────────────────────────────────────


def test_cycle_1_accrues_to_the_previous_month() -> None:
    """Days 1–15 are the tail of the cycle that began on the 16th before."""
    assert accrual_month(date(2026, 6, 10)) == (2026, 5)


def test_cycle_2_accrues_to_the_next_month() -> None:
    assert accrual_month(date(2026, 6, 20)) == (2026, 7)


def test_accrual_month_rolls_over_the_year_backwards() -> None:
    assert accrual_month(date(2026, 1, 5)) == (2025, 12)


def test_accrual_month_rolls_over_the_year_forwards() -> None:
    assert accrual_month(date(2026, 12, 20)) == (2027, 1)


# ── Whole-amount allocation ──────────────────────────────────────────────────────


def test_whole_amount_lands_in_one_cycle() -> None:
    result = whole_to_cycle(date(2026, 6, 20), 500_000)

    assert result == [CycleAmount(2026, 6, CYCLE_2, 500_000)]


# ── Calendar-month split (rent, §4.3) ────────────────────────────────────────────


def test_rent_split_uses_day_proportions() -> None:
    first, second = split_calendar_month(2026, 6, 300_000)  # 30 days

    assert first.amount == pytest.approx(150_000)
    assert second.amount == pytest.approx(150_000)


def test_rent_split_in_a_31_day_month_favours_cycle_2() -> None:
    first, second = split_calendar_month(2026, 7, 310_000)  # 15 / 16

    assert first.amount == pytest.approx(150_000)
    assert second.amount == pytest.approx(160_000)


@pytest.mark.parametrize("month", [1, 2, 4, 7, 12])
def test_rent_split_always_re_adds_to_the_whole(month) -> None:
    parts = split_calendar_month(2026, month, 1_234_567.89)
    assert sum(part.amount for part in parts) == pytest.approx(1_234_567.89)


# ── Pro-rata across a service period ─────────────────────────────────────────────


def test_full_month_subscription_splits_across_both_cycles() -> None:
    parts = split_prorata(date(2026, 6, 1), date(2026, 6, 30), 500_000)

    assert [(part.month, part.cycle) for part in parts] == [(6, CYCLE_1), (6, CYCLE_2)]
    assert parts[0].amount == pytest.approx(250_000)
    assert parts[1].amount == pytest.approx(250_000)


def test_period_inside_one_cycle_stays_there() -> None:
    parts = split_prorata(date(2026, 6, 2), date(2026, 6, 10), 90_000)

    assert len(parts) == 1
    assert parts[0].cycle == CYCLE_1
    assert parts[0].amount == pytest.approx(90_000)


def test_period_spanning_two_months_hits_four_cycles() -> None:
    parts = split_prorata(date(2026, 6, 1), date(2026, 7, 31), 610_000)

    assert [(part.month, part.cycle) for part in parts] == [
        (6, CYCLE_1),
        (6, CYCLE_2),
        (7, CYCLE_1),
        (7, CYCLE_2),
    ]


def test_prorata_parts_always_re_add_to_the_whole() -> None:
    parts = split_prorata(date(2026, 5, 20), date(2026, 8, 3), 1_000_000 / 3)
    assert sum(part.amount for part in parts) == pytest.approx(1_000_000 / 3)


def test_single_day_period_is_allowed() -> None:
    parts = split_prorata(date(2026, 6, 16), date(2026, 6, 16), 1_000)

    assert len(parts) == 1
    assert parts[0].cycle == CYCLE_2
    assert parts[0].amount == pytest.approx(1_000)


def test_inverted_period_keeps_the_money_visible() -> None:
    """A malformed row must not quietly drop out of the totals."""
    parts = split_prorata(date(2026, 6, 30), date(2026, 6, 1), 700)

    assert sum(part.amount for part in parts) == pytest.approx(700)


def test_prorata_weights_follow_day_counts_not_cycle_count() -> None:
    """16-day cycle 2 must receive more than 15-day cycle 1."""
    first, second = split_prorata(date(2026, 7, 1), date(2026, 7, 31), 31_000)

    assert first.amount == pytest.approx(15_000)
    assert second.amount == pytest.approx(16_000)


# ── Merging ──────────────────────────────────────────────────────────────────────


def test_merge_collapses_matching_buckets() -> None:
    merged = merge_cycle_amounts(
        [
            CycleAmount(2026, 6, CYCLE_1, 100),
            CycleAmount(2026, 6, CYCLE_1, 50),
            CycleAmount(2026, 6, CYCLE_2, 25),
        ]
    )

    assert merged == [
        CycleAmount(2026, 6, CYCLE_1, 150),
        CycleAmount(2026, 6, CYCLE_2, 25),
    ]


def test_merge_returns_chronological_order() -> None:
    merged = merge_cycle_amounts(
        [
            CycleAmount(2026, 7, CYCLE_1, 1),
            CycleAmount(2026, 6, CYCLE_2, 1),
            CycleAmount(2025, 12, CYCLE_1, 1),
        ]
    )

    assert [(item.year, item.month, item.cycle) for item in merged] == [
        (2025, 12, CYCLE_1),
        (2026, 6, CYCLE_2),
        (2026, 7, CYCLE_1),
    ]


def test_month_total_is_the_sum_of_both_cycles() -> None:
    """§4: the calendar column equals cycle 1 + cycle 2, no restatement needed."""
    parts = split_prorata(date(2026, 6, 1), date(2026, 6, 30), 480_000)
    assert sum(part.amount for part in parts) == pytest.approx(480_000)
