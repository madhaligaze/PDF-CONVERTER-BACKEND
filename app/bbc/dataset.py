"""Normalized view of «Сводка все ЮР лица» — one row per contract × month.

The sheet has ~61 columns; most are technical or presentational. This module maps
the ones that carry meaning into a typed row, so everything downstream (scope,
recognition, reports) works with values instead of strings.

Колонки привязываются по заголовкам, а не по номерам — см. `app.bbc.layout`.
Раньше позиции были прибиты числами, и в августе 2026 книга это сломала: в блок
АВР добавили четыре колонки («АВР (клиент принял)», «Дата АВР (клиент принял)»,
«ЭСФ (отпр.)», «Дата ЭСФ (отпр.)»), всё от «Сальдо Конец» и правее уехало на +4,
и дашборд встал с «таблица не читается». Теперь вставка колонки не значит
ничего: колонка найдётся по имени, а сдвиг попадёт в примечание.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Callable, Sequence

from app.bbc.layout import Column, Layout, LayoutError, resolve_layout
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


#: Книга, под которую написаны колонки ниже. Записана здесь, а не только в
#: BBC_SPREADSHEET_ID: `.env` не в репозитории, и без этой строки узнать, какой
#: раскладке соответствуют `hint`-подсказки, было бы неоткуда.
EXPECTED_SPREADSHEET_ID = "1xEp_QEirE49gREHrSvXwcYJRO1ZTVVzGF4Web43tDvI"
EXPECTED_WORKSHEET = "Сводка все ЮР лица"


#: Колонки листа: логическое имя, подпись для ошибок, написания заголовка и
#: позиция в книге на момент написания парсера.
#:
#: Про `names`: списком, а не строкой, потому что книгу переименовывают.
#: «АВР (Реал.)» стало «АВР (наша)» — оба написания обязаны читаться, иначе
#: старая копия книги (и тесты на ней) разъедутся с продом. Опечатка «договра»
#: в заголовке 14 — не опечатка кода, так в книге.
#:
#: Про `hint`: только подсказка для разбора одинаковых заголовков и источник
#: сообщения «колонка уехала». Требованием не является — колонка ищется по имени.
MASTER_COLUMNS: tuple[Column, ...] = (
    Column("month", "Мес", ("Мес",), hint=2),
    # Техн. 1 — «ИЮНЬ 2026» или «Старые (до Мая/Июня 2026)»
    Column("period_label", "Техн. 1 (период)", ("Техн. 1",), hint=3),
    Column("firm", "Наша Фирма", ("Наша Фирма",), hint=5),
    Column("service_kind", "Вид Услуги", ("Вид Услуги",), hint=6),
    Column("employee", "Наш Сотрудник", ("Наш Сотрудник",), hint=7),
    Column("client", "Заказчик", ("Заказчик (Название Фирмы)", "Заказчик"), hint=8),
    Column(
        "invoice_day",
        "Число выставления счёта",
        ("Число Выставления Счета",),
        hint=11,
        required=False,
    ),
    Column("contract_amount", "Сумма Договора", ("Сумма Договора",), hint=12),
    Column("department", "Отдел", ("Отдел",), hint=13),
    Column("subject", "Предмет договора", ("Предмет договра", "Предмет договора"), hint=14),
    Column("signed_at", "Дата заключения договора", ("Дата заключения Договора",), hint=15),
    Column("contract_no", "№ Договора", ("№ Договора",), hint=16),
    Column("addendum", "Доп. Соглашения", ("Доп Соглашения", "Доп. Соглашения"), hint=17, required=False),
    Column("period_start", "Период с (нач.)", ("Период с (нач.)",), hint=18),
    Column("period_end", "Период по (зав.)", ("Период по (зав.)",), hint=19),
    Column("saldo_start", "Сальдо Начало", ("Сальдо Начало",), hint=22),
    Column("invoiced", "Счет", ("Счет",), hint=24),
    Column("invoice_no", "№ Счета", ("№ Счета",), hint=26),
    Column("invoice_date", "Дата выставления счёта", ("Дата выстав. Счета",), hint=27),
    Column("paid_flag", "Факт Оплата", ("Факт Оплата",), hint=28),
    Column("paid_amount", "Сумма Факт Поступ.", ("Сумма Факт Поступ.",), hint=29),
    Column("pay_date_1", "Дата (часть 1)", ("Дата (часть 1)",), hint=30, required=False),
    Column("pay_amount_1", "Сумма (часть 1)", ("Сумма (часть 1)",), hint=31, required=False),
    Column("pay_date_2", "Дата (часть 2)", ("Дата (часть 2)",), hint=32, required=False),
    Column("pay_amount_2", "Сумма (часть 2)", ("Сумма (часть 2)",), hint=33, required=False),
    Column("pay_date_3", "Дата (часть 3)", ("Дата (часть 3)",), hint=34, required=False),
    Column("pay_amount_3", "Сумма (часть 3)", ("Сумма (часть 3)",), hint=35, required=False),
    Column("avr_flag", "АВР (наша)", ("АВР (наша)", "АВР (Реал.)"), hint=36),
    Column("avr_amount", "Сумма АВР (наша)", ("Сумма (наша)", "Сумма (Реал.)"), hint=37),
    Column("avr_no", "№ АВР", ("№ АВР (наша)", "№ АВР"), hint=38, required=False),
    Column("avr_date", "Дата АВР (наша)", ("Дата (наша)", "Дата (Реал.)"), hint=39),
    # Появились в книге в августе 2026. Необязательные: старая копия книги, на
    # которой стоят тесты и локальные прогоны, их не содержит.
    Column(
        "avr_accepted",
        "АВР (клиент принял)",
        ("АВР (клиент принял)",),
        hint=40,
        required=False,
    ),
    Column("esf_sent", "ЭСФ (отпр.)", ("ЭСФ (отпр.)",), hint=42, required=False),
    Column("saldo_end", "Сальдо Конец", ("Сальдо Конец",), hint=46),
    Column(
        "status",
        "Статус",
        ("Статус (Продл./Приостн./Разв./На исп.)", "Статус"),
        hint=48,
    ),
    Column(
        "diff_avr_paid",
        "Разница (АВР−Факт)",
        ("Разница (АВР-Факт)", "Разница (АВР − Факт)"),
        hint=50,
    ),
    # Долг строки — книга считает его сама, мы только читаем.
    Column("debit_credit", "Дебет / Кредит", ("Дебет / Кредит (в т.ч без АВР)",), hint=51),
)


SUBSCRIPTION = "Абонентская плата"
ONE_OFF = "Разовая услуга"
RENT = "Аренда"

# Период ещё не наступил: книга сама решает, когда долг становится долгом.
NOT_DUE = "ЕЩЕ РАНО"

# «Вид Услуги» = «нет» — договор зафиксирован в реестре, но в силу не вступил:
# услуга не определена, и станет «Разовая» или «Абон.П.», когда появится
# ясность. Долгом такая строка не является. Это не догадка: ни одна такая
# строка не попала во вкладки «(для Рук)», по которым живут отделы.
NOT_IN_FORCE = {"НЕТ", ""}


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
    #: Книга завела эти две колонки в августе 2026. В старой копии книги их нет,
    #: и тогда здесь None — «неизвестно», а не False.
    avr_accepted: bool | None = None
    esf_sent: bool | None = None
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
    #: Договор вступил в силу. У «Вид Услуги» = «нет» — ещё нет, и его сумма
    #: не долг, а зафиксированная в реестре договорённость.
    in_force: bool = True

    # Filled in by recognition.annotate(); mode key → allocation entries.
    recognition: dict[str, Any] = field(default_factory=dict)

    @property
    def total_debt(self) -> float:
        """Долг строки вместе с входящим остатком.

        Договор не в силе — долга нет: сумма по нему зафиксирована, но платить
        по ней пока не за что. См. `parked_debt`, где она никуда не девается.
        """
        if not self.in_force:
            return 0.0
        return (self.debt or 0.0) + (self.carry_in or 0.0)

    @property
    def parked_debt(self) -> float:
        """Сумма подвешенного договора. Не долг, но и не ноль — её видно отдельно."""
        if self.in_force:
            return 0.0
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


def _parse_debt(raw: str) -> tuple[float | None, bool, bool]:
    """Читает «Дебет / Кредит» → (сумма, период не наступил, дефект данных)."""
    if not raw:
        return None, False, False
    if raw.upper().startswith(NOT_DUE):
        return None, True, False
    if is_error(raw):
        return None, False, True
    return parse_money(raw), False, False


def parse_contract_row(index: int, row: Sequence[str], layout: Layout) -> ContractRow:
    """Строка мастер-листа Google → `ContractRow`."""
    return build_contract_row(
        index,
        cell=lambda key: clean(layout.cell(row, key)),
        has=layout.has,
    )


def build_contract_row(
    index: int,
    *,
    cell: Callable[[str], str],
    has: Callable[[str], bool],
) -> ContractRow:
    """Сборка строки договора из значений по логическим именам колонок.

    Откуда берутся значения — не её дело. Из листа Google их достаёт
    `parse_contract_row` через `Layout`, из внутренней книги —
    `app.bbc.books_source` через привязки полей к ролям. Разбор один и тот же,
    и это важнее удобства: дашборд, считающий по двум источникам разными
    правилами, показывал бы два разных ответа на один вопрос, и оба
    выглядели бы одинаково уверенно.
    """

    def flag(key: str) -> bool | None:
        """Флаг колонки, которой может не быть в книге.

        Разница существенная: `parse_bool("")` — это False («не подписан»), а
        отсутствующая колонка — None («книга про это не знает»). Схлопнуть одно
        в другое значило бы отчитаться, что клиент не принял ни одного АВР, на
        книге, которая этого просто не отслеживает.
        """
        return parse_bool(cell(key)) if has(key) else None

    firm_code = canonical_firm(cell("firm"))
    service_raw = cell("service_kind")
    period_label = cell("period_label")
    debt, debt_pending, debt_broken = _parse_debt(cell("debit_credit"))
    saldo_start = parse_money(cell("saldo_start"))
    payments = [
        Payment(parse_date(cell(f"pay_date_{part}")), parse_money(cell(f"pay_amount_{part}")))
        for part in (1, 2, 3)
    ]
    month_raw = cell("month")

    return ContractRow(
        index=index,
        month=int(month_raw) if month_raw.isdigit() else None,
        period_label=period_label,
        client=cell("client"),
        contract_no=cell("contract_no"),
        subject=cell("subject"),
        firm=firm_code,
        firm_name=firm_label(firm_code),
        departments=parse_departments(cell("department")),
        employee=cell("employee"),
        service_kind=canonical_service_kind(service_raw),
        in_force=service_raw.strip().upper() not in NOT_IN_FORCE,
        status=canonical_status(cell("status")),
        contract_amount=parse_money(cell("contract_amount")),
        paid_amount=parse_money(cell("paid_amount")),
        avr_amount=parse_money(cell("avr_amount")),
        saldo_start=saldo_start,
        saldo_end=parse_money(cell("saldo_end")),
        diff_avr_paid=parse_money(cell("diff_avr_paid")),
        invoiced=parse_bool(cell("invoiced")),
        invoice_no=cell("invoice_no"),
        invoice_date=parse_date(cell("invoice_date")),
        paid=parse_bool(cell("paid_flag")),
        payments=[p for p in payments if p.at is not None or p.amount],
        avr_signed=parse_bool(cell("avr_flag")),
        avr_date=parse_date(cell("avr_date")),
        avr_accepted=flag("avr_accepted"),
        esf_sent=flag("esf_sent"),
        period_start=parse_date(cell("period_start")),
        period_end=parse_date(cell("period_end")),
        signed_at=parse_date(cell("signed_at")),
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


def resolve_master_layout(header: Sequence[str]) -> Layout:
    """Привязать колонки мастер-листа к его шапке.

    Сдвиг колонок больше не ошибка: колонка находится по названию, а то, что
    она переехала, попадает в `Layout.drift` и оттуда — в примечание на
    дашборде. Ошибка — только если колонка пропала или стала неоднозначной:
    тогда читать нечего, и молча подставлять соседнюю ячейку нельзя.
    """
    return resolve_layout(EXPECTED_WORKSHEET, MASTER_COLUMNS, header)


def parse_dataset(values: list[list[str]]) -> tuple[list[ContractRow], Layout | None]:
    """Разобрать сетку вместе с раскладкой, по которой её прочитали.

    Раскладка нужна выше по стеку: снапшот показывает в интерфейсе, что книга
    изменилась и какие колонки уехали.
    """
    if not values:
        return [], None
    layout = resolve_master_layout(values[0])

    rows: list[ContractRow] = []
    for offset, raw in enumerate(values[1:], start=2):
        if not any(clean(cell) for cell in raw):
            continue
        rows.append(parse_contract_row(offset, raw, layout))
    assign_carry_in(rows)
    return rows, layout


def parse_contract_rows(values: list[list[str]]) -> list[ContractRow]:
    """Parse the grid, skipping the header and fully blank lines."""
    return parse_dataset(values)[0]


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
        "not_in_force_rows": sum(1 for r in rows if not r.in_force),
    }


def content_hash(values: list[list[str]]) -> str:
    """Stable digest of a raw grid — drives the live "did anything change" check."""
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "NOT_DUE",
    "assign_carry_in",
    "build_contract_row",
    "ONE_OFF",
    "RENT",
    "SUBSCRIPTION",
    "MASTER_COLUMNS",
    "ContractRow",
    "Layout",
    "LayoutError",
    "resolve_master_layout",
    "Payment",
    "collect_dimensions",
    "content_hash",
    "coverage",
    "parse_contract_row",
    "parse_contract_rows",
    "parse_dataset",
]
