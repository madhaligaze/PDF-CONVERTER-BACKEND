"""Отчёты раздела «Финансы»: деньги, прибыль, долги, проекты, календарь, план/факт.

Отчёты ничего не хранят и ничего не меняют. Каждый считается из операций по
запросу — см. объяснение в `models.py`, раздел «производное».

Две даты и два отчёта
─────────────────────
«Деньги» считаются по `paid_at` — когда деньги двинулись. «Прибыль» — по
`accrued_at` (а если её нет, то по `paid_at`) — когда возникло обязательство.
Это не два взгляда на одно: между ними лежит вся дебиторка, и вопрос «прибыль
есть, а денег нет» отвечается именно разницей этих двух отчётов.

Про перевод
───────────
Перевод между своими счетами не доход и не расход: деньги не появились и не
исчезли, они переехали. В отчёте о движении он обязан сворачиваться в ноль, и
это проверяется контрольной суммой, а не обещается.

Про честность нуля
──────────────────
Месяц, в котором ничего не было, попадает в отчёт нулём, а не пропадает из
ряда. Пропуск месяца читается как «данных нет» и ломает сравнение с соседним
месяцем — это уже стоило нам дефекта на дашборде BBC в июле 2026.
"""
from __future__ import annotations

import calendar as calendar_module
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.models import (
    Account,
    Category,
    Counterparty,
    Operation,
    OperationProject,
    Plan,
    Project,
)

ZERO = Decimal("0")


def _months(date_from: date, date_to: date) -> list[date]:
    """Первые числа всех месяцев интервала — включая пустые."""
    months: list[date] = []
    cursor = date(date_from.year, date_from.month, 1)
    last = date(date_to.year, date_to.month, 1)
    while cursor <= last:
        months.append(cursor)
        cursor = date(cursor.year + (cursor.month == 12), (cursor.month % 12) + 1, 1)
    return months


def _month_key(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def _live(workspace_id: uuid.UUID):
    return (Operation.workspace_id == workspace_id, Operation.deleted_at.is_(None))


def _visible_accounts(session: Session, workspace_id: uuid.UUID) -> set[uuid.UUID]:
    """Счета, которые участвуют в отчётах.

    Исключённый счёт (личная карта директора) не должен попадать в
    корпоративный отчёт о движении денег. Но его операции не исчезают: они
    видны в журнале, и их сумма показана отдельной строкой «исключённые
    счета» — иначе остаток в отчёте не сойдётся с суммой счетов, и объяснить
    расхождение будет нечем.
    """
    rows = session.scalars(
        sa.select(Account.id).where(
            Account.workspace_id == workspace_id, Account.excluded_from_reports.is_(False)
        )
    )
    return set(rows)


@dataclass
class MonthRow:
    month: str
    income_fact: Decimal = ZERO
    income_plan: Decimal = ZERO
    expense_fact: Decimal = ZERO
    expense_plan: Decimal = ZERO

    @property
    def net_fact(self) -> Decimal:
        return self.income_fact - self.expense_fact

    @property
    def net_all(self) -> Decimal:
        return (self.income_fact + self.income_plan) - (self.expense_fact + self.expense_plan)


def _collect_months(
    session: Session,
    workspace_id: uuid.UUID,
    date_from: date,
    date_to: date,
    *,
    by_accrual: bool,
    visible: set[uuid.UUID],
) -> dict[str, MonthRow]:
    column = sa.func.coalesce(Operation.accrued_at, Operation.paid_at) if by_accrual else Operation.paid_at
    rows = session.execute(
        sa.select(
            column.label("when"),
            Operation.kind,
            Operation.status,
            Operation.amount_base,
            Operation.account_from_id,
            Operation.account_to_id,
        ).where(*_live(workspace_id), column >= date_from, column <= date_to)
    )
    buckets: dict[str, MonthRow] = {
        _month_key(month): MonthRow(_month_key(month)) for month in _months(date_from, date_to)
    }
    for when, kind, status, amount, account_from, account_to in rows:
        if kind == "transfer":
            continue  # перевод не доход и не расход — см. докстринг модуля
        if kind == "income" and account_to not in visible:
            continue
        if kind == "expense" and account_from not in visible:
            continue
        row = buckets.setdefault(_month_key(when), MonthRow(_month_key(when)))
        value = Decimal(str(amount or 0))
        if kind == "income":
            if status == "fact":
                row.income_fact += value
            else:
                row.income_plan += value
        else:
            if status == "fact":
                row.expense_fact += value
            else:
                row.expense_plan += value
    return buckets


def _breakdown(
    session: Session,
    workspace_id: uuid.UUID,
    date_from: date,
    date_to: date,
    *,
    by_accrual: bool,
    group: str,
    visible: set[uuid.UUID],
) -> dict[str, list[dict[str, Any]]]:
    """Разбивка доходов и расходов по категориям, контрагентам или проектам."""
    column = sa.func.coalesce(Operation.accrued_at, Operation.paid_at) if by_accrual else Operation.paid_at

    if group == "project":
        rows = session.execute(
            sa.select(
                column.label("when"),
                Operation.kind,
                Operation.status,
                OperationProject.amount,
                Project.name,
                Operation.account_from_id,
                Operation.account_to_id,
            )
            .join(OperationProject, OperationProject.operation_id == Operation.id)
            .join(Project, Project.id == OperationProject.project_id)
            .where(*_live(workspace_id), column >= date_from, column <= date_to)
        )
    else:
        label_model = Category if group == "category" else Counterparty
        label_join = (
            Operation.category_id == Category.id
            if group == "category"
            else Operation.counterparty_id == Counterparty.id
        )
        rows = session.execute(
            sa.select(
                column.label("when"),
                Operation.kind,
                Operation.status,
                Operation.amount_base,
                label_model.name,
                Operation.account_from_id,
                Operation.account_to_id,
            )
            .outerjoin(label_model, label_join)
            .where(*_live(workspace_id), column >= date_from, column <= date_to)
        )

    #: Название для операций, у которых группировочного признака нет. Не
    #: «Прочее» и не пустая строка: человек должен видеть, что признак не
    #: заполнен, и мочь это исправить.
    unnamed = {
        "category": "Без категории",
        "counterparty": "Без контрагента",
        "project": "Без проекта",
    }[group]

    # Факт и ожидание считаются отдельно, а не одной суммой.
    #
    # Сначала здесь была одна сумма на месяц, и в отчёте получались две разные
    # цифры про одно и то же: в шапке «Поступило за период» стоял факт, а в
    # итоге разбивки — факт плюс ожидания. Обе верные, разница ровно на
    # запланированный платёж, и на экране это ничем не объяснялось. Человек
    # видит два ответа на один вопрос и не знает, какой из них правда.
    fact: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    plan: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    for when, kind, status, amount, name, account_from, account_to in rows:
        if kind == "transfer":
            continue
        if kind == "income" and account_to not in visible:
            continue
        if kind == "expense" and account_from not in visible:
            continue
        side = "income" if kind == "income" else "expense"
        key = (side, name or unnamed)
        bucket = fact if status == "fact" else plan
        bucket[key][_month_key(when)] += Decimal(str(amount or 0))

    months = [_month_key(month) for month in _months(date_from, date_to)]
    result: dict[str, list[dict[str, Any]]] = {"income": [], "expense": []}
    for key in set(fact) | set(plan):
        side, name = key
        fact_months = fact.get(key, {})
        plan_months = plan.get(key, {})
        result[side].append(
            {
                "name": name,
                # `total` — только факт. Ожидание показывается отдельным числом
                # и отдельной подписью, а не подмешивается в итог.
                "total": str(sum(fact_months.values(), ZERO)),
                "planned": str(sum(plan_months.values(), ZERO)),
                "months": {month: str(fact_months.get(month, ZERO)) for month in months},
                "months_plan": {month: str(plan_months.get(month, ZERO)) for month in months},
            }
        )
    for side in result:
        result[side].sort(key=lambda item: Decimal(item["total"]), reverse=True)
    return result


def account_balances(session: Session, workspace_id: uuid.UUID, *, as_of: date | None = None) -> list[dict[str, Any]]:
    """Остатки по счетам: начальный плюс движение фактических операций.

    План в остаток не входит: ожидаемый платёж деньгами ещё не является. Он
    показывается отдельно — в календаре и в строке «с учётом плана».
    """
    accounts = list(
        session.scalars(
            sa.select(Account)
            .where(Account.workspace_id == workspace_id, Account.archived_at.is_(None))
            .order_by(Account.position, Account.name)
        )
    )
    moves: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    planned: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)

    query = sa.select(
        Operation.kind, Operation.status, Operation.amount, Operation.account_from_id,
        Operation.account_to_id,
    ).where(*_live(workspace_id))
    if as_of is not None:
        query = query.where(Operation.paid_at <= as_of)
    for kind, status, amount, account_from, account_to in session.execute(query):
        value = Decimal(str(amount or 0))
        target = moves if status == "fact" else planned
        if account_from is not None:
            target[account_from] -= value
        if account_to is not None:
            target[account_to] += value

    result = []
    for account in accounts:
        start = Decimal(str(account.starting_balance or 0))
        balance = start + moves[account.id]
        result.append(
            {
                "id": str(account.id),
                "name": account.name,
                "kind": account.kind,
                "currency": account.currency,
                "starting_balance": str(start),
                "balance": str(balance),
                "balance_with_plan": str(balance + planned[account.id]),
                "excluded_from_reports": account.excluded_from_reports,
            }
        )
    return result


def cash_flow(
    session: Session,
    workspace_id: uuid.UUID,
    date_from: date,
    date_to: date,
    *,
    group: str = "category",
) -> dict[str, Any]:
    """Движение денег по месяцам с остатками и контрольной суммой."""
    visible = _visible_accounts(session, workspace_id)
    buckets = _collect_months(
        session, workspace_id, date_from, date_to, by_accrual=False, visible=visible
    )
    months = [_month_key(month) for month in _months(date_from, date_to)]

    # Остаток на начало периода: всё, что было до date_from.
    opening = ZERO
    for account in account_balances(session, workspace_id, as_of=date_from - timedelta(days=1)):
        if account["excluded_from_reports"]:
            continue
        opening += Decimal(account["balance"])

    rows = []
    running = opening
    for month in months:
        bucket = buckets.get(month) or MonthRow(month)
        start = running
        running = start + bucket.net_fact
        rows.append(
            {
                "month": month,
                "start_balance": str(start),
                "income": str(bucket.income_fact),
                "expense": str(bucket.expense_fact),
                "net": str(bucket.net_fact),
                "end_balance": str(running),
                "income_plan": str(bucket.income_plan),
                "expense_plan": str(bucket.expense_plan),
                "end_balance_with_plan": str(running + bucket.income_plan - bucket.expense_plan),
            }
        )

    breakdown = _breakdown(
        session, workspace_id, date_from, date_to, by_accrual=False, group=group, visible=visible
    )
    return {
        "months": months,
        "opening_balance": str(opening),
        "closing_balance": str(running),
        "rows": rows,
        "breakdown": breakdown,
        "checks": _checks(session, workspace_id, rows, breakdown),
    }


def profit_and_loss(
    session: Session,
    workspace_id: uuid.UUID,
    date_from: date,
    date_to: date,
    *,
    group: str = "category",
) -> dict[str, Any]:
    """Прибыль по начислению: доходы и расходы по дате сделки, а не платежа."""
    visible = _visible_accounts(session, workspace_id)
    buckets = _collect_months(
        session, workspace_id, date_from, date_to, by_accrual=True, visible=visible
    )
    months = [_month_key(month) for month in _months(date_from, date_to)]
    rows = []
    for month in months:
        bucket = buckets.get(month) or MonthRow(month)
        income = bucket.income_fact + bucket.income_plan
        expense = bucket.expense_fact + bucket.expense_plan
        rows.append(
            {
                "month": month,
                "income": str(income),
                "expense": str(expense),
                "profit": str(income - expense),
                "income_fact": str(bucket.income_fact),
                "expense_fact": str(bucket.expense_fact),
                "margin": str(
                    ((income - expense) / income * 100).quantize(Decimal("0.1")) if income else ZERO
                ),
            }
        )
    return {
        "months": months,
        "rows": rows,
        "breakdown": _breakdown(
            session, workspace_id, date_from, date_to, by_accrual=True, group=group, visible=visible
        ),
    }


def debts(session: Session, workspace_id: uuid.UUID, *, as_of: date | None = None) -> dict[str, Any]:
    """Дебиторка и кредиторка: ожидания, срок которых известен.

    Долг здесь — это операция в состоянии «план»: обязательство есть, деньги не
    двинулись. Просроченным считается план с датой платежа раньше `as_of` —
    и это то, чего не умеет календарная плановость: там наступившая дата молча
    превращает план в ничто.
    """
    as_of = as_of or date.today()
    rows = session.execute(
        sa.select(
            Operation.id,
            Operation.kind,
            Operation.paid_at,
            Operation.accrued_at,
            Operation.amount_base,
            Operation.comment,
            Counterparty.name,
            Category.name,
        )
        .outerjoin(Counterparty, Operation.counterparty_id == Counterparty.id)
        .outerjoin(Category, Operation.category_id == Category.id)
        .where(*_live(workspace_id), Operation.status == "plan")
        .order_by(Operation.paid_at)
    )

    receivable: list[dict[str, Any]] = []
    payable: list[dict[str, Any]] = []
    for op_id, kind, paid_at, accrued_at, amount, comment, counterparty, category in rows:
        item = {
            "id": str(op_id),
            "due": paid_at.isoformat(),
            "accrued": (accrued_at or paid_at).isoformat(),
            "amount": str(Decimal(str(amount or 0))),
            "counterparty": counterparty or "Без контрагента",
            "category": category or "",
            "comment": comment or "",
            "overdue_days": max(0, (as_of - paid_at).days),
        }
        (receivable if kind == "income" else payable).append(item)

    def summarize(items: Sequence[dict[str, Any]]) -> dict[str, Any]:
        by_counterparty: dict[str, Decimal] = defaultdict(lambda: ZERO)
        overdue = ZERO
        total = ZERO
        for item in items:
            value = Decimal(item["amount"])
            by_counterparty[item["counterparty"]] += value
            total += value
            if item["overdue_days"] > 0:
                overdue += value
        return {
            "total": str(total),
            "overdue": str(overdue),
            "by_counterparty": [
                {"name": name, "amount": str(amount)}
                for name, amount in sorted(by_counterparty.items(), key=lambda kv: kv[1], reverse=True)
            ],
            "items": items,
        }

    return {
        "as_of": as_of.isoformat(),
        "receivable": summarize(receivable),
        "payable": summarize(payable),
    }


def projects_report(
    session: Session, workspace_id: uuid.UUID, date_from: date, date_to: date
) -> dict[str, Any]:
    """Прибыль по проектам — и честная строка о том, что разнесено не всё.

    Операция без разнесения не попадает ни в один проект. Сумма таких операций
    показывается строкой «не разнесено»: без неё сумма проектов не сходится с
    отчётом о прибыли, а причину этого расхождения найти нельзя.
    """
    rows = session.execute(
        sa.select(Project.name, Operation.kind, sa.func.sum(OperationProject.amount))
        .join(OperationProject, OperationProject.project_id == Project.id)
        .join(Operation, Operation.id == OperationProject.operation_id)
        .where(*_live(workspace_id), Operation.paid_at >= date_from, Operation.paid_at <= date_to)
        .group_by(Project.name, Operation.kind)
    )
    by_project: dict[str, dict[str, Decimal]] = defaultdict(lambda: {"income": ZERO, "expense": ZERO})
    for name, kind, total in rows:
        if kind == "transfer":
            continue
        by_project[name][kind] += Decimal(str(total or 0))

    unassigned = session.execute(
        sa.select(Operation.kind, sa.func.sum(Operation.amount_base))
        .where(
            *_live(workspace_id),
            Operation.paid_at >= date_from,
            Operation.paid_at <= date_to,
            Operation.split_state == "none",
            Operation.kind != "transfer",
        )
        .group_by(Operation.kind)
    )
    not_split = {"income": ZERO, "expense": ZERO}
    for kind, total in unassigned:
        not_split[kind] = Decimal(str(total or 0))

    items = []
    for name, values in by_project.items():
        income, expense = values["income"], values["expense"]
        profit = income - expense
        items.append(
            {
                "name": name,
                "income": str(income),
                "expense": str(expense),
                "profit": str(profit),
                "margin": str((profit / income * 100).quantize(Decimal("0.1")) if income else ZERO),
            }
        )
    items.sort(key=lambda item: Decimal(item["profit"]), reverse=True)
    return {
        "items": items,
        "not_split": {
            "income": str(not_split["income"]),
            "expense": str(not_split["expense"]),
            "note": "операции, не разнесённые ни на один проект",
        },
    }


def calendar(session: Session, workspace_id: uuid.UUID, year: int, month: int) -> dict[str, Any]:
    """Календарь платежей на месяц: движение по дням и остаток на конец дня.

    Кассовый разрыв — это день, в который остаток уходит ниже нуля с учётом
    плановых платежей. Он помечается и перечисляется отдельно: цвет в
    интерфейсе положен только такому дню, см. правило об индикаторах в
    `CLAUDE.md`.
    """
    first = date(year, month, 1)
    last = date(year, month, calendar_module.monthrange(year, month)[1])

    opening = ZERO
    for account in account_balances(session, workspace_id, as_of=first - timedelta(days=1)):
        if account["excluded_from_reports"]:
            continue
        opening += Decimal(account["balance"])

    rows = session.execute(
        sa.select(
            Operation.paid_at, Operation.kind, Operation.status, Operation.amount_base,
            Operation.account_from_id, Operation.account_to_id,
        ).where(*_live(workspace_id), Operation.paid_at >= first, Operation.paid_at <= last)
    )
    visible = _visible_accounts(session, workspace_id)
    per_day: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"income": ZERO, "expense": ZERO, "income_plan": ZERO, "expense_plan": ZERO}
    )
    for paid_at, kind, status, amount, account_from, account_to in rows:
        if kind == "transfer":
            continue
        if kind == "income" and account_to not in visible:
            continue
        if kind == "expense" and account_from not in visible:
            continue
        value = Decimal(str(amount or 0))
        bucket = per_day[paid_at.isoformat()]
        suffix = "" if status == "fact" else "_plan"
        bucket[("income" if kind == "income" else "expense") + suffix] += value

    days = []
    running = opening
    gaps: list[str] = []
    cursor = first
    while cursor <= last:
        key = cursor.isoformat()
        bucket = per_day.get(key) or {"income": ZERO, "expense": ZERO, "income_plan": ZERO, "expense_plan": ZERO}
        start = running
        running = (
            start + bucket["income"] + bucket["income_plan"] - bucket["expense"] - bucket["expense_plan"]
        )
        negative = running < 0
        if negative:
            gaps.append(key)
        days.append(
            {
                "date": key,
                "income": str(bucket["income"]),
                "expense": str(bucket["expense"]),
                "income_plan": str(bucket["income_plan"]),
                "expense_plan": str(bucket["expense_plan"]),
                "balance": str(running),
                "negative": negative,
            }
        )
        cursor += timedelta(days=1)

    return {
        "month": f"{year:04d}-{month:02d}",
        "opening_balance": str(opening),
        "days": days,
        "cash_gaps": gaps,
    }


def plan_actual(
    session: Session,
    workspace_id: uuid.UUID,
    date_from: date,
    date_to: date,
    *,
    method: str = "cash",
) -> dict[str, Any]:
    """План против факта по статьям и месяцам.

    `method` выбирает, чем считается факт: деньгами (`cash`) или начислением
    (`accrual`). План хранится отдельно для каждого способа — бюджет платежей и
    бюджет расходов разные документы, и складывать их нельзя.
    """
    by_accrual = method == "accrual"
    column = sa.func.coalesce(Operation.accrued_at, Operation.paid_at) if by_accrual else Operation.paid_at
    months = [_month_key(month) for month in _months(date_from, date_to)]

    fact_rows = session.execute(
        sa.select(column.label("when"), Operation.kind, Category.id, Category.name, sa.func.sum(Operation.amount_base))
        .outerjoin(Category, Operation.category_id == Category.id)
        .where(
            *_live(workspace_id),
            Operation.status == "fact",
            Operation.kind != "transfer",
            column >= date_from,
            column <= date_to,
        )
        .group_by(column, Operation.kind, Category.id, Category.name)
    )
    facts: dict[tuple[str, str, str], Decimal] = defaultdict(lambda: ZERO)
    names: dict[str, str] = {}
    for when, kind, category_id, name, total in fact_rows:
        key = str(category_id) if category_id else "none"
        names[key] = name or "Без категории"
        side = "income" if kind == "income" else "expense"
        facts[(side, key, _month_key(when))] += Decimal(str(total or 0))

    plan_rows = session.scalars(
        sa.select(Plan).where(
            Plan.workspace_id == workspace_id,
            Plan.method == method,
            Plan.month >= date(date_from.year, date_from.month, 1),
            Plan.month <= date(date_to.year, date_to.month, 1),
        )
    )
    plans: dict[tuple[str, str, str], Decimal] = defaultdict(lambda: ZERO)
    for plan in plan_rows:
        key = str(plan.category_id) if plan.category_id else "none"
        plans[(plan.side, key, _month_key(plan.month))] += Decimal(str(plan.amount or 0))
        if key not in names and plan.category_id:
            category = session.get(Category, plan.category_id)
            names[key] = category.name if category else "Категория удалена"

    items: list[dict[str, Any]] = []
    keys = {(side, key) for side, key, _month in list(facts) + list(plans)}
    for side, key in sorted(keys, key=lambda pair: names.get(pair[1], "")):
        cells = []
        for month in months:
            fact = facts.get((side, key, month), ZERO)
            plan = plans.get((side, key, month), ZERO)
            cells.append(
                {
                    "month": month,
                    "fact": str(fact),
                    "plan": str(plan),
                    "deviation": str(fact - plan),
                    # Процент выполнения не считается при нулевом плане: «∞%»
                    # в отчёте выглядит как ошибка расчёта, а не как «плана не
                    # было». Отсутствие плана показывается пустотой.
                    "done_pct": str((fact / plan * 100).quantize(Decimal("1"))) if plan else None,
                }
            )
        items.append(
            {
                "side": side,
                "key": key,
                "name": names.get(key, "Без категории"),
                "cells": cells,
                "fact_total": str(sum((Decimal(cell["fact"]) for cell in cells), ZERO)),
                "plan_total": str(sum((Decimal(cell["plan"]) for cell in cells), ZERO)),
            }
        )
    return {"months": months, "method": method, "items": items}


def _checks(
    session: Session,
    workspace_id: uuid.UUID,
    rows: Sequence[dict[str, Any]],
    breakdown: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Контрольные суммы отчёта — то, чем отчёт доказывает свою правоту.

    Проверяется не «примерно сошлось», а тождества, которые обязаны быть
    точными: сумма разбивки равна итогу по месяцам, переводы свёрнуты в ноль,
    разнесение по проектам не превышает сумму операции. Расхождение показывается
    строкой в отчёте, а не пишется в лог, где его никто не увидит.
    """
    checks: list[dict[str, Any]] = []

    for side, field in (("income", "income"), ("expense", "expense")):
        months_total = sum((Decimal(row[field]) for row in rows), ZERO)
        # `total` в разбивке — уже только факт, вычитать ожидания не нужно.
        parts_total = sum((Decimal(item["total"]) for item in breakdown[side]), ZERO)
        checks.append(
            {
                "name": f"Разбивка сходится с итогом ({'поступления' if side == 'income' else 'списания'})",
                "ok": months_total == parts_total,
                "left": str(months_total),
                "right": str(parts_total),
            }
        )

    transfers = session.scalar(
        sa.select(sa.func.count(Operation.id)).where(
            *_live(workspace_id),
            Operation.kind == "transfer",
            sa.or_(Operation.account_from_id.is_(None), Operation.account_to_id.is_(None)),
        )
    )
    checks.append(
        {
            "name": "У каждого перевода есть оба счёта",
            "ok": not transfers,
            "left": str(transfers or 0),
            "right": "0",
        }
    )

    mismatched = session.scalar(
        sa.select(sa.func.count(Operation.id)).where(
            *_live(workspace_id), Operation.split_state == "mismatch"
        )
    )
    checks.append(
        {
            "name": "Разнесение по проектам не больше суммы операции",
            "ok": not mismatched,
            "left": str(mismatched or 0),
            "right": "0",
            "hint": "операции, где сумма разнесения превышает сумму платежа" if mismatched else "",
        }
    )
    return checks


__all__ = [
    "account_balances",
    "calendar",
    "cash_flow",
    "debts",
    "plan_actual",
    "profit_and_loss",
    "projects_report",
]
