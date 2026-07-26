"""Two accrual cycles inside a calendar month (spec part 1, §4).

    Цикл 1 — days 01–15, the tail of the subscription cycle that started on the
             16th of the previous month, so it belongs to the *previous* accrual month.
    Цикл 2 — days 16 … end of month (15 or 16 days), belongs to the *next* accrual month.

The calendar month total is simply cycle 1 + cycle 2, which is why the model never
needs restating when new data arrives (§6.4).

Everything here is a pure function of dates and amounts — no sheet, no network —
so the rules can be tested exhaustively.
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date

CYCLE_1 = 1
CYCLE_2 = 2

# Cycle 1 is always days 1..15; cycle 2 takes whatever the month has left.
CYCLE_1_LAST_DAY = 15


def days_in_month(year: int, month: int) -> int:
    return monthrange(year, month)[1]


def cycle_of(value: date) -> int:
    """Which cycle a date falls into. Days 1–15 → cycle 1, 16+ → cycle 2."""
    return CYCLE_1 if value.day <= CYCLE_1_LAST_DAY else CYCLE_2


def cycle_day_counts(year: int, month: int) -> tuple[int, int]:
    """(days in cycle 1, days in cycle 2). 31-day months give (15, 16) per §4."""
    total = days_in_month(year, month)
    return CYCLE_1_LAST_DAY, total - CYCLE_1_LAST_DAY


def cycle_bounds(year: int, month: int, cycle: int) -> tuple[date, date]:
    """Inclusive first/last date of a cycle."""
    if cycle == CYCLE_1:
        return date(year, month, 1), date(year, month, CYCLE_1_LAST_DAY)
    return date(year, month, CYCLE_1_LAST_DAY + 1), date(year, month, days_in_month(year, month))


def accrual_month(value: date) -> tuple[int, int]:
    """The accrual month a date belongs to (§4).

    Cycle 1 (days 1–15) is the tail of the cycle that began on the 16th of the
    previous month, so it accrues to the previous month; cycle 2 accrues to the
    next one. Returns `(year, month)`.
    """
    if cycle_of(value) == CYCLE_1:
        return _shift_month(value.year, value.month, -1)
    return _shift_month(value.year, value.month, +1)


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


@dataclass(frozen=True)
class CycleAmount:
    """An amount landing in one cycle of one calendar month."""

    year: int
    month: int
    cycle: int
    amount: float

    @property
    def key(self) -> tuple[int, int, int]:
        return self.year, self.month, self.cycle


def whole_to_cycle(value: date, amount: float) -> list[CycleAmount]:
    """Put the entire amount into the cycle containing `value`.

    Used for one-off expenses (§4.5) and for the "целиком по дате начала"
    allocation method of subscription revenue.
    """
    return [CycleAmount(value.year, value.month, cycle_of(value), amount)]


def split_calendar_month(year: int, month: int, amount: float) -> list[CycleAmount]:
    """Split a monthly amount across cycles by day count (§4.3, rent).

    `Аренда_Цикл1 = Аренда × 15 / дней_в_месяце`, remainder to cycle 2.
    """
    first_days, second_days = cycle_day_counts(year, month)
    total_days = first_days + second_days
    first = amount * first_days / total_days
    return [
        CycleAmount(year, month, CYCLE_1, first),
        # Subtract rather than recompute so the two parts always re-add exactly.
        CycleAmount(year, month, CYCLE_2, amount - first),
    ]


def split_prorata(start: date, end: date, amount: float) -> list[CycleAmount]:
    """Spread an amount across every cycle the [start, end] period touches.

    Day-proportional, the same idea as §4.3 but for an arbitrary service period —
    this is the "pro-rata по дням" allocation method for revenue.

    Returns a single zero-length entry when the period is inverted or empty, so a
    bad row never silently vanishes from the totals.
    """
    if end < start:
        return [CycleAmount(start.year, start.month, cycle_of(start), amount)]

    buckets = _period_day_counts(start, end)
    total_days = sum(buckets.values())
    if total_days <= 0:  # pragma: no cover — guarded by the `end < start` branch
        return [CycleAmount(start.year, start.month, cycle_of(start), amount)]

    out: list[CycleAmount] = []
    allocated = 0.0
    keys = sorted(buckets)
    for index, key in enumerate(keys):
        year, month, cycle = key
        if index == len(keys) - 1:
            # Last bucket absorbs the rounding so the parts sum to `amount` exactly.
            part = amount - allocated
        else:
            part = amount * buckets[key] / total_days
            allocated += part
        out.append(CycleAmount(year, month, cycle, part))
    return out


def _period_day_counts(start: date, end: date) -> dict[tuple[int, int, int], int]:
    """Days of [start, end] falling into each (year, month, cycle) bucket."""
    counts: dict[tuple[int, int, int], int] = {}
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        for cycle in (CYCLE_1, CYCLE_2):
            cycle_start, cycle_end = cycle_bounds(year, month, cycle)
            overlap_start = max(cycle_start, start)
            overlap_end = min(cycle_end, end)
            days = (overlap_end - overlap_start).days + 1
            if days > 0:
                counts[(year, month, cycle)] = days
        year, month = _shift_month(year, month, +1)
    return counts


def merge_cycle_amounts(items: list[CycleAmount]) -> list[CycleAmount]:
    """Collapse entries sharing a (year, month, cycle) bucket."""
    totals: dict[tuple[int, int, int], float] = {}
    for item in items:
        totals[item.key] = totals.get(item.key, 0.0) + item.amount
    return [
        CycleAmount(year, month, cycle, amount)
        for (year, month, cycle), amount in sorted(totals.items())
    ]


__all__ = [
    "CYCLE_1",
    "CYCLE_2",
    "CycleAmount",
    "accrual_month",
    "cycle_bounds",
    "cycle_day_counts",
    "cycle_of",
    "days_in_month",
    "merge_cycle_amounts",
    "split_calendar_month",
    "split_prorata",
    "whole_to_cycle",
]
