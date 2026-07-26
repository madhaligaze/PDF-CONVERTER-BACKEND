"""Блок «Отдел продаж» — разбор таблицы «расчет плана ОМиП».

Лист устроен зеркально: слева план (колонки B–H), справа факт (M–S), причём
секции идут одинаково — ФОТ, подрядчики, бюджет на рекламу, итог расходов.
Выручка лежит отдельно: J/K — план и факт по отделу, ниже в K — по каждому МОП.

Разбор привязан к подписям («ФОТ», «Подрядчики», «Бюджет на рекламу», «ИТОГО»),
а не к номерам строк: лист ведут руками, и строки в нём появляются.

Эффективность каналов считается сопоставлением расхода на канал из этого листа
с выручкой по полю «Источник» из общего реестра продаж.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from app.bbc.normalize import clean, parse_money

log = logging.getLogger(__name__)

# Секции листа и колонки, с которых начинаются план и факт.
PLAN_COL = 1  # B
FACT_COL = 12  # M
# Смещения внутри секции относительно её первой колонки.
ROLE, FIXED, BONUS, NET, TAXES, TOTAL = 1, 2, 3, 4, 5, 6

PAYROLL_HEADER = "ФОТ"
CONTRACTORS_HEADER = "Подрядчики"
AD_HEADER = "Бюджет на рекламу"
TOTAL_LABEL = "ИТОГО"
GRAND_TOTAL_LABEL = "ИТОГО РАСХОДЫ"


@dataclass
class PayrollLine:
    """Одна строка ФОТ: сотрудник, роль и разложенная выплата."""

    name: str
    role: str
    fixed: float = 0.0
    bonus: float = 0.0
    net: float = 0.0
    taxes: float = 0.0
    total: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SpendLine:
    """Подрядчик или рекламный канал."""

    name: str
    channel: str = ""
    plan: float = 0.0
    fact: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChannelResult:
    """Отдача на маркетинговый канал."""

    channel: str
    spend: float = 0.0
    revenue: float = 0.0
    deals: int = 0

    @property
    def roi(self) -> float | None:
        """Сколько тенге выручки на тенге расхода. None, если расхода нет."""
        return (self.revenue / self.spend) if self.spend else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "spend": round(self.spend, 2),
            "revenue": round(self.revenue, 2),
            "deals": self.deals,
            "roi": round(self.roi, 2) if self.roi is not None else None,
        }


@dataclass
class SalesReport:
    worksheet: str = ""
    revenue_plan: float = 0.0
    revenue_fact: float = 0.0
    per_mop: list[dict[str, Any]] = field(default_factory=list)
    payroll_plan: list[PayrollLine] = field(default_factory=list)
    payroll_fact: list[PayrollLine] = field(default_factory=list)
    contractors: list[SpendLine] = field(default_factory=list)
    ad_budget: list[SpendLine] = field(default_factory=list)
    expense_plan: float = 0.0
    expense_fact: float = 0.0
    channels: list[ChannelResult] = field(default_factory=list)

    @property
    def plan_completion(self) -> float:
        return (self.revenue_fact / self.revenue_plan) if self.revenue_plan else 0.0

    @property
    def margin_fact(self) -> float:
        return self.revenue_fact - self.expense_fact

    def to_dict(self) -> dict[str, Any]:
        return {
            "worksheet": self.worksheet,
            "revenue_plan": round(self.revenue_plan, 2),
            "revenue_fact": round(self.revenue_fact, 2),
            "plan_completion": round(self.plan_completion, 4),
            "per_mop": self.per_mop,
            "payroll_plan": [line.to_dict() for line in self.payroll_plan],
            "payroll_fact": [line.to_dict() for line in self.payroll_fact],
            "contractors": [line.to_dict() for line in self.contractors],
            "ad_budget": [line.to_dict() for line in self.ad_budget],
            "expense_plan": round(self.expense_plan, 2),
            "expense_fact": round(self.expense_fact, 2),
            "margin_fact": round(self.margin_fact, 2),
            "channels": [item.to_dict() for item in self.channels],
        }


def _cell(grid: Sequence[Sequence[str]], row: int, col: int) -> str:
    if row >= len(grid) or col >= len(grid[row]):
        return ""
    return clean(grid[row][col])


def _find_row(grid: Sequence[Sequence[str]], col: int, label: str, start: int = 0) -> int | None:
    """Номер строки, где в колонке `col` стоит подпись `label`."""
    needle = label.casefold()
    for index in range(start, len(grid)):
        if _cell(grid, index, col).casefold() == needle:
            return index
    return None


def _parse_payroll(grid: Sequence[Sequence[str]], base_col: int) -> list[PayrollLine]:
    """Строки ФОТ от заголовка «ФОТ» до «ИТОГО»."""
    header = _find_row(grid, base_col, PAYROLL_HEADER)
    if header is None:
        return []

    lines: list[PayrollLine] = []
    # +2: под заголовком идёт строка с названиями колонок.
    for index in range(header + 2, len(grid)):
        name = _cell(grid, index, base_col)
        if not name:
            continue
        if name.casefold() == TOTAL_LABEL.casefold():
            break
        lines.append(
            PayrollLine(
                name=name,
                role=_cell(grid, index, base_col + ROLE),
                fixed=parse_money(_cell(grid, index, base_col + FIXED)) or 0.0,
                bonus=parse_money(_cell(grid, index, base_col + BONUS)) or 0.0,
                net=parse_money(_cell(grid, index, base_col + NET)) or 0.0,
                taxes=parse_money(_cell(grid, index, base_col + TAXES)) or 0.0,
                total=parse_money(_cell(grid, index, base_col + TOTAL)) or 0.0,
            )
        )
    return lines


def _parse_spend(grid: Sequence[Sequence[str]], header: str) -> list[SpendLine]:
    """Секция расходов (подрядчики / реклама) сразу в двух колонках: план и факт."""
    plan_row = _find_row(grid, PLAN_COL, header)
    fact_row = _find_row(grid, FACT_COL, header)
    if plan_row is None:
        return []

    lines: list[SpendLine] = []
    for offset in range(1, 12):
        index = plan_row + offset
        name = _cell(grid, index, PLAN_COL)
        if not name:
            continue
        if name.casefold() == TOTAL_LABEL.casefold():
            break
        fact_index = (fact_row + offset) if fact_row is not None else index
        lines.append(
            SpendLine(
                name=name,
                channel=_cell(grid, index, PLAN_COL + ROLE),
                plan=parse_money(_cell(grid, index, PLAN_COL + FIXED)) or 0.0,
                fact=parse_money(_cell(grid, fact_index, FACT_COL + FIXED)) or 0.0,
            )
        )
    return lines


def _parse_revenue(grid: Sequence[Sequence[str]]) -> tuple[float, float, list[dict[str, Any]]]:
    """План/факт выручки отдела и разбивка по менеджерам.

    План и факт стоят под подписями «ПЛАН»/«ФАКТ»; ниже в колонке факта чередуются
    имя менеджера и его сумма.
    """
    plan = fact = 0.0
    per_mop: list[dict[str, Any]] = []

    for col in range(len(grid[0]) if grid else 0):
        if _cell(grid, 3, col).casefold() == "план":
            plan = parse_money(_cell(grid, 4, col)) or 0.0
        if _cell(grid, 3, col).casefold() == "факт":
            fact = parse_money(_cell(grid, 4, col)) or 0.0
            # Пары «имя / сумма» идут вниз по той же колонке.
            index = 5
            while index < len(grid) - 1:
                name = _cell(grid, index, col)
                amount = parse_money(_cell(grid, index + 1, col))
                if name and amount is not None and not name[0].isdigit():
                    per_mop.append({"name": name, "revenue": round(amount, 2)})
                    index += 2
                    continue
                if not name and not _cell(grid, index + 1, col):
                    break
                index += 1
    return plan, fact, per_mop


def _grand_total(grid: Sequence[Sequence[str]], base_col: int) -> float:
    row = _find_row(grid, base_col, GRAND_TOTAL_LABEL)
    if row is None:
        return 0.0
    return parse_money(_cell(grid, row, base_col + TOTAL)) or 0.0


def parse_sales_report(grid: Sequence[Sequence[str]], worksheet: str = "") -> SalesReport:
    """Разобрать лист «Отчет <месяц>» таблицы ОМиП."""
    if not grid:
        return SalesReport(worksheet=worksheet)

    plan, fact, per_mop = _parse_revenue(grid)
    return SalesReport(
        worksheet=worksheet,
        revenue_plan=plan,
        revenue_fact=fact,
        per_mop=per_mop,
        payroll_plan=_parse_payroll(grid, PLAN_COL),
        payroll_fact=_parse_payroll(grid, FACT_COL),
        contractors=_parse_spend(grid, CONTRACTORS_HEADER),
        ad_budget=_parse_spend(grid, AD_HEADER),
        expense_plan=_grand_total(grid, PLAN_COL),
        expense_fact=_grand_total(grid, FACT_COL),
    )


# ── Эффективность каналов ────────────────────────────────────────────────────────

# Как канал называется в бюджете и как — в поле «Источник» реестра продаж.
CHANNEL_ALIASES: dict[str, str] = {
    "контекст": "Контекст",
    "контекс": "Контекст",
    "таргет": "Таргет",
    "без метки": "Без метки",
    "мобил": "Мобильный",
}


def canonical_channel(value: str) -> str:
    text = clean(value).casefold()
    return CHANNEL_ALIASES.get(text, clean(value))


def channel_results(
    report: SalesReport,
    sales_rows: Sequence[dict[str, Any]],
) -> list[ChannelResult]:
    """Сопоставить расход на канал с выручкой по этому каналу.

    `sales_rows` — строки общего реестра продаж: {"source": …, "amount": …}.
    Каналы без расхода тоже показываем: сделки по ним есть, просто бесплатные.
    """
    spend: dict[str, float] = {}
    for line in [*report.ad_budget, *report.contractors]:
        channel = canonical_channel(line.channel or line.name)
        spend[channel] = spend.get(channel, 0.0) + (line.fact or line.plan)

    revenue: dict[str, float] = {}
    deals: dict[str, int] = {}
    for row in sales_rows:
        channel = canonical_channel(str(row.get("source", "")))
        if not channel:
            continue
        revenue[channel] = revenue.get(channel, 0.0) + float(row.get("amount") or 0.0)
        deals[channel] = deals.get(channel, 0) + 1

    results = [
        ChannelResult(
            channel=channel,
            spend=spend.get(channel, 0.0),
            revenue=revenue.get(channel, 0.0),
            deals=deals.get(channel, 0),
        )
        for channel in sorted(set(spend) | set(revenue))
    ]
    results.sort(key=lambda item: item.revenue, reverse=True)
    return results


# ── Реестр продаж ────────────────────────────────────────────────────────────────

class SalesCol:
    """Позиции колонок в «Общий Реестр Продаж»."""

    MONTH = 0
    DATE = 2
    MOP = 3
    EXECUTOR = 4
    CLIENT = 5
    CONTRACT = 6
    SERVICE = 7
    TOTAL_AMOUNT = 9
    FACT_AMOUNT = 10
    REMAINDER = 11
    SOURCE = 13


def parse_sales_registry(grid: Sequence[Sequence[str]]) -> list[dict[str, Any]]:
    """Строки реестра продаж, пригодные для аналитики по каналам и менеджерам."""
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(grid[1:], start=2):
        client = _cell([raw], 0, SalesCol.CLIENT)
        mop = _cell([raw], 0, SalesCol.MOP)
        # Лист содержит повторяющиеся строки-заголовки — отбрасываем их.
        if not client or client.casefold() == "клиент" or mop.casefold() == "моп":
            continue
        rows.append(
            {
                "index": index,
                "month": _cell([raw], 0, SalesCol.MONTH),
                "date": _cell([raw], 0, SalesCol.DATE),
                "mop": mop,
                "executor": _cell([raw], 0, SalesCol.EXECUTOR),
                "client": client,
                "contract": _cell([raw], 0, SalesCol.CONTRACT),
                "service": _cell([raw], 0, SalesCol.SERVICE),
                "amount": parse_money(_cell([raw], 0, SalesCol.TOTAL_AMOUNT)) or 0.0,
                "paid": parse_money(_cell([raw], 0, SalesCol.FACT_AMOUNT)) or 0.0,
                "source": _cell([raw], 0, SalesCol.SOURCE),
            }
        )
    return rows


__all__ = [
    "ChannelResult",
    "PayrollLine",
    "SalesCol",
    "SalesReport",
    "SpendLine",
    "canonical_channel",
    "channel_results",
    "parse_sales_registry",
    "parse_sales_report",
]
