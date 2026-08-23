"""Блок «Журнал» — операции по счетам и конструктор мини-сводок.

Важно про полноту: в листе 5958 строк, но содержательных — 1390 (есть дата либо
сумма); остальные технические. Если считать проценты от всех строк, картина
выглядит хуже, чем есть. Среди содержательных строк дата, контрагент, фирма и
счёт заполнены полностью, а вот **категория только у 32%**, подкатегория у 58%.

Отсюда правило блока: сводки по контрагенту, фирме, счёту и месяцу достоверны,
а сводка по категории обязана показывать, какая доля денег в неё не попала.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Callable, Sequence

from app.bbc.layout import Column, Layout, resolve_layout
from app.bbc.normalize import clean, parse_date, parse_money

log = logging.getLogger(__name__)

WORKSHEET = "Журнал"

#: Колонки листа «Журнал». Ищутся по заголовку — см. `app.bbc.layout`.
#:
#: Здесь это не перестраховка, а исправление. Раньше позиции были прибиты
#: числами и проверки шапки у «Журнала» не было вовсе. В книгу вставили колонку
#: «ФОТ/Детали» на позицию 10, и «Проект», «Категория», «Комментарии» уехали на
#: +1. Парсер продолжал читать молча: в «Категорию» попадал «Проект», в
#: «Комментарий» — «Категория». Сводка по категориям расходов была неверной и
#: ничем себя не выдавала. Ровно поэтому колонки теперь ищутся по названию.
JOURNAL_COLUMNS: tuple[Column, ...] = (
    Column("dds_month", "ДДС Мес", ("ДДС Мес (цифра)",), hint=0, required=False),
    Column("pnl_period", "ОПиУ период", ("ОПиУ период",), hint=1, required=False),
    Column("date", "Дата", ("Дата",), hint=2),
    Column("account", "Счет", ("Счет",), hint=3),
    Column("inflow", "Приход", ("Приход",), hint=4),
    Column("outflow", "Расход", ("Расход",), hint=5),
    Column("counterparty", "Контрагент", ("Контрагент",), hint=6),
    Column("contract", "№ Договора", ("№Дог.", "№ Дог.", "№ Договора"), hint=8, required=False),
    Column("subcategory", "Подкатегория", ("Подкатегория",), hint=9, required=False),
    Column("payroll_loan", "ФОТ/Детали", ("ФОТ/Детали", "ФОТ, займ"), hint=10, required=False),
    Column("project", "Проект", ("Проект",), hint=11, required=False),
    Column("category", "Категория", ("Категория",), hint=12, required=False),
    Column("comment", "Комментарии", ("Комментарии", "Комментарий"), hint=13, required=False),
    Column("product", "Продукт", ("Продукт",), hint=22, required=False),
    Column("firm", "Фирма", ("Фирма",), hint=24, required=False),
)


@dataclass
class JournalRow:
    index: int
    at: date | None
    account: str
    counterparty: str
    firm: str
    category: str
    subcategory: str
    project: str
    contract: str
    comment: str
    inflow: float
    outflow: float

    @property
    def net(self) -> float:
        return self.inflow - self.outflow

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["at"] = self.at.isoformat() if self.at else None
        data["net"] = round(self.net, 2)
        return data


def _cell(layout: Layout, row: Sequence[str], key: str) -> str:
    value = clean(layout.cell(row, key))
    # Формульные ошибки в текстовых колонках — то же самое, что пусто.
    return "" if value.startswith("#") else value


def resolve_journal_layout(header: Sequence[str]) -> Layout:
    return resolve_layout(WORKSHEET, JOURNAL_COLUMNS, header)


def parse_journal(grid: Sequence[Sequence[str]]) -> list[JournalRow]:
    """Разобрать лист, оставив только содержательные строки.

    Содержательная — та, где есть дата либо хоть одна сумма. Технические строки
    (а их в листе большинство) отбрасываются: они только раздували бы выдачу и
    портили проценты полноты.
    """
    if not grid:
        return []
    layout = resolve_journal_layout(grid[0])

    rows: list[JournalRow] = []
    for index, raw in enumerate(grid[1:], start=2):
        at = parse_date(_cell(layout, raw, "date"))
        inflow = parse_money(_cell(layout, raw, "inflow")) or 0.0
        outflow = parse_money(_cell(layout, raw, "outflow")) or 0.0
        if at is None and not inflow and not outflow:
            continue
        rows.append(
            JournalRow(
                index=index,
                at=at,
                account=_cell(layout, raw, "account"),
                counterparty=_cell(layout, raw, "counterparty"),
                firm=_cell(layout, raw, "firm"),
                category=_cell(layout, raw, "category"),
                subcategory=_cell(layout, raw, "subcategory"),
                project=_cell(layout, raw, "project"),
                contract=_cell(layout, raw, "contract"),
                comment=_cell(layout, raw, "comment"),
                inflow=inflow,
                outflow=outflow,
            )
        )
    return rows


# ── Мини-сводки ──────────────────────────────────────────────────────────────────

GROUPS: dict[str, tuple[str, Callable[[JournalRow], str]]] = {
    "counterparty": ("Контрагент", lambda row: row.counterparty),
    "firm": ("Фирма", lambda row: row.firm),
    "account": ("Счёт", lambda row: row.account),
    "category": ("Категория", lambda row: row.category),
    "subcategory": ("Подкатегория", lambda row: row.subcategory),
    "project": ("Проект", lambda row: row.project),
    "month": ("Месяц", lambda row: row.at.strftime("%Y-%m") if row.at else ""),
}

MEASURES: dict[str, tuple[str, Callable[[JournalRow], float]]] = {
    "inflow": ("Приход", lambda row: row.inflow),
    "outflow": ("Расход", lambda row: row.outflow),
    "net": ("Сальдо", lambda row: row.net),
    "turnover": ("Оборот", lambda row: row.inflow + row.outflow),
}


def summarize(
    rows: list[JournalRow],
    group: str = "counterparty",
    measure: str = "outflow",
    limit: int = 25,
) -> dict[str, Any]:
    """Свод по одному измерению.

    Вместе с итогами возвращает долю строк и денег, у которых измерение не
    заполнено: по категории это треть данных, и молчать об этом нельзя.
    """
    if group not in GROUPS:
        group = "counterparty"
    if measure not in MEASURES:
        measure = "outflow"

    group_title, key_of = GROUPS[group]
    measure_title, value_of = MEASURES[measure]

    buckets: dict[str, dict[str, float]] = {}
    missing_rows = 0
    missing_value = 0.0

    for row in rows:
        key = key_of(row)
        value = value_of(row)
        if not key:
            missing_rows += 1
            missing_value += abs(value)
            continue
        bucket = buckets.setdefault(key, {"value": 0.0, "count": 0.0, "inflow": 0.0, "outflow": 0.0})
        bucket["value"] += value
        bucket["count"] += 1
        bucket["inflow"] += row.inflow
        bucket["outflow"] += row.outflow

    items = [
        {
            "key": key,
            "value": round(data["value"], 2),
            "count": int(data["count"]),
            "inflow": round(data["inflow"], 2),
            "outflow": round(data["outflow"], 2),
        }
        for key, data in buckets.items()
    ]
    items.sort(key=lambda item: abs(item["value"]), reverse=True)

    covered = len(rows) - missing_rows
    return {
        "group": group,
        "group_title": group_title,
        "measure": measure,
        "measure_title": measure_title,
        "items": items[:limit],
        "total": round(sum(item["value"] for item in items), 2),
        "rows": len(rows),
        "covered_rows": covered,
        # Доля строк, попавших в свод. Для категории это ~32%.
        "coverage": round(covered / len(rows), 4) if rows else 0.0,
        "missing_rows": missing_rows,
        "missing_value": round(missing_value, 2),
        "truncated": len(items) > limit,
    }


def coverage(rows: list[JournalRow]) -> dict[str, Any]:
    """Насколько заполнены измерения — по содержательным строкам."""
    total = len(rows) or 1

    def share(pick: Callable[[JournalRow], str]) -> float:
        return round(sum(1 for row in rows if pick(row)) / total, 4)

    return {
        "rows": len(rows),
        "with_date": share(lambda row: row.at.isoformat() if row.at else ""),
        "with_counterparty": share(lambda row: row.counterparty),
        "with_firm": share(lambda row: row.firm),
        "with_account": share(lambda row: row.account),
        "with_category": share(lambda row: row.category),
        "with_subcategory": share(lambda row: row.subcategory),
        "inflow": round(sum(row.inflow for row in rows), 2),
        "outflow": round(sum(row.outflow for row in rows), 2),
        "net": round(sum(row.net for row in rows), 2),
    }


def dimensions(rows: list[JournalRow]) -> dict[str, list[str]]:
    """Значения измерений для фильтров блока."""

    def distinct(pick: Callable[[JournalRow], str]) -> list[str]:
        return sorted({value for value in map(pick, rows) if value})

    return {
        "firms": distinct(lambda row: row.firm),
        "accounts": distinct(lambda row: row.account),
        "categories": distinct(lambda row: row.category),
        "subcategories": distinct(lambda row: row.subcategory),
        "months": distinct(lambda row: row.at.strftime("%Y-%m") if row.at else ""),
    }


__all__ = [
    "GROUPS",
    "MEASURES",
    "JOURNAL_COLUMNS",
    "JournalRow",
    "coverage",
    "dimensions",
    "parse_journal",
    "resolve_journal_layout",
    "summarize",
]
