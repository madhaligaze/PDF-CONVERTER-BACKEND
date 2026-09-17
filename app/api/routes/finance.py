"""HTTP-маршруты раздела «Финансы».

Почему они здесь, а не внутри `app/finance/`
────────────────────────────────────────────
По той же причине, что у «Книг»: модуль задуман переносимым, а маршрутам надо
знать, как в этом продукте устроены учётки. Место, где сходятся модули,
называется корнем композиции и лежит снаружи обоих. Здесь и только здесь
встречаются `app.finance` и `app.bbc.deps`.

Обработчики — обычный `def`, а не `async def`: внутри синхронные SQLAlchemy и
openpyxl, разбор файла на двадцать тысяч строк считается секундами. В
`async def` это встало бы колом в цикле событий и подвесило заодно дашборд;
обычный `def` FastAPI уводит в пул потоков.
"""
from __future__ import annotations

import calendar as calendar_module
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from app.bbc.deps import require_admin, require_block_user
from app.finance import grid as grid_module, reports, service
from app.finance.config import finance_settings
from app.finance.db import finance_session
from app.finance.importing import ImportError_, analyze
from app.finance.models import (
    Account,
    Category,
    Counterparty,
    ImportBatch,
    ImportRow,
    Operation,
    Plan,
    Project,
    Tag,
)
from app.finance.service import FinanceError, VersionConflict

log = logging.getLogger(__name__)

router = APIRouter(prefix="/finance", tags=["finance"])

# Кто пускается в раздел. Отдельное право «finance», а не `require_user`:
# «вошёл в дашборд» — это не право видеть движение денег компании.
require_finance = require_block_user("finance")
# Всё, что меняет устройство учёта, а не запись в нём: состав счетов, валюта,
# начальные остатки. Начальный остаток — не настройка отображения: изменив его,
# сотрудник изменил бы остаток на всех счетах и во всех отчётах задним числом.
require_finance_admin = require_admin


def _guard() -> None:
    if not finance_settings.enabled:
        raise HTTPException(status_code=404, detail="Раздел «Финансы» выключен")


def _actor(user: Any) -> str:
    return getattr(user, "username", "") or ""


def _fail(exc: FinanceError) -> HTTPException:
    if isinstance(exc, VersionConflict):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _parse_date(value: str | None, *, field: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field}: ждём дату вида 2026-09-17") from exc


def _money(value: Any, *, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"{field}: это не сумма") from exc


# ── Обзор ────────────────────────────────────────────────────────────────────


@router.get("/overview")
def overview(user=Depends(require_finance)) -> dict[str, Any]:
    """Первый экран: счета, остатки и ближайшие ожидания.

    Один запрос, а не четыре: раздел открывают десятки раз в день, и каждый
    лишний круг до сервера человек ощущает как «подвисло».
    """
    _guard()
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        balances = reports.account_balances(session, workspace.id)
        debt = reports.debts(session, workspace.id)
        total = sum(Decimal(item["balance"]) for item in balances)
        total_with_plan = sum(Decimal(item["balance_with_plan"]) for item in balances)
        operations, count = service.list_operations(
            session, workspace.id, service.OperationFilter(statuses=("plan",)), limit=5
        )
        return {
            "workspace": {
                "id": str(workspace.id),
                "title": workspace.title,
                "currency": workspace.base_currency,
            },
            "accounts": balances,
            "total": str(total),
            "total_with_plan": str(total_with_plan),
            "receivable": debt["receivable"]["total"],
            "payable": debt["payable"]["total"],
            "overdue_receivable": debt["receivable"]["overdue"],
            "planned_count": count,
            "planned_preview": [
                {
                    "id": str(operation.id),
                    "kind": operation.kind,
                    "paid_at": operation.paid_at.isoformat(),
                    "amount": str(operation.amount),
                    "comment": operation.comment,
                }
                for operation in operations
            ],
        }


# ── Справочники ──────────────────────────────────────────────────────────────


@router.get("/dictionaries")
def dictionaries(user=Depends(require_finance)) -> dict[str, Any]:
    """Все справочники разом — ими наполняются выпадающие списки форм."""
    _guard()
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        return {
            "accounts": [
                {
                    "id": str(item.id),
                    "name": item.name,
                    "kind": item.kind,
                    "currency": item.currency,
                    "starting_balance": str(item.starting_balance),
                    "excluded_from_reports": item.excluded_from_reports,
                }
                for item in service.list_accounts(session, workspace.id)
            ],
            "categories": [
                {"id": str(item.id), "name": item.name, "side": item.side, "system_key": item.system_key}
                for item in service.list_categories(session, workspace.id)
            ],
            "counterparties": [
                {"id": str(item.id), "name": item.name, "role": item.role}
                for item in service.list_counterparties(session, workspace.id)
            ],
            "projects": [
                {"id": str(item.id), "name": item.name, "closed": item.closed}
                for item in service.list_projects(session, workspace.id)
            ],
            "tags": [{"id": str(item.id), "name": item.name} for item in service.list_tags(session, workspace.id)],
        }


class AccountIn(BaseModel):
    name: str
    kind: str = "bank"
    currency: str | None = None
    starting_balance: str = "0"
    excluded_from_reports: bool = False


@router.post("/accounts")
def create_account(body: AccountIn, user=Depends(require_finance_admin)) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        try:
            account = service.create_account(
                session,
                workspace,
                name=body.name,
                kind=body.kind,
                currency=body.currency,
                starting_balance=_money(body.starting_balance, field="starting_balance"),
                excluded_from_reports=body.excluded_from_reports,
            )
        except FinanceError as exc:
            raise _fail(exc) from exc
        return {"id": str(account.id), "name": account.name}


class EntryIn(BaseModel):
    name: str
    #: Для категорий — сторона учёта, для контрагентов — роль.
    side: str | None = None
    role: str | None = None


@router.post("/dictionaries/{kind}")
def create_entry(kind: str, body: EntryIn, user=Depends(require_finance)) -> dict[str, Any]:
    """Создать статью, контрагента, проект или тег.

    Счёт сюда не попадает намеренно: он требует прав администратора и заводится
    своим маршрутом выше.
    """
    _guard()
    if kind not in ("categories", "counterparties", "projects", "tags"):
        raise HTTPException(status_code=404, detail="Неизвестный справочник")
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        if kind == "categories":
            if body.side not in ("income", "expense"):
                raise HTTPException(status_code=422, detail="У категории должна быть сторона: доход или расход")
            item = service.ensure_category(session, workspace.id, body.side, body.name)
        elif kind == "counterparties":
            item = service.ensure_counterparty(session, workspace.id, body.name, role=body.role or "client")
        elif kind == "projects":
            item = service.ensure_project(session, workspace.id, body.name)
        else:
            item = service.ensure_tag(session, workspace.id, body.name)
        if item is None:
            raise HTTPException(status_code=422, detail="Пустое название")
        return {"id": str(item.id), "name": item.name}


@router.delete("/dictionaries/{kind}/{item_id}")
def archive_entry(kind: str, item_id: UUID, user=Depends(require_finance_admin)) -> dict[str, bool]:
    """Убрать запись справочника из списков (архив, не удаление)."""
    _guard()
    models = {
        "accounts": Account,
        "categories": Category,
        "counterparties": Counterparty,
        "projects": Project,
        "tags": Tag,
    }
    if kind not in models:
        raise HTTPException(status_code=404, detail="Неизвестный справочник")
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        try:
            service.archive(session, models[kind], workspace.id, item_id)
        except FinanceError as exc:
            raise _fail(exc) from exc
        return {"ok": True}


# ── Журнал ───────────────────────────────────────────────────────────────────


def _operation_out(
    operation: Operation,
    *,
    names: dict[str, dict[UUID, str]],
    splits: dict[UUID, list[Any]],
    tags: dict[UUID, list[UUID]],
) -> dict[str, Any]:
    return {
        "id": str(operation.id),
        "version": operation.version,
        "kind": operation.kind,
        "status": operation.status,
        "paid_at": operation.paid_at.isoformat(),
        "accrued_at": operation.accrued_at.isoformat() if operation.accrued_at else None,
        "period_start": operation.period_start.isoformat() if operation.period_start else None,
        "period_end": operation.period_end.isoformat() if operation.period_end else None,
        "amount": str(operation.amount),
        "currency": operation.currency,
        "amount_base": str(operation.amount_base),
        "account_from": names["accounts"].get(operation.account_from_id, ""),
        "account_to": names["accounts"].get(operation.account_to_id, ""),
        "account_from_id": str(operation.account_from_id) if operation.account_from_id else None,
        "account_to_id": str(operation.account_to_id) if operation.account_to_id else None,
        "category": names["categories"].get(operation.category_id, ""),
        "category_id": str(operation.category_id) if operation.category_id else None,
        "counterparty": names["counterparties"].get(operation.counterparty_id, ""),
        "counterparty_id": str(operation.counterparty_id) if operation.counterparty_id else None,
        "projects": [
            {
                "id": str(split.project_id),
                "name": names["projects"].get(split.project_id, ""),
                "amount": str(split.amount),
            }
            for split in splits.get(operation.id, [])
        ],
        "split_state": operation.split_state,
        "tags": [str(tag_id) for tag_id in tags.get(operation.id, [])],
        "comment": operation.comment,
        "source": operation.source,
        "import_batch_id": str(operation.import_batch_id) if operation.import_batch_id else None,
    }


def _names(session, workspace_id: UUID) -> dict[str, dict[UUID, str]]:
    return {
        "accounts": {item.id: item.name for item in service.list_accounts(session, workspace_id, with_archived=True)},
        "categories": {item.id: item.name for item in service.list_categories(session, workspace_id)},
        "counterparties": {item.id: item.name for item in service.list_counterparties(session, workspace_id)},
        "projects": {item.id: item.name for item in service.list_projects(session, workspace_id)},
    }


def _filter(
    date_from: str | None,
    date_to: str | None,
    kinds: str | None,
    statuses: str | None,
    search: str | None,
    by: str,
    amount_from: str | None,
    amount_to: str | None,
    account_id: UUID | None,
    category_id: UUID | None,
    counterparty_id: UUID | None,
    project_id: UUID | None,
) -> service.OperationFilter:
    def split(value: str | None) -> tuple[str, ...]:
        return tuple(part for part in (value or "").split(",") if part)

    return service.OperationFilter(
        date_from=_parse_date(date_from, field="date_from"),
        date_to=_parse_date(date_to, field="date_to"),
        kinds=split(kinds),
        statuses=split(statuses),
        search=search or "",
        by=by,
        amount_from=_money(amount_from, field="amount_from") if amount_from else None,
        amount_to=_money(amount_to, field="amount_to") if amount_to else None,
        account_ids=(account_id,) if account_id else (),
        category_ids=(category_id,) if category_id else (),
        counterparty_ids=(counterparty_id,) if counterparty_id else (),
        project_ids=(project_id,) if project_id else (),
    )


@router.get("/operations")
def list_operations(
    user=Depends(require_finance),
    date_from: str | None = None,
    date_to: str | None = None,
    kinds: str | None = None,
    statuses: str | None = None,
    search: str | None = None,
    by: str = Query(default="paid", pattern="^(paid|accrued)$"),
    amount_from: str | None = None,
    amount_to: str | None = None,
    account_id: UUID | None = None,
    category_id: UUID | None = None,
    counterparty_id: UUID | None = None,
    project_id: UUID | None = None,
    limit: int = Query(default=250, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Журнал операций с фильтрами."""
    _guard()
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        flt = _filter(
            date_from, date_to, kinds, statuses, search, by, amount_from, amount_to,
            account_id, category_id, counterparty_id, project_id,
        )
        operations, total = service.list_operations(session, workspace.id, flt, limit=limit, offset=offset)
        ids = [operation.id for operation in operations]
        names = _names(session, workspace.id)
        splits = service.operation_projects(session, ids)
        tags = service.operation_tags(session, ids)
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "sums": {
                "income": str(sum((o.amount_base for o in operations if o.kind == "income"), Decimal("0"))),
                "expense": str(sum((o.amount_base for o in operations if o.kind == "expense"), Decimal("0"))),
            },
            "items": [
                _operation_out(operation, names=names, splits=splits, tags=tags)
                for operation in operations
            ],
        }


class ProjectSplitIn(BaseModel):
    project_id: UUID
    amount: str


class OperationIn(BaseModel):
    kind: str = Field(pattern="^(income|expense|transfer)$")
    status: str = Field(default="fact", pattern="^(fact|plan)$")
    paid_at: str
    accrued_at: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    amount: str
    currency: str | None = None
    rate: str | None = None
    account_from_id: UUID | None = None
    account_to_id: UUID | None = None
    category_id: UUID | None = None
    counterparty_id: UUID | None = None
    comment: str = ""
    projects: list[ProjectSplitIn] = Field(default_factory=list)
    tags: list[UUID] = Field(default_factory=list)


@router.post("/operations")
def create_operation(body: OperationIn, user=Depends(require_finance)) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        paid_at = _parse_date(body.paid_at, field="paid_at")
        if paid_at is None:
            raise HTTPException(status_code=422, detail="Без даты платежа операции не бывает")
        data = service.OperationInput(
            kind=body.kind,
            status=body.status,
            paid_at=paid_at,
            accrued_at=_parse_date(body.accrued_at, field="accrued_at"),
            period_start=_parse_date(body.period_start, field="period_start"),
            period_end=_parse_date(body.period_end, field="period_end"),
            amount=_money(body.amount, field="amount"),
            currency=body.currency,
            rate=_money(body.rate, field="rate") if body.rate else None,
            account_from_id=body.account_from_id,
            account_to_id=body.account_to_id,
            category_id=body.category_id,
            counterparty_id=body.counterparty_id,
            comment=body.comment,
            projects=[(item.project_id, _money(item.amount, field="projects")) for item in body.projects],
            tags=list(body.tags),
            source="app",
        )
        try:
            operation = service.create_operation(session, workspace, data, actor=_actor(user))
        except FinanceError as exc:
            raise _fail(exc) from exc
        names = _names(session, workspace.id)
        splits = service.operation_projects(session, [operation.id])
        return _operation_out(operation, names=names, splits=splits, tags={})


class OperationPatch(BaseModel):
    version: int | None = None
    status: str | None = Field(default=None, pattern="^(fact|plan)$")
    paid_at: str | None = None
    accrued_at: str | None = None
    amount: str | None = None
    account_from_id: UUID | None = None
    account_to_id: UUID | None = None
    category_id: UUID | None = None
    counterparty_id: UUID | None = None
    comment: str | None = None
    projects: list[ProjectSplitIn] | None = None
    tags: list[UUID] | None = None


@router.patch("/operations/{operation_id}")
def patch_operation(
    operation_id: UUID, body: OperationPatch, user=Depends(require_finance)
) -> dict[str, Any]:
    _guard()
    changes: dict[str, Any] = {}
    if body.status is not None:
        changes["status"] = body.status
    if body.paid_at is not None:
        changes["paid_at"] = _parse_date(body.paid_at, field="paid_at")
    if body.accrued_at is not None:
        changes["accrued_at"] = _parse_date(body.accrued_at, field="accrued_at")
    if body.amount is not None:
        changes["amount"] = _money(body.amount, field="amount")
    for key in ("account_from_id", "account_to_id", "category_id", "counterparty_id"):
        value = getattr(body, key)
        if value is not None:
            changes[key] = value
    if body.comment is not None:
        changes["comment"] = body.comment
    if body.projects is not None:
        changes["projects"] = [(item.project_id, _money(item.amount, field="projects")) for item in body.projects]
    if body.tags is not None:
        changes["tags"] = list(body.tags)
    if not changes:
        raise HTTPException(status_code=422, detail="Нечего менять")

    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        try:
            operation = service.update_operation(
                session, workspace, operation_id, changes, version=body.version, actor=_actor(user)
            )
        except FinanceError as exc:
            raise _fail(exc) from exc
        names = _names(session, workspace.id)
        splits = service.operation_projects(session, [operation.id])
        tags = service.operation_tags(session, [operation.id])
        return _operation_out(operation, names=names, splits=splits, tags=tags)


@router.delete("/operations/{operation_id}")
def delete_operation(operation_id: UUID, user=Depends(require_finance)) -> dict[str, bool]:
    _guard()
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        try:
            service.delete_operation(session, workspace, operation_id, actor=_actor(user))
        except FinanceError as exc:
            raise _fail(exc) from exc
        return {"ok": True}


# ── Табличный вид ────────────────────────────────────────────────────────────


@router.get("/grid")
def read_grid(
    user=Depends(require_finance),
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Журнал как лист: шапка, строки и значения для выпадающих списков."""
    _guard()
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        flt = service.OperationFilter(
            date_from=_parse_date(date_from, field="date_from"),
            date_to=_parse_date(date_to, field="date_to"),
        )
        operations, total = service.list_operations(
            session, workspace.id, flt, limit=min(limit or finance_settings.grid_max_rows, finance_settings.grid_max_rows)
        )
        payload = grid_module.build_grid(session, workspace, operations)
        payload["total"] = total
        return payload


class CellPatch(BaseModel):
    operation_id: UUID
    column: str
    value: Any = None
    version: int | None = None


@router.patch("/grid/cell")
def patch_cell(body: CellPatch, user=Depends(require_finance)) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        try:
            operation = grid_module.apply_cell(
                session, workspace, body.operation_id, body.column, body.value,
                version=body.version, actor=_actor(user),
            )
        except FinanceError as exc:
            raise _fail(exc) from exc
        names = _names(session, workspace.id)
        splits = service.operation_projects(session, [operation.id])
        tags = service.operation_tags(session, [operation.id])
        return _operation_out(operation, names=names, splits=splits, tags=tags)


class GridRowIn(BaseModel):
    cells: dict[str, Any]


@router.post("/grid/row")
def add_grid_row(body: GridRowIn, user=Depends(require_finance)) -> dict[str, Any]:
    """Новая операция из строки, набранной внизу листа."""
    _guard()
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        try:
            operation = grid_module.append_row(session, workspace, body.cells, actor=_actor(user))
        except FinanceError as exc:
            raise _fail(exc) from exc
        names = _names(session, workspace.id)
        splits = service.operation_projects(session, [operation.id])
        return _operation_out(operation, names=names, splits=splits, tags={})


# ── Отчёты ───────────────────────────────────────────────────────────────────


def _period(date_from: str | None, date_to: str | None) -> tuple[date, date]:
    """Период отчёта. По умолчанию — шесть месяцев, включая текущий целиком.

    Полгода, а не месяц: отчёт из одного столбца не отвечает ни на один
    управленческий вопрос — сравнивать не с чем.

    Про «включая текущий целиком»
    ─────────────────────────────
    Сначала здесь стояло `end = первое число текущего месяца`, и это был
    дефект: операции текущего месяца после первого числа в отчёты не попадали
    вовсе. Экран выглядел исправным — столбец сентября на месте, — но стоял в
    нём ноль, а журнал в это же время показывал одиннадцать операций.
    Поймано сквозным прогоном 18 сентября 2026 на разделе «План/Факт», где
    пустота видна сразу.

    Поэтому конец периода — последний день текущего месяца. Будущие даты внутри
    месяца ничего не портят: плановые платежи в отчётах и так показаны
    отдельными столбцами.
    """
    today = date.today()
    end = _parse_date(date_to, field="date_to")
    if end is None:
        last_day = calendar_module.monthrange(today.year, today.month)[1]
        end = date(today.year, today.month, last_day)
    start = _parse_date(date_from, field="date_from")
    if start is None:
        month = end.month - 5
        year = end.year
        while month <= 0:
            month += 12
            year -= 1
        start = date(year, month, 1)
    if start > end:
        raise HTTPException(status_code=422, detail="Начало периода позже его конца")
    return start, end


@router.get("/reports/cash-flow")
def report_cash_flow(
    user=Depends(require_finance),
    date_from: str | None = None,
    date_to: str | None = None,
    group: str = Query(default="category", pattern="^(category|counterparty|project)$"),
) -> dict[str, Any]:
    _guard()
    start, end = _period(date_from, date_to)
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        return reports.cash_flow(session, workspace.id, start, end, group=group)


@router.get("/reports/profit")
def report_profit(
    user=Depends(require_finance),
    date_from: str | None = None,
    date_to: str | None = None,
    group: str = Query(default="category", pattern="^(category|counterparty|project)$"),
) -> dict[str, Any]:
    _guard()
    start, end = _period(date_from, date_to)
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        return reports.profit_and_loss(session, workspace.id, start, end, group=group)


@router.get("/reports/debts")
def report_debts(user=Depends(require_finance), as_of: str | None = None) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        return reports.debts(session, workspace.id, as_of=_parse_date(as_of, field="as_of"))


@router.get("/reports/projects")
def report_projects(
    user=Depends(require_finance), date_from: str | None = None, date_to: str | None = None
) -> dict[str, Any]:
    _guard()
    start, end = _period(date_from, date_to)
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        return reports.projects_report(session, workspace.id, start, end)


@router.get("/reports/calendar")
def report_calendar(
    user=Depends(require_finance),
    year: int = Query(default=0, ge=0, le=2200),
    month: int = Query(default=0, ge=0, le=12),
) -> dict[str, Any]:
    _guard()
    today = date.today()
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        return reports.calendar(session, workspace.id, year or today.year, month or today.month)


@router.get("/reports/plan-actual")
def report_plan_actual(
    user=Depends(require_finance),
    date_from: str | None = None,
    date_to: str | None = None,
    method: str = Query(default="cash", pattern="^(cash|accrual)$"),
) -> dict[str, Any]:
    _guard()
    start, end = _period(date_from, date_to)
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        return reports.plan_actual(session, workspace.id, start, end, method=method)


class PlanIn(BaseModel):
    month: str
    side: str = Field(pattern="^(income|expense)$")
    method: str = Field(default="cash", pattern="^(cash|accrual)$")
    amount: str
    category_id: UUID | None = None
    project_id: UUID | None = None
    comment: str = ""


@router.post("/plans")
def upsert_plan(body: PlanIn, user=Depends(require_finance)) -> dict[str, Any]:
    """Поставить или изменить план на месяц по статье."""
    _guard()
    month = _parse_date(body.month, field="month")
    if month is None:
        raise HTTPException(status_code=422, detail="Не указан месяц плана")
    month = date(month.year, month.month, 1)
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        existing = session.scalar(
            sa.select(Plan).where(
                Plan.workspace_id == workspace.id,
                Plan.month == month,
                Plan.side == body.side,
                Plan.method == body.method,
                Plan.category_id == body.category_id,
                Plan.project_id == body.project_id,
            )
        )
        amount = _money(body.amount, field="amount")
        if existing is None:
            existing = Plan(
                workspace_id=workspace.id,
                month=month,
                side=body.side,
                method=body.method,
                category_id=body.category_id,
                project_id=body.project_id,
                amount=amount,
                comment=body.comment,
                created_by=_actor(user),
            )
            session.add(existing)
        else:
            existing.amount = amount
            existing.comment = body.comment
        session.flush()
        return {"id": str(existing.id), "month": month.isoformat(), "amount": str(existing.amount)}


# ── Импорт ───────────────────────────────────────────────────────────────────


@router.post("/import/preview")
def import_preview(
    user=Depends(require_finance),
    file: UploadFile = File(...),
    date_order: str | None = Query(default=None, pattern="^(dmy|mdy)$"),
    default_account: str | None = None,
) -> dict[str, Any]:
    """Разобрать файл и показать, что получится. Ничего не записывает в учёт.

    Разбор сохраняется партией импорта: человек может уйти разбираться со
    строками и вернуться, не загружая файл заново.
    """
    _guard()
    data = file.file.read()
    if len(data) > finance_settings.import_max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Файл больше {finance_settings.import_max_mb} МБ — разделите его на части",
        )
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        accounts = [account.name for account in service.list_accounts(session, workspace.id)]
        try:
            preview = analyze(
                data,
                file.filename or "файл",
                accounts,
                date_order=date_order,
                default_account=default_account,
            )
        except ImportError_ as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if len(preview.rows) > finance_settings.import_max_rows:
            raise HTTPException(
                status_code=413,
                detail=f"В файле {len(preview.rows)} строк — потолок {finance_settings.import_max_rows}",
            )
        batch = service.save_preview(session, workspace, preview, actor=_actor(user))
        return {
            "batch_id": str(batch.id),
            "file_name": preview.file_name,
            "header_line": preview.header_line,
            "counts": preview.counts,
            "question": preview.question,
            "date_order": preview.date_reading.order,
            "date_evidence": preview.date_reading.evidence,
            "mapping": preview.mapping,
            "unused_columns": preview.unused_columns,
            "accounts_missing": preview.accounts_missing,
            "rows": [
                {
                    "line": row.line,
                    "state": row.state,
                    "problems": row.problems,
                    "values": row.values,
                    "raw": row.raw,
                }
                for row in preview.rows
            ],
        }


@router.get("/import/batches")
def list_batches(user=Depends(require_finance), limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    """Прошлые загрузки: что за файл, сколько завелось, сколько отложено."""
    _guard()
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        batches = session.scalars(
            sa.select(ImportBatch)
            .where(ImportBatch.workspace_id == workspace.id)
            .order_by(ImportBatch.created_at.desc())
            .limit(limit)
        )
        return {
            "items": [
                {
                    "id": str(batch.id),
                    "file_name": batch.file_name,
                    "status": batch.status,
                    "created_at": batch.created_at.isoformat() if batch.created_at else None,
                    "applied_at": batch.applied_at.isoformat() if batch.applied_at else None,
                    "rows_total": batch.rows_total,
                    "rows_imported": batch.rows_imported,
                    "rows_failed": batch.rows_failed,
                    "rows_skipped": batch.rows_skipped,
                    "rows_duplicate": batch.rows_duplicate,
                    "decisions": batch.decisions,
                }
                for batch in batches
            ]
        }


@router.get("/import/batches/{batch_id}")
def read_batch(batch_id: UUID, user=Depends(require_finance)) -> dict[str, Any]:
    """Строки партии: заведённые, отложенные и почему."""
    _guard()
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        batch = session.get(ImportBatch, batch_id)
        if batch is None or batch.workspace_id != workspace.id:
            raise HTTPException(status_code=404, detail="Загрузка не найдена")
        rows = session.scalars(
            sa.select(ImportRow).where(ImportRow.batch_id == batch.id).order_by(ImportRow.line)
        )
        return {
            "id": str(batch.id),
            "file_name": batch.file_name,
            "status": batch.status,
            "mapping": batch.mapping,
            "decisions": batch.decisions,
            "counts": {
                "total": batch.rows_total,
                "imported": batch.rows_imported,
                "failed": batch.rows_failed,
                "skipped": batch.rows_skipped,
                "duplicate": batch.rows_duplicate,
            },
            "rows": [
                {
                    "line": row.line,
                    "state": row.state,
                    "problems": row.problems,
                    "values": row.parsed,
                    "raw": row.raw,
                    "operation_id": str(row.operation_id) if row.operation_id else None,
                }
                for row in rows
            ],
        }


class ApplyIn(BaseModel):
    #: Пусто — завести все готовые строки. Список — только эти строки.
    lines: list[int] | None = None
    create_dictionaries: bool = True


@router.post("/import/batches/{batch_id}/apply")
def apply_batch(batch_id: UUID, body: ApplyIn, user=Depends(require_finance)) -> dict[str, Any]:
    """Завести готовые строки партии.

    Отложенные строки остаются в партии и не мешают: файл из двухсот строк с
    одной испорченной заводит сто девяносто девять.
    """
    _guard()
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        try:
            return service.apply_batch(
                session,
                workspace,
                batch_id,
                actor=_actor(user),
                only_lines=body.lines,
                create_dictionaries=body.create_dictionaries,
            )
        except FinanceError as exc:
            raise _fail(exc) from exc


class RowFixIn(BaseModel):
    patch: dict[str, Any]


@router.patch("/import/batches/{batch_id}/rows/{line}")
def fix_row(batch_id: UUID, line: int, body: RowFixIn, user=Depends(require_finance)) -> dict[str, Any]:
    """Поправить отложенную строку, не перезагружая файл."""
    _guard()
    with finance_session() as session:
        workspace = service.ensure_workspace(session)
        try:
            row = service.fix_import_row(session, workspace, batch_id, line, body.patch)
        except FinanceError as exc:
            raise _fail(exc) from exc
        return {
            "line": row.line,
            "state": row.state,
            "problems": row.problems,
            "values": row.parsed,
        }
