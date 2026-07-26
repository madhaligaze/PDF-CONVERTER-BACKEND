"""Платёжный календарь — три версии прогноза (Часть 2, §5).

    Договорной    — план по датам договора: деньги ждём тогда, когда выставлен
                    счёт (или, если счёта ещё нет, к началу периода обслуживания).
    Статистический — та же дата плюс общий медианный лаг «счёт → оплата»,
                    посчитанный по всем прошлым оплатам.
    Предиктивный  — лаг конкретного клиента по его собственной истории; если
                    истории мало, честно откатывается на общую статистику и
                    помечает это в ответе.

Измерено на живых данных: медианный лаг 3 дня по 249 оплатам, но история из
3+ оплат есть лишь у 16 клиентов из 134. Поэтому предиктивная версия честно
сообщает, для скольких строк она реально сработала, а не притворяется точной.

Прогноз строится только по строкам, где счёт выставлен, а денег нет, — плюс,
для договорной версии, по тем, где счёт ещё предстоит выставить.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.bbc.dataset import ContractRow

CONTRACT = "contract"
STATISTICAL = "statistical"
PREDICTIVE = "predictive"
METHODS: tuple[str, ...] = (CONTRACT, STATISTICAL, PREDICTIVE)

METHOD_TITLES: dict[str, str] = {
    CONTRACT: "Договорной",
    STATISTICAL: "Статистический",
    PREDICTIVE: "Предиктивный",
}

METHOD_HINTS: dict[str, str] = {
    CONTRACT: "План по датам договора: ждём оплату в день выставления счёта.",
    STATISTICAL: "Дата счёта плюс общий медианный лаг оплаты по всем клиентам.",
    PREDICTIVE: "Лаг конкретного клиента по его истории; при нехватке данных — общая статистика.",
}

# Личная статистика клиента используется только начиная с этого числа оплат:
# по одной-двум точкам «личный характер» не отличить от случайности.
MIN_HISTORY = 3


@dataclass
class LagStats:
    """Распределение задержки оплаты, в днях."""

    sample: int = 0
    median: float = 0.0
    mean: float = 0.0
    p80: float = 0.0
    worst: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample": self.sample,
            "median": round(self.median, 1),
            "mean": round(self.mean, 1),
            "p80": round(self.p80, 1),
            "worst": round(self.worst, 1),
        }


@dataclass
class Expectation:
    """Одно ожидаемое поступление."""

    row_index: int
    client: str
    departments: list[str]
    amount: float
    expected_at: date
    # Что легло в основу даты: "invoice" (дата счёта) или "period" (начало периода).
    anchor: str
    # Насколько прогнозу можно верить: personal | global | plan
    basis: str
    days_overdue: int = 0
    invoiced: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_index": self.row_index,
            "client": self.client,
            "departments": self.departments,
            "amount": round(self.amount, 2),
            "expected_at": self.expected_at.isoformat(),
            "anchor": self.anchor,
            "basis": self.basis,
            "days_overdue": self.days_overdue,
            "invoiced": self.invoiced,
        }


@dataclass
class Calendar:
    method: str
    title: str
    hint: str
    stats: LagStats
    expectations: list[Expectation] = field(default_factory=list)
    clients_with_history: int = 0
    personal_rows: int = 0
    accuracy: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        overdue = [item for item in self.expectations if item.days_overdue > 0]
        return {
            "method": self.method,
            "title": self.title,
            "hint": self.hint,
            "stats": self.stats.to_dict(),
            "clients_with_history": self.clients_with_history,
            "personal_rows": self.personal_rows,
            "expectations": [item.to_dict() for item in self.expectations],
            "by_day": _by_day(self.expectations),
            "total": round(sum(item.amount for item in self.expectations), 2),
            "overdue_total": round(sum(item.amount for item in overdue), 2),
            "overdue_count": len(overdue),
            "accuracy": self.accuracy,
        }


# ── Статистика лагов ─────────────────────────────────────────────────────────────


def _lag_days(row: ContractRow) -> int | None:
    """Сколько дней прошло от счёта до первой оплаты."""
    paid_at = row.first_payment_date
    if paid_at is None or row.invoice_date is None:
        return None
    return (paid_at - row.invoice_date).days


def payment_lags(rows: list[ContractRow]) -> LagStats:
    """Общая статистика задержки оплаты по всем закрытым строкам."""
    lags = [lag for lag in (_lag_days(row) for row in rows) if lag is not None]
    if not lags:
        return LagStats()
    ordered = sorted(lags)
    return LagStats(
        sample=len(ordered),
        median=statistics.median(ordered),
        mean=statistics.fmean(ordered),
        p80=ordered[min(len(ordered) - 1, int(len(ordered) * 0.8))],
        worst=ordered[-1],
    )


def client_lags(rows: list[ContractRow]) -> dict[str, LagStats]:
    """Личная статистика по каждому клиенту, у кого её достаточно."""
    buckets: dict[str, list[int]] = {}
    for row in rows:
        lag = _lag_days(row)
        if lag is not None and row.client:
            buckets.setdefault(row.client, []).append(lag)

    out: dict[str, LagStats] = {}
    for client, lags in buckets.items():
        if len(lags) < MIN_HISTORY:
            continue
        ordered = sorted(lags)
        out[client] = LagStats(
            sample=len(ordered),
            median=statistics.median(ordered),
            mean=statistics.fmean(ordered),
            p80=ordered[min(len(ordered) - 1, int(len(ordered) * 0.8))],
            worst=ordered[-1],
        )
    return out


# ── Построение календаря ─────────────────────────────────────────────────────────


def _awaiting_payment(row: ContractRow) -> bool:
    """Деньги ещё ждём: сумма есть, оплаты нет."""
    return bool(row.contract_amount) and row.paid is not True


def _anchor_date(row: ContractRow) -> tuple[date, str] | None:
    """От какой даты отсчитывать ожидание."""
    if row.invoice_date is not None:
        return row.invoice_date, "invoice"
    if row.period_start is not None:
        return row.period_start, "period"
    return None


def build_calendar(
    rows: list[ContractRow],
    method: str = PREDICTIVE,
    *,
    today: date | None = None,
) -> Calendar:
    """Собрать календарь ожидаемых поступлений по выбранной версии прогноза."""
    if method not in METHODS:
        method = PREDICTIVE
    today = today or date.today()

    overall = payment_lags(rows)
    personal = client_lags(rows) if method == PREDICTIVE else {}

    expectations: list[Expectation] = []
    personal_rows = 0

    for row in rows:
        if not _awaiting_payment(row):
            continue
        anchor = _anchor_date(row)
        if anchor is None:
            # Ни счёта, ни периода — поставить в календарь не на что.
            continue
        anchor_date, anchor_kind = anchor

        if method == CONTRACT:
            expected, basis = anchor_date, "plan"
        elif method == STATISTICAL:
            expected = anchor_date + timedelta(days=round(overall.median))
            basis = "global"
        else:
            stats = personal.get(row.client)
            if stats is not None:
                expected = anchor_date + timedelta(days=round(stats.median))
                basis = "personal"
                personal_rows += 1
            else:
                expected = anchor_date + timedelta(days=round(overall.median))
                basis = "global"

        expectations.append(
            Expectation(
                row_index=row.index,
                client=row.client or "—",
                departments=list(row.departments),
                amount=row.contract_amount or 0.0,
                expected_at=expected,
                anchor=anchor_kind,
                basis=basis,
                days_overdue=max(0, (today - expected).days),
                invoiced=row.invoiced is True,
            )
        )

    expectations.sort(key=lambda item: item.expected_at)

    return Calendar(
        method=method,
        title=METHOD_TITLES[method],
        hint=METHOD_HINTS[method],
        stats=overall,
        expectations=expectations,
        clients_with_history=len(client_lags(rows)),
        personal_rows=personal_rows,
        accuracy=forecast_accuracy(rows, overall),
    )


def _by_day(expectations: list[Expectation]) -> dict[str, dict[str, Any]]:
    """`{"2026-07-28": {"amount": …, "count": …}}` — сетка календаря."""
    days: dict[str, dict[str, Any]] = {}
    for item in expectations:
        key = item.expected_at.isoformat()
        bucket = days.setdefault(key, {"amount": 0.0, "count": 0, "overdue": 0})
        bucket["amount"] = round(bucket["amount"] + item.amount, 2)
        bucket["count"] += 1
        if item.days_overdue > 0:
            bucket["overdue"] += 1
    return dict(sorted(days.items()))


def forecast_accuracy(rows: list[ContractRow], overall: LagStats) -> list[dict[str, Any]]:
    """Насколько прогноз сбывался в прошлом — план против факта по месяцам.

    Считается только по уже оплаченным строкам: сравниваем, в каком месяце
    статистика предсказала бы оплату, с месяцем, когда деньги пришли на самом
    деле. Без этого блока календарь выглядел бы точнее, чем он есть.
    """
    predicted: dict[str, float] = {}
    actual: dict[str, float] = {}
    hits = 0
    total = 0

    for row in rows:
        paid_at = row.first_payment_date
        if paid_at is None or row.invoice_date is None:
            continue
        amount = row.paid_amount or row.contract_amount or 0.0
        if not amount:
            continue

        expected = row.invoice_date + timedelta(days=round(overall.median))
        predicted_key = f"{expected.year:04d}-{expected.month:02d}"
        actual_key = f"{paid_at.year:04d}-{paid_at.month:02d}"
        predicted[predicted_key] = predicted.get(predicted_key, 0.0) + amount
        actual[actual_key] = actual.get(actual_key, 0.0) + amount
        total += 1
        if predicted_key == actual_key:
            hits += 1

    months = sorted(set(predicted) | set(actual))
    return [
        {
            "month": month,
            "predicted": round(predicted.get(month, 0.0), 2),
            "actual": round(actual.get(month, 0.0), 2),
            "hit_rate": round(hits / total, 3) if total else 0.0,
        }
        for month in months
    ]


__all__ = [
    "CONTRACT",
    "METHODS",
    "METHOD_HINTS",
    "METHOD_TITLES",
    "MIN_HISTORY",
    "PREDICTIVE",
    "STATISTICAL",
    "Calendar",
    "Expectation",
    "LagStats",
    "build_calendar",
    "client_lags",
    "forecast_accuracy",
    "payment_lags",
]
