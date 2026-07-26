"""Anomaly detection — the «Предупреждения» block.

The source is a living, hand-maintained spreadsheet: it is always partly filled,
always changing, and always subject to human slips and Google Sheets quirks
(formula errors, stray text in numeric columns, values typed several ways). The
dashboard is not just a reader of that data — it is the layer that makes those
problems visible so a person can fix them at the source.

Design rules:

* Every finding names **which sheet rows** are affected and **how much money** is
  involved, so triage is by materiality rather than by row count.
* Nothing is auto-corrected. The service points, the human decides.
* Severity says what the problem does to the numbers:
    `critical`  — the row cannot be placed in time at all, money hides in WIP;
    `important` — the figures still compute but are internally inconsistent;
    `info`      — worth knowing, no effect on totals.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from app.bbc.dataset import ContractRow

CRITICAL = "critical"
IMPORTANT = "important"
INFO = "info"

# Only ever list this many row numbers per finding — the UI drills down for more.
MAX_LISTED_ROWS = 50


@dataclass
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    hint: str
    count: int
    amount: float = 0.0
    rows: list[int] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "hint": self.hint,
            "count": self.count,
            "amount": round(self.amount, 2),
            "rows": self.rows[:MAX_LISTED_ROWS],
            "truncated": len(self.rows) > MAX_LISTED_ROWS,
            "samples": self.samples[:3],
        }


def _finding(
    code: str,
    severity: str,
    title: str,
    detail: str,
    hint: str,
    rows: list[ContractRow],
) -> Finding | None:
    if not rows:
        return None
    return Finding(
        code=code,
        severity=severity,
        title=title,
        detail=detail,
        hint=hint,
        count=len(rows),
        amount=sum(row.contract_amount or 0.0 for row in rows),
        rows=[row.index for row in rows],
        samples=[f"строка {row.index}: {row.client or '—'}" for row in rows[:3]],
    )


# ── Rules ────────────────────────────────────────────────────────────────────────


def _no_period_start(rows: list[ContractRow]) -> Finding | None:
    affected = [row for row in rows if row.contract_amount and not row.period_start]
    return _finding(
        "no_period_start",
        CRITICAL,
        "Нет даты начала периода",
        "Модель признания по периодам не может отнести эти строки ни к одному месяцу, "
        "поэтому их сумма целиком висит в «на исполнении» и не попадает в ОПиУ.",
        "Заполнить «Период с (нач.)» — деньги сразу разойдутся по месяцам и циклам.",
        affected,
    )


def _one_off_without_end(rows: list[ContractRow]) -> Finding | None:
    affected = [
        row for row in rows if row.is_one_off and row.period_start and not row.period_end
    ]
    return _finding(
        "one_off_without_end",
        IMPORTANT,
        "Разовая услуга без даты завершения",
        "В режиме «подвесить до завершения» такие услуги ждут в «на исполнении»: "
        "признать доход не по чему, пока работа не закрыта.",
        "Заполнить «Период по (зав.)» по факту сдачи — сумма распределится по дням работы.",
        affected,
    )


def _inverted_period(rows: list[ContractRow]) -> Finding | None:
    affected = [
        row
        for row in rows
        if row.period_start and row.period_end and row.period_end < row.period_start
    ]
    return _finding(
        "inverted_period",
        CRITICAL,
        "Период задом наперёд",
        "«Период по» раньше «Период с» — распределение по дням посчитать невозможно.",
        "Проверить и поменять даты местами.",
        affected,
    )


def _orphan_row(rows: list[ContractRow]) -> Finding | None:
    affected = [
        row for row in rows if row.contract_amount and not row.client and not row.contract_no
    ]
    return _finding(
        "orphan_row",
        CRITICAL,
        "Сумма без клиента и договора",
        "В строке есть деньги, но не указаны ни заказчик, ни номер договора — "
        "её невозможно отнести ни к кому.",
        "Дозаполнить клиента и договор либо удалить строку, если она техническая.",
        affected,
    )


def _no_department(rows: list[ContractRow]) -> Finding | None:
    affected = [row for row in rows if not row.departments]
    return _finding(
        "no_department",
        IMPORTANT,
        "Не указан отдел",
        "Эти строки не увидит ни один руководитель отдела по своей ссылке — "
        "они доступны только администратору.",
        "Проставить «Отдел» (ОБО / НО / ЮО / HR / ФО).",
        affected,
    )


def _paid_without_invoice(rows: list[ContractRow]) -> Finding | None:
    affected = [row for row in rows if row.paid is True and row.invoiced is False]
    return _finding(
        "paid_without_invoice",
        IMPORTANT,
        "Оплата есть, счёт не выставлен",
        "Деньги получены, но счёт в таблице не отмечен — либо счёт забыли отметить, "
        "либо оплата попала не в ту строку.",
        "Сверить со счётом: проставить «Счет» или перенести оплату.",
        affected,
    )


def _avr_without_invoice(rows: list[ContractRow]) -> Finding | None:
    affected = [row for row in rows if row.avr_signed is True and row.invoiced is False]
    return _finding(
        "avr_without_invoice",
        IMPORTANT,
        "Акт подписан, счёт не выставлен",
        "Работа закрыта документом, а счёта нет — риск, что оплату не запросят.",
        "Выставить счёт или отметить уже выставленный.",
        affected,
    )


def _overpaid(rows: list[ContractRow]) -> Finding | None:
    affected = [
        row
        for row in rows
        if row.contract_amount and row.paid_amount and row.paid_amount > row.contract_amount * 1.01
    ]
    return _finding(
        "overpaid",
        IMPORTANT,
        "Оплачено больше суммы договора",
        "Поступление превышает сумму договора — возможен аванс за будущий период "
        "либо оплата, отнесённая не к той строке.",
        "Проверить назначение платежа и при необходимости разнести на нужный период.",
        affected,
    )


def _avr_exceeds_contract(rows: list[ContractRow]) -> Finding | None:
    affected = [
        row
        for row in rows
        if row.contract_amount and row.avr_amount and row.avr_amount > row.contract_amount * 1.01
    ]
    return _finding(
        "avr_exceeds_contract",
        IMPORTANT,
        "Сумма акта больше суммы договора",
        "По документам закрыто больше, чем предусмотрено договором.",
        "Сверить акт с договором и допсоглашениями.",
        affected,
    )


def _payment_parts_mismatch(rows: list[ContractRow]) -> Finding | None:
    affected = []
    for row in rows:
        parts = [p.amount for p in row.payments if p.amount]
        if not parts or row.paid_amount is None:
            continue
        if abs(sum(parts) - row.paid_amount) > 1:
            affected.append(row)
    return _finding(
        "payment_parts_mismatch",
        IMPORTANT,
        "Части оплаты не сходятся с итогом",
        "Сумма частей 1–3 не равна «Сумма Факт Поступ.» — одна из ячеек устарела.",
        "Пересчитать: либо итог, либо части.",
        affected,
    )


def _avr_before_period(rows: list[ContractRow]) -> Finding | None:
    affected = [
        row
        for row in rows
        if row.avr_date and row.period_start and row.avr_date < row.period_start
    ]
    return _finding(
        "avr_before_period",
        IMPORTANT,
        "Акт подписан раньше начала услуги",
        "Дата акта предшествует началу периода обслуживания — скорее всего опечатка в дате.",
        "Проверить «Дата (Реал.)» и «Период с».",
        affected,
    )


def _payment_before_invoice(rows: list[ContractRow]) -> Finding | None:
    affected = []
    for row in rows:
        first = row.first_payment_date
        if first and row.invoice_date and first < row.invoice_date:
            affected.append(row)
    return _finding(
        "payment_before_invoice",
        INFO,
        "Оплата раньше счёта",
        "Клиент заплатил до выставления счёта — обычно предоплата, но иногда "
        "признак неверной даты.",
        "Убедиться, что дата счёта проставлена верно.",
        affected,
    )


def _duplicate_contract_month(rows: list[ContractRow]) -> Finding | None:
    seen: dict[tuple[str, int | None], list[ContractRow]] = defaultdict(list)
    for row in rows:
        if row.contract_no and row.month:
            seen[(row.contract_no, row.month)].append(row)
    affected = [row for group in seen.values() if len(group) > 1 for row in group]
    return _finding(
        "duplicate_contract_month",
        IMPORTANT,
        "Один договор дважды в одном месяце",
        "Пара «номер договора + месяц» повторяется — выручка может задваиваться.",
        "Проверить, не продублирована ли строка при копировании.",
        affected,
    )


def _missing_amount(rows: list[ContractRow]) -> Finding | None:
    affected = [row for row in rows if row.client and row.contract_amount is None]
    return _finding(
        "missing_amount",
        INFO,
        "Нет суммы договора",
        "У строки есть клиент, но нет суммы — в выручке она не участвует.",
        "Проставить «Сумма Договора» либо убедиться, что строка нужна.",
        affected,
    )


def _no_employee(rows: list[ContractRow]) -> Finding | None:
    affected = [row for row in rows if row.client and not row.employee]
    return _finding(
        "no_employee",
        INFO,
        "Не назначен сотрудник",
        "Строка не попадёт в аналитику по сотрудникам и в расчёт нагрузки.",
        "Указать ответственного в «Наш Сотрудник».",
        affected,
    )


RULES: tuple[Callable[[list[ContractRow]], Finding | None], ...] = (
    _no_period_start,
    _inverted_period,
    _orphan_row,
    _one_off_without_end,
    _no_department,
    _duplicate_contract_month,
    _paid_without_invoice,
    _avr_without_invoice,
    _overpaid,
    _avr_exceeds_contract,
    _payment_parts_mismatch,
    _avr_before_period,
    _payment_before_invoice,
    _missing_amount,
    _no_employee,
)

_SEVERITY_ORDER = {CRITICAL: 0, IMPORTANT: 1, INFO: 2}


def analyze(rows: list[ContractRow]) -> list[Finding]:
    """Run every rule. Most severe first, then by money at stake."""
    findings = [finding for rule in RULES if (finding := rule(rows)) is not None]
    findings.sort(key=lambda f: (_SEVERITY_ORDER[f.severity], -f.amount))
    return findings


def summarize(findings: Iterable[Finding]) -> dict[str, Any]:
    """Counters for the block header and the navigation badge."""
    by_severity = Counter(finding.severity for finding in findings)
    return {
        "total": sum(by_severity.values()),
        "critical": by_severity[CRITICAL],
        "important": by_severity[IMPORTANT],
        "info": by_severity[INFO],
        "rows_affected": len({index for finding in findings for index in finding.rows}),
        "amount_at_risk": round(
            sum(finding.amount for finding in findings if finding.severity == CRITICAL), 2
        ),
    }


__all__ = [
    "CRITICAL",
    "IMPORTANT",
    "INFO",
    "Finding",
    "RULES",
    "analyze",
    "summarize",
]
