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

from app.bbc.layout import Column, Layout, resolve_layout
from app.bbc.normalize import clean, parse_money

log = logging.getLogger(__name__)

PAYROLL_HEADER = "ФОТ"
CONTRACTORS_HEADER = "Подрядчики"
AD_HEADER = "Бюджет на рекламу"
TOTAL_LABEL = "ИТОГО"
GRAND_TOTAL_LABEL = "ИТОГО РАСХОДЫ"

# Денежные колонки панели и то, как они подписаны в строке под «ФОТ».
#
# Раньше здесь стояли числа: PLAN_COL = 1, FACT_COL = 12, а внутри секции
# ФИКС = +2, БОНУС = +3, НАЛОГИ = +5. Лист ведут руками, и вставленная слева
# колонка сдвинула бы всё вправо: на экране остались бы правдоподобные суммы,
# только «Налоги» читались бы из «На руки». Это тот самый тихий случай, из-за
# которого в CLAUDE.md записано правило искать колонки по названиям, — журнал
# и сводку на него уже перевели, лист ОМиП оставался последним.
#
# Роли в списке нет намеренно: у неё в живом листе подписи нет вовсе, найти её
# можно только соседством с именем. Зато она и не деньги — ошибиться в ней
# значит показать «МОП» вместо «РОП», а не 82 039 вместо 250 000.
PAYROLL_MONEY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fixed", ("фикс", "оклад")),
    ("bonus", ("бонус", "премия")),
    ("net", ("на руки",)),
    ("taxes", ("налоги", "налог")),
    ("total", ("всего", "итого")),
)

#: Насколько вправо от начала панели ищутся её колонки. Панели плана и факта
#: стоят на одном листе рядом, и заезжать в соседнюю нельзя: «Фикс» факта не
#: должен найтись при разборе плана.
PANEL_WIDTH = 10


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
    #: Что разобрать не удалось и почему. Пусто — прочиталось всё.
    #: Пустая секция без объяснения читается как «расходов нет», а это другое.
    issues: list[str] = field(default_factory=list)

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
            "issues": list(self.issues),
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


def _find_all(grid: Sequence[Sequence[str]], label: str) -> list[tuple[int, int]]:
    """Все ячейки с такой подписью — (строка, колонка), слева направо и сверху вниз."""
    needle = label.casefold()
    found: list[tuple[int, int]] = []
    for row in range(len(grid)):
        for col in range(len(grid[row])):
            if _cell(grid, row, col).casefold() == needle:
                found.append((row, col))
    return found


@dataclass(frozen=True)
class Panel:
    """Половина листа — план или факт: где начинается и где в ней какие деньги."""

    base_col: int
    header_row: int
    #: Ключ из `PAYROLL_MONEY` → номер колонки в этой панели.
    money: dict[str, int]

    @property
    def role_col(self) -> int:
        """Роль стоит сразу за именем: подписи у неё нет, искать её нечем."""
        return self.base_col + 1


def _resolve_panel(
    grid: Sequence[Sequence[str]], header_row: int, base_col: int, issues: list[str]
) -> Panel | None:
    """Найти денежные колонки панели по подписям в строке под «ФОТ».

    Не нашлось или нашлось дважды — панель не разбирается вовсе. Это правило из
    CLAUDE.md: два похожих заголовка на денежную колонку — отказ читать, а не
    выбор наугад. Пустая секция заметна, а «Налоги», прочитанные из соседней
    колонки, выглядят как налоги.
    """
    titles_row = header_row + 1
    money: dict[str, int] = {}
    for key, names in PAYROLL_MONEY:
        matches = [
            col
            for col in range(base_col, base_col + PANEL_WIDTH)
            if _cell(grid, titles_row, col).casefold() in names
        ]
        if len(matches) != 1:
            issues.append(
                f"Колонка «{names[0]}» в панели ФОТ (столбец {base_col + 1}): "
                + ("не найдена" if not matches else f"найдена {len(matches)} раза")
            )
            log.warning(
                "BBC sales: колонка %r не разрешилась в панели с базой %s (совпадений: %s)",
                names[0],
                base_col,
                len(matches),
            )
            return None
        money[key] = matches[0]
    return Panel(base_col=base_col, header_row=header_row, money=money)


def _panels(grid: Sequence[Sequence[str]], issues: list[str]) -> tuple[Panel | None, Panel | None]:
    """План и факт. Обе панели помечены одной подписью «ФОТ», левая — план."""
    headers = _find_all(grid, PAYROLL_HEADER)
    if not headers:
        issues.append("Не найден заголовок «ФОТ» — лист не похож на отчёт ОМиП")
        return None, None
    resolved = [_resolve_panel(grid, row, col, issues) for row, col in headers[:2]]
    plan = resolved[0] if resolved else None
    fact = resolved[1] if len(resolved) > 1 else None
    return plan, fact


def _parse_payroll(grid: Sequence[Sequence[str]], panel: Panel | None) -> list[PayrollLine]:
    """Строки ФОТ от заголовка «ФОТ» до «ИТОГО»."""
    if panel is None:
        return []

    lines: list[PayrollLine] = []

    def money(index: int, key: str) -> float:
        return parse_money(_cell(grid, index, panel.money[key])) or 0.0

    # +2: под заголовком идёт строка с названиями колонок.
    for index in range(panel.header_row + 2, len(grid)):
        name = _cell(grid, index, panel.base_col)
        if not name:
            continue
        if name.casefold() == TOTAL_LABEL.casefold():
            break
        lines.append(
            PayrollLine(
                name=name,
                role=_cell(grid, index, panel.role_col),
                fixed=money(index, "fixed"),
                bonus=money(index, "bonus"),
                net=money(index, "net"),
                taxes=money(index, "taxes"),
                total=money(index, "total"),
            )
        )
    return lines


def _parse_spend(
    grid: Sequence[Sequence[str]],
    header: str,
    plan: Panel | None,
    fact: Panel | None,
) -> list[SpendLine]:
    """Секция расходов (подрядчики / реклама) сразу в двух колонках: план и факт.

    Своей строки заголовков у этих секций нет — они стоят в той же сетке
    колонок, что и ФОТ, поэтому номера берутся у разобранной панели, а не
    отсчитываются от края листа.
    """
    if plan is None:
        return []
    plan_row = _find_row(grid, plan.base_col, header)
    fact_row = _find_row(grid, fact.base_col, header) if fact is not None else None
    if plan_row is None:
        return []

    lines: list[SpendLine] = []
    for offset in range(1, 12):
        index = plan_row + offset
        name = _cell(grid, index, plan.base_col)
        if not name:
            continue
        if name.casefold() == TOTAL_LABEL.casefold():
            break
        fact_index = (fact_row + offset) if fact_row is not None else index
        fact_col = fact.money["fixed"] if fact is not None else plan.money["fixed"]
        lines.append(
            SpendLine(
                name=name,
                channel=_cell(grid, index, plan.role_col),
                plan=parse_money(_cell(grid, index, plan.money["fixed"])) or 0.0,
                fact=parse_money(_cell(grid, fact_index, fact_col)) or 0.0,
            )
        )
    return lines


def _parse_revenue(grid: Sequence[Sequence[str]]) -> tuple[float, float, list[dict[str, Any]]]:
    """План/факт выручки отдела и разбивка по менеджерам.

    План и факт стоят под подписями «ПЛАН»/«ФАКТ»; ниже в колонке факта чередуются
    имя менеджера и его сумма.

    Подписи ищутся по всему листу, а не в четвёртой строке: раньше номера строк
    были прибиты (`_cell(grid, 3, col)`), и одна вставленная сверху строка
    обнуляла выручку отдела, не сказав об этом ни слова.
    """
    plan = fact = 0.0
    per_mop: list[dict[str, Any]] = []

    plan_at = _find_all(grid, "план")
    if plan_at:
        row, col = plan_at[0]
        plan = parse_money(_cell(grid, row + 1, col)) or 0.0

    fact_at = _find_all(grid, "факт")
    if fact_at:
        row, col = fact_at[0]
        fact = parse_money(_cell(grid, row + 1, col)) or 0.0
        # Пары «имя / сумма» идут вниз по той же колонке.
        index = row + 2
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


def _grand_total(grid: Sequence[Sequence[str]], panel: Panel | None) -> float:
    if panel is None:
        return 0.0
    row = _find_row(grid, panel.base_col, GRAND_TOTAL_LABEL)
    if row is None:
        return 0.0
    return parse_money(_cell(grid, row, panel.money["total"])) or 0.0


def parse_sales_report(grid: Sequence[Sequence[str]], worksheet: str = "") -> SalesReport:
    """Разобрать лист «Отчет <месяц>» таблицы ОМиП."""
    if not grid:
        return SalesReport(worksheet=worksheet)

    issues: list[str] = []
    plan_panel, fact_panel = _panels(grid, issues)
    plan, fact, per_mop = _parse_revenue(grid)
    return SalesReport(
        worksheet=worksheet,
        revenue_plan=plan,
        revenue_fact=fact,
        per_mop=per_mop,
        payroll_plan=_parse_payroll(grid, plan_panel),
        payroll_fact=_parse_payroll(grid, fact_panel),
        contractors=_parse_spend(grid, CONTRACTORS_HEADER, plan_panel, fact_panel),
        ad_budget=_parse_spend(grid, AD_HEADER, plan_panel, fact_panel),
        expense_plan=_grand_total(grid, plan_panel),
        expense_fact=_grand_total(grid, fact_panel),
        issues=issues,
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

REGISTRY_WORKSHEET = "Общий Реестр Продаж"

#: Колонки реестра продаж. Ищутся по заголовку — см. `app.bbc.layout`.
REGISTRY_COLUMNS: tuple[Column, ...] = (
    Column("month", "Мес", ("Мес",), hint=0, required=False),
    Column("date", "Дата", ("Дата",), hint=2, required=False),
    Column("mop", "МОП", ("МОП",), hint=3),
    Column("executor", "Исполнитель (BBC)", ("Исполнитель (BBC)", "Исполнитель"), hint=4, required=False),
    Column("client", "Клиент", ("Клиент",), hint=5),
    Column("contract", "№ Договора", ("№ Договора",), hint=6, required=False),
    Column("service", "Тип Услуги", ("Тип Услуги",), hint=7, required=False),
    Column("total_amount", "Общая Сумма", ("Общая Сумма",), hint=9),
    Column("fact_amount", "Факт Сумма", ("Факт Сумма",), hint=10),
    Column("remainder", "Остаток Сумма", ("Остаток Сумма",), hint=11, required=False),
    Column("source", "Источник", ("Источник",), hint=13, required=False),
)


def resolve_registry_layout(header: Sequence[str]) -> Layout:
    return resolve_layout(REGISTRY_WORKSHEET, REGISTRY_COLUMNS, header)


def parse_sales_registry(grid: Sequence[Sequence[str]]) -> list[dict[str, Any]]:
    """Строки реестра продаж, пригодные для аналитики по каналам и менеджерам."""
    if not grid:
        return []
    layout = resolve_registry_layout(grid[0])

    def cell(raw: Sequence[str], key: str) -> str:
        return clean(layout.cell(raw, key))

    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(grid[1:], start=2):
        client = cell(raw, "client")
        mop = cell(raw, "mop")
        # Лист содержит повторяющиеся строки-заголовки — отбрасываем их.
        if not client or client.casefold() == "клиент" or mop.casefold() == "моп":
            continue
        rows.append(
            {
                "index": index,
                "month": cell(raw, "month"),
                "date": cell(raw, "date"),
                "mop": mop,
                "executor": cell(raw, "executor"),
                "client": client,
                "contract": cell(raw, "contract"),
                "service": cell(raw, "service"),
                "amount": parse_money(cell(raw, "total_amount")) or 0.0,
                "paid": parse_money(cell(raw, "fact_amount")) or 0.0,
                "source": cell(raw, "source"),
            }
        )
    return rows


__all__ = [
    "ChannelResult",
    "PAYROLL_MONEY",
    "Panel",
    "PayrollLine",
    "REGISTRY_COLUMNS",
    "SalesReport",
    "SpendLine",
    "canonical_channel",
    "channel_results",
    "parse_sales_registry",
    "parse_sales_report",
    "resolve_registry_layout",
]
