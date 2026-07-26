"""Normalized view of «Сводка все ЮР лица» — one row per contract × month.

The sheet has 48 columns; most are technical or presentational. This module maps
the ones that carry meaning into a typed row, so everything downstream (scope,
recognition, reports) works with values instead of strings.

Column positions are pinned as constants: the sheet's headers contain newlines
and duplicated `.` separators, which makes name-based lookup fragile.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Sequence

from app.bbc.normalize import (
    canonical_firm,
    canonical_service_kind,
    canonical_status,
    clean,
    firm_label,
    parse_bool,
    parse_date,
    parse_money,
)
from app.bbc.scope import parse_departments

log = logging.getLogger(__name__)


class Col:
    """Zero-based column positions in «Сводка все ЮР лица»."""

    MONTH = 1  # Мес (6/7/8)
    PERIOD_LABEL = 2  # Техн. 1 — «ИЮНЬ 2026»
    FIRM = 4  # Наша Фирма
    SERVICE_KIND = 5  # Вид Услуги
    EMPLOYEE = 6  # Наш Сотрудник
    CLIENT = 7  # Заказчик
    INVOICE_DAY = 10  # Число выставления счёта
    CONTRACT_AMOUNT = 11  # Сумма Договора
    DEPARTMENT = 12  # Отдел
    SUBJECT = 13  # Предмет договора
    SIGNED_AT = 14  # Дата заключения договора
    CONTRACT_NO = 15  # № Договора
    PERIOD_START = 17  # Период с (нач.)
    PERIOD_END = 18  # Период по (зав.)
    SALDO_START = 21  # Сальдо Начало
    INVOICED = 23  # Счет
    INVOICE_NO = 25  # № Счета
    INVOICE_DATE = 26  # Дата выставления счёта
    PAID_FLAG = 27  # Факт Оплата
    PAID_AMOUNT = 28  # Сумма Факт Поступ.
    PAY_DATE_1, PAY_AMOUNT_1 = 29, 30
    PAY_DATE_2, PAY_AMOUNT_2 = 31, 32
    PAY_DATE_3, PAY_AMOUNT_3 = 33, 34
    AVR_FLAG = 35  # АВР (Реал.)
    AVR_AMOUNT = 36  # Сумма (Реал.)
    AVR_NO = 37  # № АВР
    AVR_DATE = 38  # Дата (Реал.)
    SALDO_END = 41  # Сальдо Конец
    STATUS = 43  # Статус
    DIFF_AVR_PAID = 45  # Разница (АВР − Факт)
    DEBIT_CREDIT = 46  # Дебет / Кредит


SUBSCRIPTION = "Абонентская плата"
ONE_OFF = "Разовая услуга"
RENT = "Аренда"


@dataclass
class Payment:
    at: date | None
    amount: float | None


@dataclass
class ContractRow:
    """One (contract × month) line, normalized."""

    index: int  # 1-based row number in the sheet, for traceability
    month: int | None
    client: str
    contract_no: str
    subject: str
    firm: str
    firm_name: str
    departments: tuple[str, ...]
    employee: str
    service_kind: str
    status: str

    contract_amount: float | None
    paid_amount: float | None
    avr_amount: float | None
    saldo_start: float | None
    saldo_end: float | None
    diff_avr_paid: float | None

    invoiced: bool | None
    invoice_no: str
    invoice_date: date | None
    paid: bool | None
    payments: list[Payment] = field(default_factory=list)
    avr_signed: bool | None = None
    avr_date: date | None = None
    period_start: date | None = None
    period_end: date | None = None
    signed_at: date | None = None

    # Filled in by recognition.annotate(); mode key → allocation entries.
    recognition: dict[str, Any] = field(default_factory=dict)

    @property
    def is_one_off(self) -> bool:
        return self.service_kind == ONE_OFF

    @property
    def is_subscription(self) -> bool:
        return self.service_kind == SUBSCRIPTION

    @property
    def first_payment_date(self) -> date | None:
        for payment in self.payments:
            if payment.at is not None:
                return payment.at
        return None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["departments"] = list(self.departments)
        for key in ("invoice_date", "avr_date", "period_start", "period_end", "signed_at"):
            value = data.get(key)
            data[key] = value.isoformat() if isinstance(value, date) else None
        data["payments"] = [
            {"at": p.at.isoformat() if p.at else None, "amount": p.amount} for p in self.payments
        ]
        return data


def _cell(row: Sequence[str], index: int) -> str:
    return clean(row[index]) if index < len(row) else ""


def parse_contract_row(index: int, row: Sequence[str]) -> ContractRow:
    firm_code = canonical_firm(_cell(row, Col.FIRM))
    payments = [
        Payment(parse_date(_cell(row, d)), parse_money(_cell(row, a)))
        for d, a in (
            (Col.PAY_DATE_1, Col.PAY_AMOUNT_1),
            (Col.PAY_DATE_2, Col.PAY_AMOUNT_2),
            (Col.PAY_DATE_3, Col.PAY_AMOUNT_3),
        )
    ]
    month_raw = _cell(row, Col.MONTH)

    return ContractRow(
        index=index,
        month=int(month_raw) if month_raw.isdigit() else None,
        client=_cell(row, Col.CLIENT),
        contract_no=_cell(row, Col.CONTRACT_NO),
        subject=_cell(row, Col.SUBJECT),
        firm=firm_code,
        firm_name=firm_label(firm_code),
        departments=parse_departments(_cell(row, Col.DEPARTMENT)),
        employee=_cell(row, Col.EMPLOYEE),
        service_kind=canonical_service_kind(_cell(row, Col.SERVICE_KIND)),
        status=canonical_status(_cell(row, Col.STATUS)),
        contract_amount=parse_money(_cell(row, Col.CONTRACT_AMOUNT)),
        paid_amount=parse_money(_cell(row, Col.PAID_AMOUNT)),
        avr_amount=parse_money(_cell(row, Col.AVR_AMOUNT)),
        saldo_start=parse_money(_cell(row, Col.SALDO_START)),
        saldo_end=parse_money(_cell(row, Col.SALDO_END)),
        diff_avr_paid=parse_money(_cell(row, Col.DIFF_AVR_PAID)),
        invoiced=parse_bool(_cell(row, Col.INVOICED)),
        invoice_no=_cell(row, Col.INVOICE_NO),
        invoice_date=parse_date(_cell(row, Col.INVOICE_DATE)),
        paid=parse_bool(_cell(row, Col.PAID_FLAG)),
        payments=[p for p in payments if p.at is not None or p.amount],
        avr_signed=parse_bool(_cell(row, Col.AVR_FLAG)),
        avr_date=parse_date(_cell(row, Col.AVR_DATE)),
        period_start=parse_date(_cell(row, Col.PERIOD_START)),
        period_end=parse_date(_cell(row, Col.PERIOD_END)),
        signed_at=parse_date(_cell(row, Col.SIGNED_AT)),
    )


def parse_contract_rows(values: list[list[str]]) -> list[ContractRow]:
    """Parse the grid, skipping the header and fully blank lines."""
    rows: list[ContractRow] = []
    for offset, raw in enumerate(values[1:], start=2):
        if not any(clean(cell) for cell in raw):
            continue
        rows.append(parse_contract_row(offset, raw))
    return rows


# ── Dimensions & coverage ────────────────────────────────────────────────────────


def collect_dimensions(rows: list[ContractRow]) -> dict[str, list[str]]:
    """Distinct filter values, so the UI never has to scan the whole dataset."""

    def distinct(getter) -> list[str]:
        return sorted({value for value in map(getter, rows) if value})

    departments: set[str] = set()
    for row in rows:
        departments.update(row.departments)

    return {
        "firms": distinct(lambda r: r.firm),
        "departments": sorted(departments),
        "employees": distinct(lambda r: r.employee),
        "service_kinds": distinct(lambda r: r.service_kind),
        "statuses": distinct(lambda r: r.status),
        "clients": distinct(lambda r: r.client),
        "months": [str(m) for m in sorted({r.month for r in rows if r.month})],
    }


def coverage(rows: list[ContractRow]) -> dict[str, Any]:
    """How complete the source is — surfaced in the UI so numbers are never
    presented as more trustworthy than the data behind them."""
    total = len(rows) or 1

    def share(predicate) -> float:
        return round(sum(1 for row in rows if predicate(row)) / total, 4)

    return {
        "rows": len(rows),
        "with_department": share(lambda r: bool(r.departments)),
        "with_period_start": share(lambda r: r.period_start is not None),
        "with_period_end": share(lambda r: r.period_end is not None),
        "with_contract_amount": share(lambda r: r.contract_amount is not None),
        "invoiced": share(lambda r: r.invoiced is True),
        "avr_signed": share(lambda r: r.avr_signed is True),
        "paid": share(lambda r: r.paid is True),
        # One-off rows still awaiting an end date: they sit in the WIP bucket.
        "one_off_without_end": sum(
            1 for r in rows if r.is_one_off and r.period_start and not r.period_end
        ),
        "unassigned_rows": sum(1 for r in rows if not r.departments),
    }


def content_hash(values: list[list[str]]) -> str:
    """Stable digest of a raw grid — drives the live "did anything change" check."""
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "ONE_OFF",
    "RENT",
    "SUBSCRIPTION",
    "Col",
    "ContractRow",
    "Payment",
    "collect_dimensions",
    "content_hash",
    "coverage",
    "parse_contract_row",
    "parse_contract_rows",
]
