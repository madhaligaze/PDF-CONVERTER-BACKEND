"""Revenue recognition — the two report variants and their allocation methods.

Four switches, nine meaningful combinations:

    1. Variant        — по периодам (V2) · по документам (V1)
    2. Cycle method   — pro-rata по дням · целиком по дате начала
    3. One-off method — по месяцу предоплаты · WIP до завершения
    4. V1 allocation  — по периоду услуги · по дате акта

V2 is the canonical model of the spec (§4.1: the service period start decides the
cycle). V1 is the documentary layer on top: same shapes, but only rows with a
signed АВР count, and the base amount is `Сумма (Реал.)`.

Switches 2 and 3 do not apply to `v1:avrdate` — an act is a point event, so there
is nothing to spread. That is why there are 9 modes rather than 16.

All of the accounting lives here, in Python. Each row is annotated with the
already-computed allocation for **every** mode, so the frontend only ever sums
numbers — the rules are never reimplemented in TypeScript.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.bbc.dataset import ContractRow
from app.bbc.periods import CycleAmount, split_prorata, whole_to_cycle

# Mode keys, exactly as the frontend requests them.
V2_PRORATA_PREPAY = "v2:prorata:prepay"
V2_PRORATA_WIP = "v2:prorata:wip"
V2_START_PREPAY = "v2:start:prepay"
V2_START_WIP = "v2:start:wip"
V1_PERIOD_PRORATA_PREPAY = "v1:period:prorata:prepay"
V1_PERIOD_PRORATA_WIP = "v1:period:prorata:wip"
V1_PERIOD_START_PREPAY = "v1:period:start:prepay"
V1_PERIOD_START_WIP = "v1:period:start:wip"
V1_AVRDATE = "v1:avrdate"

MODES: tuple[str, ...] = (
    V2_PRORATA_PREPAY,
    V2_PRORATA_WIP,
    V2_START_PREPAY,
    V2_START_WIP,
    V1_PERIOD_PRORATA_PREPAY,
    V1_PERIOD_PRORATA_WIP,
    V1_PERIOD_START_PREPAY,
    V1_PERIOD_START_WIP,
    V1_AVRDATE,
)

DEFAULT_MODE = V2_PRORATA_WIP

# Presets shown in the UI so nobody has to reason about four switches at once.
PRESETS: dict[str, dict[str, str]] = {
    "management": {"mode": V2_PRORATA_WIP, "title": "Управленческий"},
    "documentary": {"mode": V1_PERIOD_PRORATA_WIP, "title": "Документарный"},
    "accounting_rhythm": {"mode": V1_AVRDATE, "title": "Бухгалтерский ритм"},
}


def describe_mode(mode: str) -> str:
    """Plain-Russian description of a mode, shown permanently in the header."""
    if mode == V1_AVRDATE:
        return "Доход: по подписанным актам · Период: по дате акта"
    variant = "по периодам услуг" if mode.startswith("v2:") else "по документам (АВР)"
    cycles = "пропорционально дням" if ":prorata:" in mode else "целиком по дате начала"
    one_off = "по месяцу предоплаты" if mode.endswith(":prepay") else "подвешены до завершения"
    return f"Доход: {variant} · Циклы: {cycles} · Разовые: {one_off}"


def _encode(items: list[CycleAmount]) -> list[list[float]]:
    """Compact wire form: [year, month, cycle, amount]."""
    return [[item.year, item.month, item.cycle, round(item.amount, 2)] for item in items]


def _allocate(
    row: ContractRow,
    amount: float,
    *,
    prorata: bool,
    prepay_one_off: bool,
) -> tuple[list[CycleAmount], float]:
    """Spread `amount` over cycles. Returns (allocation, wip_amount).

    A non-zero WIP amount means the money is recognised nowhere yet — the service
    is «на исполнении» and has no completion date, so it waits in its own bucket
    instead of being guessed into a month.
    """
    start = row.period_start
    end = row.period_end

    if row.is_one_off:
        if prepay_one_off:
            # Recognised in the month the prepayment landed; fall back to the
            # service start date when the row carries no payment date.
            anchor = row.first_payment_date or start
            return (whole_to_cycle(anchor, amount), 0.0) if anchor else ([], amount)
        # WIP: hold it until a completion date («Период по») appears.
        if start and end:
            return split_prorata(start, end, amount), 0.0
        return [], amount

    if start and end and prorata:
        return split_prorata(start, end, amount), 0.0
    if start:
        return whole_to_cycle(start, amount), 0.0
    # No period at all — the money must stay visible, so park it in WIP rather
    # than dropping it from every total.
    return [], amount


def _by_act_date(row: ContractRow, amount: float) -> tuple[list[CycleAmount], float]:
    anchor: date | None = row.avr_date
    if anchor is None:
        # One of the 6 rows with a signed act but no act date: keep the amount
        # visible in WIP instead of silently discarding it.
        return [], amount
    return whole_to_cycle(anchor, amount), 0.0


def annotate(row: ContractRow) -> ContractRow:
    """Attach the allocation for every mode to a row. Idempotent."""
    v2_base = row.contract_amount or 0.0
    v1_base = (row.avr_amount or 0.0) if row.avr_signed else 0.0

    recognition: dict[str, Any] = {}

    for key, base, prorata, prepay in (
        (V2_PRORATA_PREPAY, v2_base, True, True),
        (V2_PRORATA_WIP, v2_base, True, False),
        (V2_START_PREPAY, v2_base, False, True),
        (V2_START_WIP, v2_base, False, False),
        (V1_PERIOD_PRORATA_PREPAY, v1_base, True, True),
        (V1_PERIOD_PRORATA_WIP, v1_base, True, False),
        (V1_PERIOD_START_PREPAY, v1_base, False, True),
        (V1_PERIOD_START_WIP, v1_base, False, False),
    ):
        if not base:
            recognition[key] = {"alloc": [], "wip": 0.0}
            continue
        allocation, wip = _allocate(row, base, prorata=prorata, prepay_one_off=prepay)
        recognition[key] = {"alloc": _encode(allocation), "wip": round(wip, 2)}

    if v1_base:
        allocation, wip = _by_act_date(row, v1_base)
        recognition[V1_AVRDATE] = {"alloc": _encode(allocation), "wip": round(wip, 2)}
    else:
        recognition[V1_AVRDATE] = {"alloc": [], "wip": 0.0}

    row.recognition = recognition
    return row


def annotate_all(rows: list[ContractRow]) -> list[ContractRow]:
    for row in rows:
        annotate(row)
    return rows


# ── Aggregation ──────────────────────────────────────────────────────────────────


def recognized_total(rows: list[ContractRow], mode: str) -> float:
    """Revenue recognised across the given rows in one mode.

    Always call this on an already scope-filtered list — see `scope.filter_rows`.
    """
    total = 0.0
    for row in rows:
        for _, _, _, amount in row.recognition.get(mode, {}).get("alloc", []):
            total += amount
    return total


def wip_total(rows: list[ContractRow], mode: str) -> float:
    """Money held in «на исполнении» — earned but not yet recognisable."""
    return sum(row.recognition.get(mode, {}).get("wip", 0.0) for row in rows)


def by_month(rows: list[ContractRow], mode: str) -> dict[str, float]:
    """`{"2026-06": amount}` — the calendar column of the report."""
    totals: dict[str, float] = {}
    for row in rows:
        for year, month, _cycle, amount in row.recognition.get(mode, {}).get("alloc", []):
            key = f"{int(year):04d}-{int(month):02d}"
            totals[key] = totals.get(key, 0.0) + amount
    return dict(sorted(totals.items()))


def by_month_cycle(rows: list[ContractRow], mode: str) -> dict[str, dict[str, float]]:
    """`{"2026-06": {"1": …, "2": …, "total": …}}` — ОПиУ's sub-columns."""
    totals: dict[str, dict[str, float]] = {}
    for row in rows:
        for year, month, cycle, amount in row.recognition.get(mode, {}).get("alloc", []):
            key = f"{int(year):04d}-{int(month):02d}"
            bucket = totals.setdefault(key, {"1": 0.0, "2": 0.0, "total": 0.0})
            bucket[str(int(cycle))] += amount
            bucket["total"] += amount
    return dict(sorted(totals.items()))


def gap_by_month(rows: list[ContractRow], v2_mode: str, v1_mode: str) -> dict[str, dict[str, float]]:
    """Earned vs documented, per month — the «Разрыв» block.

    Only meaningful when both sides share a time base, i.e. when V1 is allocated
    «по периоду». With `v1:avrdate` the two sit on different calendars and the
    ratio would be nonsense, so the UI hides the block instead.
    """
    earned = by_month(rows, v2_mode)
    documented = by_month(rows, v1_mode)
    out: dict[str, dict[str, float]] = {}
    for key in sorted(set(earned) | set(documented)):
        got, closed = earned.get(key, 0.0), documented.get(key, 0.0)
        out[key] = {
            "earned": got,
            "documented": closed,
            "gap": got - closed,
            "closed_share": (closed / got) if got else 0.0,
        }
    return out


__all__ = [
    "DEFAULT_MODE",
    "MODES",
    "PRESETS",
    "V1_AVRDATE",
    "V1_PERIOD_PRORATA_PREPAY",
    "V1_PERIOD_PRORATA_WIP",
    "V1_PERIOD_START_PREPAY",
    "V1_PERIOD_START_WIP",
    "V2_PRORATA_PREPAY",
    "V2_PRORATA_WIP",
    "V2_START_PREPAY",
    "V2_START_WIP",
    "annotate",
    "annotate_all",
    "by_month",
    "by_month_cycle",
    "describe_mode",
    "gap_by_month",
    "recognized_total",
    "wip_total",
]
