"""Normalized view of «Сводка все ЮР лица» — one row per contract × month.

The sheet has 59 columns; most are technical or presentational. This module maps
the ones that carry meaning into a typed row, so everything downstream (scope,
recognition, reports) works with values instead of strings.

Column positions are pinned as constants: the sheet's headers contain newlines
and duplicated `.` separators, which makes name-based lookup fragile. Pinned
positions break silently when the book gains a column, so `verify_layout` checks
the header before anything is parsed.
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
    is_error,
    parse_bool,
    parse_date,
    parse_money,
)
from app.bbc.scope import parse_departments

log = logging.getLogger(__name__)


class Col:
    """Zero-based column positions in «Сводка все ЮР лица»."""

    MONTH = 2  # Мес (6/7/8)
    PERIOD_LABEL = 3  # Техн. 1 — «ИЮНЬ 2026» или «Старые (до Мая/Июня 2026)»
    FIRM = 5  # Наша Фирма
    SERVICE_KIND = 6  # Вид Услуги
    EMPLOYEE = 7  # Наш Сотрудник
    CLIENT = 8  # Заказчик
    INVOICE_DAY = 11  # Число выставления счёта
    CONTRACT_AMOUNT = 12  # Сумма Договора
    DEPARTMENT = 13  # Отдел
    SUBJECT = 14  # Предмет договора
    SIGNED_AT = 15  # Дата заключения договора
    CONTRACT_NO = 16  # № Договора
    ADDENDUM = 17  # Доп. Соглашения
    PERIOD_START = 18  # Период с (нач.)
    PERIOD_END = 19  # Период по (зав.)
    SALDO_START = 22  # Сальдо Начало
    INVOICED = 24  # Счет
    INVOICE_NO = 26  # № Счета
    INVOICE_DATE = 27  # Дата выставления счёта
    PAID_FLAG = 28  # Факт Оплата
    PAID_AMOUNT = 29  # Сумма Факт Поступ.
    PAY_DATE_1, PAY_AMOUNT_1 = 30, 31
    PAY_DATE_2, PAY_AMOUNT_2 = 32, 33
    PAY_DATE_3, PAY_AMOUNT_3 = 34, 35
    AVR_FLAG = 36  # АВР (Реал.)
    AVR_AMOUNT = 37  # Сумма (Реал.)
    AVR_NO = 38  # № АВР
    AVR_DATE = 39  # Дата (Реал.)
    SALDO_END = 42  # Сальдо Конец
    STATUS = 44  # Статус
    DIFF_AVR_PAID = 46  # Разница (АВР − Факт)
    DEBIT_CREDIT = 47  # Дебет / Кредит (в т.ч без АВР) — долг строки


# Заголовки, по которым сверяется раскладка. Ключевое здесь — не полнота, а то,
# что каждая из этих колонок стоит в другом «блоке» листа: если книгу сдвинут на
# колонку, хотя бы одна из проверок обязана не сойтись.
HEADER_ANCHORS: dict[int, str] = {
    Col.MONTH: "Мес",
    Col.CLIENT: "Заказчик",
    Col.CONTRACT_AMOUNT: "Сумма",
    Col.DEPARTMENT: "Отдел",
    Col.CONTRACT_NO: "Договора",
    Col.SALDO_START: "Сальдо",
    Col.AVR_FLAG: "АВР",
    Col.SALDO_END: "Сальдо",
    Col.DEBIT_CREDIT: "Дебет",
}


class LayoutError(RuntimeError):
    """Раскладка листа не совпала с той, под которую написан парсер."""


SUBSCRIPTION = "Абонентская плата"
ONE_OFF = "Разовая услуга"
RENT = "Аренда"

# Период ещё не наступил: книга сама решает, когда долг становится долгом.
NOT_DUE = "ЕЩЕ РАНО"


@dataclass
class Payment:
    at: date | None
    amount: float | None


@dataclass
class ContractRow:
    """One (contract × month) line, normalized."""

    index: int  # 1-based row number in the sheet, for traceability
    month: int | None
    period_label: str  # «ИЮНЬ 2026» / «Старые (до Мая/Июня 2026)»
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

    # ── Долг ────────────────────────────────────────────────────────────────
    # Книга считает долг сама, в колонке «Дебет / Кредит (в т.ч без АВР)»:
    # начальный остаток плюс суммы договоров по наступившим периодам, минус
    # оплаты. Мы её читаем, а не пересчитываем — иначе цифра на дашборде
    # разойдётся с той, по которой отдел живёт.
    debt: float | None = None
    #: Период не наступил — книга пишет «Еще рано». Это не долг, а предстоящее.
    debt_pending: bool = False
    #: В колонке долга формульная ошибка. Строку нельзя ни считать, ни молчать.
    debt_broken: bool = False
    #: Долг, накопленный до учётных периодов. Только у строки «Старые…».
    carry_in: float | None = None

    # Filled in by recognition.annotate(); mode key → allocation entries.
    recognition: dict[str, Any] = field(default_factory=dict)

    @property
    def total_debt(self) -> float:
        """Долг строки вместе с входящим остатком."""
        return (self.debt or 0.0) + (self.carry_in or 0.0)

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


def _parse_debt(raw: str) -> tuple[float | None, bool, bool]:
    """Читает «Дебет / Кредит» → (сумма, период не наступил, дефект данных)."""
    if not raw:
        return None, False, False
    if raw.upper().startswith(NOT_DUE):
        return None, True, False
    if is_error(raw):
        return None, False, True
    return parse_money(raw), False, False


def parse_contract_row(index: int, row: Sequence[str]) -> ContractRow:
    firm_code = canonical_firm(_cell(row, Col.FIRM))
    period_label = _cell(row, Col.PERIOD_LABEL)
    debt, debt_pending, debt_broken = _parse_debt(_cell(row, Col.DEBIT_CREDIT))
    saldo_start = parse_money(_cell(row, Col.SALDO_START))
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
        period_label=period_label,
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
        saldo_start=saldo_start,
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
        debt=debt,
        debt_pending=debt_pending,
        debt_broken=debt_broken,
    )


def _period_order(row: ContractRow) -> tuple:
    """Порядок учётных периодов договора. Строка «Старые…» идёт раньше всех."""
    return (
        row.month if row.month is not None else -1,
        row.period_start or date.min,
        row.index,
    )


def assign_carry_in(rows: list[ContractRow]) -> None:
    """Проставляет входящий остаток — долг, накопленный до учётных периодов.

    «Сальдо Начало» значит разное в разных строках договора. У первой это
    входящий остаток: у ИП ПЕН Н.И. в июне там −66 000, и книга показывает
    начальнику 165 000 = 66 000 долга прошлых периодов плюс 99 000 за три
    месяца. У последующих — текущее сальдо: у ИП Vector в июле там −85 000 при
    долге 85 000, и сложить их значило бы удвоить долг.

    Отличить одно от другого построчно нельзя, поэтому остаток берётся у самой
    ранней строки договора и только у неё.
    """
    contracts: dict[tuple[str, str], list[ContractRow]] = {}
    for row in rows:
        contracts.setdefault((row.client, row.contract_no), []).append(row)

    for group in contracts.values():
        first = min(group, key=_period_order)
        for row in group:
            row.carry_in = None
        if first.saldo_start:
            first.carry_in = abs(first.saldo_start)


def verify_layout(header: Sequence[str]) -> None:
    """Сверяет заголовок с раскладкой, под которую написан `Col`.

    Позиции прибиты числами, поэтому вставленная в книгу колонка сдвигает всё
    правее неё и парсер начинает читать соседние ячейки. Молча: суммы останутся
    похожими на суммы, даты на даты, и дашборд покажет неверные деньги с тем же
    уверенным видом. Поэтому — падаем, а не продолжаем.
    """
    mismatched: list[str] = []
    for position, expected in HEADER_ANCHORS.items():
        actual = clean(header[position]) if position < len(header) else ""
        if expected.upper() not in actual.upper():
            mismatched.append(f"[{position}] ждали «{expected}», нашли «{actual}»")
    if mismatched:
        raise LayoutError(
            "Раскладка листа «Сводка все ЮР лица» не совпала с парсером. "
            "Самая частая причина — BBC_SPREADSHEET_ID указывает на старую книгу: "
            "парсер написан под «Копия Общая сводка BBC» "
            "(1xEp_QEirE49gREHrSvXwcYJRO1ZTVVzGF4Web43tDvI), где все колонки "
            "сдвинуты на одну вправо. Не совпало: " + "; ".join(mismatched)
        )


def parse_contract_rows(values: list[list[str]]) -> list[ContractRow]:
    """Parse the grid, skipping the header and fully blank lines."""
    if not values:
        return []
    verify_layout(values[0])

    rows: list[ContractRow] = []
    for offset, raw in enumerate(values[1:], start=2):
        if not any(clean(cell) for cell in raw):
            continue
        rows.append(parse_contract_row(offset, raw))
    assign_carry_in(rows)
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
        # Долг: сколько строк книга посчитать не смогла и сколько ещё не наступило.
        "with_debt": share(lambda r: r.debt is not None),
        "debt_pending_rows": sum(1 for r in rows if r.debt_pending),
        "debt_broken_rows": sum(1 for r in rows if r.debt_broken),
    }


def content_hash(values: list[list[str]]) -> str:
    """Stable digest of a raw grid — drives the live "did anything change" check."""
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "NOT_DUE",
    "assign_carry_in",
    "ONE_OFF",
    "RENT",
    "SUBSCRIPTION",
    "Col",
    "ContractRow",
    "LayoutError",
    "verify_layout",
    "Payment",
    "collect_dimensions",
    "content_hash",
    "coverage",
    "parse_contract_row",
    "parse_contract_rows",
]
