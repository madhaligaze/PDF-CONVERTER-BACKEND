"""HTTP-маршруты раздела «Финансы».

Своя авторизация, а не общая с дашбордом
────────────────────────────────────────
Раньше здесь стояли `require_block_user("finance")` и `require_admin` из
`app.bbc.deps`, то есть в раздел пускали учётки BBC Dashboard. Это было неверно
по существу: в «Финансах» каждая компания регистрируется сама и ведёт свой
учёт, к BBC отношения не имея, — а BBC однажды может сам переехать сюда, и
тогда зависимость смотрела бы от общего к частному.

Теперь у раздела свои учётки (`app.finance.auth`), и `app.bbc` этот файл не
импортирует вовсе. Компания берётся **из сессии**, а не «по умолчанию»: один
человек ведёт несколько компаний, и переключатель в шапке меняет контекст
сессии.

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
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field

from app.finance import auth, grid as grid_module, reports, rules, service, sheets
from app.finance.auth import AuthError, Member
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

def _guard() -> None:
    if not finance_settings.enabled:
        raise HTTPException(status_code=404, detail="Раздел «Финансы» выключен")


#: Окружения, где `Secure` на cookie означал бы «войти нельзя»: локальный
#: запуск и тесты ходят по http. Тот же список и та же причина, что у дашборда.
_INSECURE_ENVIRONMENTS = frozenset({"development", "dev", "local", "test", "testing"})


def _cookie_secure(request: Request) -> bool:
    """Ставить ли `Secure` на cookie сессии.

    Решает окружение, а схема запроса и `X-Forwarded-Proto` могут только
    добавить флаг, но не снять: браузер приходит на Next по HTTPS, Next
    проксирует на API по HTTP, и бэкенд видит `http://api:8000` — то есть схема
    здесь описывает внутреннюю сеть, а не то, как ходит человек. Ошибиться в
    сторону `Secure` безопасно, в обратную — нет.
    """
    if request.url.scheme == "https":
        return True
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    if forwarded.lower() == "https":
        return True
    from app.core.config import settings

    return (settings.environment or "").strip().lower() not in _INSECURE_ENVIRONMENTS


def _set_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        auth.COOKIE_NAME,
        token,
        max_age=int(auth.SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )


def current_member(request: Request) -> Member:
    """Вошедший в «Финансы». Нет сессии — 401 с текстом, а не пустой экран."""
    _guard()
    with finance_session() as session:
        member = auth.resolve(session, request.cookies.get(auth.COOKIE_NAME))
    if member is None:
        raise HTTPException(status_code=401, detail="Войдите в «Финансы»")
    return member


def require_ability(ability: str):
    """Зависимость «этому можно вот это».

    Права спрашиваются по способности (`write`, `accounts`, `people`), а не по
    названию роли: добавление роли не должно требовать правки каждого маршрута.
    """

    def _dependency(member: Member = Depends(current_member)) -> Member:
        if member.must_change_password and ability != "read":
            raise HTTPException(status_code=403, detail="Сначала смените временный пароль")
        if not member.can(ability):
            raise HTTPException(
                status_code=403,
                detail={
                    "read": "Этот раздел вам не открыт",
                    "write": "Ваша роль позволяет только смотреть",
                    "accounts": "Счета заводит владелец или администратор",
                    "people": "Людей добавляет владелец или администратор",
                    "company": "Это может только владелец компании",
                }.get(ability, "Недостаточно прав"),
            )
        return member

    return _dependency


def _workspace(session, member: Member):
    """Компания текущей сессии.

    Отсутствие выбранной компании — не ошибка сервера: так бывает у человека,
    которого исключили из компании, пока он был в разделе. Отвечаем 409 и
    текстом, по которому фронт покажет выбор компании.
    """
    if member.workspace_id is None:
        raise HTTPException(status_code=409, detail="Выберите компанию")
    try:
        return service.get_workspace(session, member.workspace_id)
    except FinanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _actor(member: Any) -> str:
    """Подпись под операцией — почта, а не имя: имя меняют, почта это логин."""
    return getattr(member, "email", "") or ""


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


# ── Учётки и компании ────────────────────────────────────────────────────────
#
# Раздел живёт сам по себе: регистрация открыта, компания создаётся вместе с
# первым пользователем, он её владелец. Ни одной проверки прав дашборда здесь
# нет и не должно появиться.


class RegisterIn(BaseModel):
    email: str
    password: str
    company: str
    full_name: str = ""


class LoginIn(BaseModel):
    email: str
    password: str


def _me_payload(member: Member, companies: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "authenticated": True,
        "user": {
            "id": str(member.user_id),
            "email": member.email,
            "full_name": member.full_name,
            "must_change_password": member.must_change_password,
        },
        "company": (
            {"id": str(member.workspace_id), "title": member.workspace_title, "role": member.role}
            if member.workspace_id
            else None
        ),
        "companies": companies,
        "abilities": sorted(
            ability
            for ability in ("read", "write", "accounts", "people", "company")
            if member.can(ability)
        ),
    }


@router.post("/auth/register", status_code=201)
def auth_register(body: RegisterIn, request: Request, response: Response) -> dict[str, Any]:
    """Регистрация компании: почта, пароль, название — и человек внутри."""
    _guard()
    with finance_session() as session:
        try:
            member, token = auth.register(
                session,
                email=body.email,
                password=body.password,
                company=body.company,
                full_name=body.full_name,
                user_agent=request.headers.get("user-agent", ""),
            )
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload = _me_payload(member, auth.companies_of(session, member.user_id))
    _set_cookie(request, response, token)
    return payload


@router.post("/auth/login")
def auth_login(body: LoginIn, request: Request, response: Response) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        try:
            member, token = auth.login(
                session,
                email=body.email,
                password=body.password,
                user_agent=request.headers.get("user-agent", ""),
            )
        except AuthError as exc:
            # 401, а не 400: фронт по коду решает, показывать форму входа снова
            # или сообщение о недостатке прав.
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        payload = _me_payload(member, auth.companies_of(session, member.user_id))
    _set_cookie(request, response, token)
    return payload


@router.post("/auth/logout")
def auth_logout(request: Request, response: Response) -> dict[str, bool]:
    _guard()
    with finance_session() as session:
        auth.logout(session, request.cookies.get(auth.COOKIE_NAME))
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    """Кто вошёл. Отдаёт `authenticated: false` вместо 401.

    Этот маршрут спрашивают при открытии страницы, и 401 в консоли браузера на
    каждом заходе гостя — шум, из которого потом не видно настоящих ошибок.
    """
    _guard()
    with finance_session() as session:
        member = auth.resolve(session, request.cookies.get(auth.COOKIE_NAME))
        if member is None:
            return {"authenticated": False}
        return _me_payload(member, auth.companies_of(session, member.user_id))


class SwitchIn(BaseModel):
    company_id: UUID


@router.post("/auth/switch")
def auth_switch(body: SwitchIn, request: Request) -> dict[str, Any]:
    """Сменить компанию в текущей сессии."""
    _guard()
    token = request.cookies.get(auth.COOKIE_NAME)
    with finance_session() as session:
        try:
            member = auth.switch_company(session, token or "", body.company_id)
        except AuthError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return _me_payload(member, auth.companies_of(session, member.user_id))


class CompanyIn(BaseModel):
    title: str


@router.post("/auth/companies", status_code=201)
def auth_add_company(
    body: CompanyIn, member: Member = Depends(current_member)
) -> dict[str, Any]:
    """Ещё одна компания тому же человеку — он её владелец."""
    _guard()
    with finance_session() as session:
        try:
            created = auth.add_company(session, member, title=body.title)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return created


@router.patch("/auth/company")
def auth_rename_company(
    body: CompanyIn, member: Member = Depends(require_ability("company"))
) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            service.rename_workspace(session, workspace, title=body.title)
        except FinanceError as exc:
            raise _fail(exc) from exc
        return {"id": str(workspace.id), "title": workspace.title}


class InviteIn(BaseModel):
    email: str
    password: str
    role: str = "accountant"
    full_name: str = ""


@router.get("/auth/members")
def auth_members(member: Member = Depends(require_ability("people"))) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return {"items": auth.members_of(session, workspace.id)}


@router.post("/auth/members", status_code=201)
def auth_invite(
    body: InviteIn, member: Member = Depends(require_ability("people"))
) -> dict[str, Any]:
    """Добавить человека в компанию с временным паролем."""
    _guard()
    with finance_session() as session:
        try:
            return auth.invite(
                session,
                member,
                email=body.email,
                role=body.role,
                full_name=body.full_name,
                password=body.password,
            )
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


class RoleIn(BaseModel):
    role: str


@router.patch("/auth/members/{user_id}")
def auth_change_role(
    user_id: UUID, body: RoleIn, member: Member = Depends(require_ability("people"))
) -> dict[str, bool]:
    _guard()
    with finance_session() as session:
        try:
            auth.change_role(session, member, user_id=user_id, role=body.role)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}


@router.delete("/auth/members/{user_id}")
def auth_remove_member(
    user_id: UUID, member: Member = Depends(require_ability("people"))
) -> dict[str, bool]:
    _guard()
    with finance_session() as session:
        try:
            auth.remove_member(session, member, user_id=user_id)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}


class PasswordIn(BaseModel):
    old_password: str
    new_password: str


@router.post("/auth/password")
def auth_password(
    body: PasswordIn, member: Member = Depends(current_member)
) -> dict[str, bool]:
    """Смена своего пароля. Доступна и тем, у кого пароль временный."""
    _guard()
    with finance_session() as session:
        try:
            auth.set_password(session, member, old=body.old_password, new=body.new_password)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}


@router.get("/auth/sessions")
def auth_sessions(member: Member = Depends(current_member)) -> dict[str, Any]:
    """Свои открытые сессии — чтобы увидеть чужой вход и отозвать его."""
    _guard()
    with finance_session() as session:
        return {"items": auth.sessions_of(session, member.user_id)}


@router.delete("/auth/sessions/{session_id}")
def auth_revoke_session(
    session_id: UUID, member: Member = Depends(current_member)
) -> dict[str, bool]:
    _guard()
    with finance_session() as session:
        try:
            auth.revoke_session(session, member, session_id)
        except AuthError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}


# ── Обзор ────────────────────────────────────────────────────────────────────


@router.get("/overview")
def overview(member: Member = Depends(current_member)) -> dict[str, Any]:
    """Первый экран: счета, остатки и ближайшие ожидания.

    Один запрос, а не четыре: раздел открывают десятки раз в день, и каждый
    лишний круг до сервера человек ощущает как «подвисло».
    """
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
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
def dictionaries(member: Member = Depends(current_member)) -> dict[str, Any]:
    """Все справочники разом — ими наполняются выпадающие списки форм."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
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
def create_account(body: AccountIn, member: Member = Depends(require_ability("accounts"))) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
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
def create_entry(kind: str, body: EntryIn, member: Member = Depends(require_ability("write"))) -> dict[str, Any]:
    """Создать статью, контрагента, проект или тег.

    Счёт сюда не попадает намеренно: он требует прав администратора и заводится
    своим маршрутом выше.
    """
    _guard()
    if kind not in ("categories", "counterparties", "projects", "tags"):
        raise HTTPException(status_code=404, detail="Неизвестный справочник")
    with finance_session() as session:
        workspace = _workspace(session, member)
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
def archive_entry(kind: str, item_id: UUID, member: Member = Depends(require_ability("accounts"))) -> dict[str, bool]:
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
        workspace = _workspace(session, member)
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
    member: Member = Depends(current_member),
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
        workspace = _workspace(session, member)
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
def create_operation(body: OperationIn, member: Member = Depends(require_ability("write"))) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
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
            operation = service.create_operation(session, workspace, data, actor=_actor(member))
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
    operation_id: UUID, body: OperationPatch, member: Member = Depends(require_ability("write"))
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
        workspace = _workspace(session, member)
        try:
            operation = service.update_operation(
                session, workspace, operation_id, changes, version=body.version, actor=_actor(member)
            )
        except FinanceError as exc:
            raise _fail(exc) from exc
        names = _names(session, workspace.id)
        splits = service.operation_projects(session, [operation.id])
        tags = service.operation_tags(session, [operation.id])
        return _operation_out(operation, names=names, splits=splits, tags=tags)


@router.delete("/operations/{operation_id}")
def delete_operation(operation_id: UUID, member: Member = Depends(require_ability("write"))) -> dict[str, bool]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            service.delete_operation(session, workspace, operation_id, actor=_actor(member))
        except FinanceError as exc:
            raise _fail(exc) from exc
        return {"ok": True}


# ── Табличный вид ────────────────────────────────────────────────────────────


@router.get("/grid")
def read_grid(
    member: Member = Depends(current_member),
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Журнал как лист: шапка, строки и значения для выпадающих списков."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
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
def patch_cell(body: CellPatch, member: Member = Depends(require_ability("write"))) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            operation = grid_module.apply_cell(
                session, workspace, body.operation_id, body.column, body.value,
                version=body.version, actor=_actor(member),
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
def add_grid_row(body: GridRowIn, member: Member = Depends(require_ability("write"))) -> dict[str, Any]:
    """Новая операция из строки, набранной внизу листа."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            operation = grid_module.append_row(session, workspace, body.cells, actor=_actor(member))
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
    member: Member = Depends(current_member),
    date_from: str | None = None,
    date_to: str | None = None,
    group: str = Query(default="category", pattern="^(category|counterparty|project)$"),
) -> dict[str, Any]:
    _guard()
    start, end = _period(date_from, date_to)
    with finance_session() as session:
        workspace = _workspace(session, member)
        return reports.cash_flow(session, workspace.id, start, end, group=group)


@router.get("/reports/profit")
def report_profit(
    member: Member = Depends(current_member),
    date_from: str | None = None,
    date_to: str | None = None,
    group: str = Query(default="category", pattern="^(category|counterparty|project)$"),
) -> dict[str, Any]:
    _guard()
    start, end = _period(date_from, date_to)
    with finance_session() as session:
        workspace = _workspace(session, member)
        return reports.profit_and_loss(session, workspace.id, start, end, group=group)


@router.get("/reports/debts")
def report_debts(member: Member = Depends(current_member), as_of: str | None = None) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return reports.debts(session, workspace.id, as_of=_parse_date(as_of, field="as_of"))


@router.get("/reports/projects")
def report_projects(
    member: Member = Depends(current_member), date_from: str | None = None, date_to: str | None = None
) -> dict[str, Any]:
    _guard()
    start, end = _period(date_from, date_to)
    with finance_session() as session:
        workspace = _workspace(session, member)
        return reports.projects_report(session, workspace.id, start, end)


@router.get("/reports/calendar")
def report_calendar(
    member: Member = Depends(current_member),
    year: int = Query(default=0, ge=0, le=2200),
    month: int = Query(default=0, ge=0, le=12),
) -> dict[str, Any]:
    _guard()
    today = date.today()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return reports.calendar(session, workspace.id, year or today.year, month or today.month)


@router.get("/reports/plan-actual")
def report_plan_actual(
    member: Member = Depends(current_member),
    date_from: str | None = None,
    date_to: str | None = None,
    method: str = Query(default="cash", pattern="^(cash|accrual)$"),
) -> dict[str, Any]:
    _guard()
    start, end = _period(date_from, date_to)
    with finance_session() as session:
        workspace = _workspace(session, member)
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
def upsert_plan(body: PlanIn, member: Member = Depends(require_ability("write"))) -> dict[str, Any]:
    """Поставить или изменить план на месяц по статье."""
    _guard()
    month = _parse_date(body.month, field="month")
    if month is None:
        raise HTTPException(status_code=422, detail="Не указан месяц плана")
    month = date(month.year, month.month, 1)
    with finance_session() as session:
        workspace = _workspace(session, member)
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
                created_by=_actor(member),
            )
            session.add(existing)
        else:
            existing.amount = amount
            existing.comment = body.comment
        session.flush()
        return {"id": str(existing.id), "month": month.isoformat(), "amount": str(existing.amount)}



# ── Автоправила ──────────────────────────────────────────────────────────────


class RuleIn(BaseModel):
    name: str
    conditions: list[dict[str, Any]]
    actions: dict[str, Any]
    match: str = "all"


@router.get("/rules")
def list_rules(member: Member = Depends(current_member)) -> dict[str, Any]:
    """Правила компании вместе со счётчиком срабатываний.

    Счётчик важнее, чем кажется: правило, которое не совпало ни разу, выглядит
    работающим, и человек уверен, что разметка идёт.
    """
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return {
            "items": [
                {
                    "id": str(rule.id),
                    "name": rule.name,
                    "active": rule.active,
                    "match": rule.match,
                    "conditions": rule.conditions,
                    "actions": rule.actions,
                    "applied_count": rule.applied_count,
                    "position": rule.position,
                }
                for rule in rules.list_rules(session, workspace.id)
            ]
        }


@router.post("/rules", status_code=201)
def create_rule(body: RuleIn, member: Member = Depends(require_ability("write"))) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            rule = rules.create_rule(
                session,
                workspace.id,
                name=body.name,
                conditions=body.conditions,
                actions=body.actions,
                match=body.match,
                actor=_actor(member),
            )
        except rules.RuleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": str(rule.id), "name": rule.name}


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: UUID, member: Member = Depends(require_ability("write"))) -> dict[str, bool]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            rules.delete_rule(session, workspace.id, rule_id)
        except rules.RuleError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}


class RuleToggleIn(BaseModel):
    active: bool


@router.patch("/rules/{rule_id}")
def toggle_rule(
    rule_id: UUID, body: RuleToggleIn, member: Member = Depends(require_ability("write"))
) -> dict[str, Any]:
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            rule = rules.toggle_rule(session, workspace.id, rule_id, body.active)
        except rules.RuleError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"id": str(rule.id), "active": rule.active}


class RuleApplyIn(BaseModel):
    #: По умолчанию трогаем только неразмеченное: переразметка задним числом
    #: меняет отчёты, которые человек уже видел и, возможно, кому-то показал.
    only_uncategorized: bool = True


@router.post("/rules/apply")
def apply_rules(
    body: RuleApplyIn, member: Member = Depends(require_ability("write"))
) -> dict[str, Any]:
    """Применить правила к уже заведённым операциям."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return rules.apply_to_operations(
            session, workspace.id, only_uncategorized=body.only_uncategorized
        )


@router.get("/rules/suggest")
def suggest_rules(member: Member = Depends(current_member)) -> dict[str, Any]:
    """С чего начать разметку: частые слова в неразмеченных операциях."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        return {"items": rules.suggest(session, workspace.id)}


# ── Импорт ───────────────────────────────────────────────────────────────────


@router.post("/import/preview")
def import_preview(
    member: Member = Depends(require_ability("write")),
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
        workspace = _workspace(session, member)
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
        # Разметка правилами — ДО записи. Человек видит «412 строк лягут в
        # „Продукты“ по правилу „Magnum“» и может передумать; у соседей по
        # рынку правила срабатывают уже после того, как операции заведены.
        rule_hits = rules.preview_rows(
            rules.list_rules(session, workspace.id, only_active=True),
            [row.values for row in preview.rows],
        )
        return _preview_response(session, workspace, preview, rule_hits, member)


def _preview_response(
    session: Any,
    workspace: Any,
    preview: Any,
    rule_hits: dict[str, int],
    member: Member,
) -> dict[str, Any]:
    """Ответ предпросмотра — один и тот же для файла и для книги Google.

    Собран отдельной функцией не ради краткости: два ответа с разным набором
    полей значили бы, что экран импорта показывает про книгу меньше, чем про
    файл, — и «сколько строк отложено» стало бы зависеть от того, откуда данные.
    """
    batch = service.save_preview(session, workspace, preview, actor=_actor(member))
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
        "rules_applied": rule_hits,
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


# ── Книги Google ────────────────────────────────────────────────────────────


@router.get("/sheets/books")
def sheets_books(member: Member = Depends(current_member)) -> dict[str, Any]:
    """Книги Google, открытые сервисному аккаунту программы.

    Неготовность доступа — не ошибка экрана: `configured: false` показывается
    объяснением, что книгу нужно открыть сервисному аккаунту, а не отказом.
    """
    _guard()
    if not sheets.is_configured():
        return {"configured": False, "items": []}
    try:
        return {"configured": True, "items": sheets.books()}
    except sheets.SheetsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/sheets/books/{book_id}")
def sheets_book(book_id: str, member: Member = Depends(current_member)) -> dict[str, Any]:
    """Вкладки книги и ссылка на неё саму."""
    _guard()
    try:
        return sheets.tabs(book_id)
    except sheets.SheetsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class SheetImportIn(BaseModel):
    book_id: str
    tab: str
    date_order: str | None = Field(default=None, pattern="^(dmy|mdy)$")
    default_account: str | None = None


@router.post("/sheets/preview")
def sheets_preview(
    body: SheetImportIn,
    member: Member = Depends(require_ability("write")),
) -> dict[str, Any]:
    """Разобрать вкладку книги — тем же разбором, что и загруженный файл."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        accounts = [account.name for account in service.list_accounts(session, workspace.id)]
        try:
            preview = sheets.preview_tab(
                body.book_id,
                body.tab,
                accounts,
                date_order=body.date_order,
                default_account=body.default_account,
            )
        except ImportError_ as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except sheets.SheetsError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if len(preview.rows) > finance_settings.import_max_rows:
            raise HTTPException(
                status_code=413,
                detail=f"Во вкладке {len(preview.rows)} строк — потолок {finance_settings.import_max_rows}",
            )
        rule_hits = rules.preview_rows(
            rules.list_rules(session, workspace.id, only_active=True),
            [row.values for row in preview.rows],
        )
        return _preview_response(session, workspace, preview, rule_hits, member)


@router.get("/import/batches")
def list_batches(member: Member = Depends(current_member), limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    """Прошлые загрузки: что за файл, сколько завелось, сколько отложено."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
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
def read_batch(batch_id: UUID, member: Member = Depends(current_member)) -> dict[str, Any]:
    """Строки партии: заведённые, отложенные и почему."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
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
def apply_batch(batch_id: UUID, body: ApplyIn, member: Member = Depends(require_ability("write"))) -> dict[str, Any]:
    """Завести готовые строки партии.

    Отложенные строки остаются в партии и не мешают: файл из двухсот строк с
    одной испорченной заводит сто девяносто девять.
    """
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
        try:
            return service.apply_batch(
                session,
                workspace,
                batch_id,
                actor=_actor(member),
                only_lines=body.lines,
                create_dictionaries=body.create_dictionaries,
            )
        except FinanceError as exc:
            raise _fail(exc) from exc


class RowFixIn(BaseModel):
    patch: dict[str, Any]


@router.patch("/import/batches/{batch_id}/rows/{line}")
def fix_row(batch_id: UUID, line: int, body: RowFixIn, member: Member = Depends(require_ability("write"))) -> dict[str, Any]:
    """Поправить отложенную строку, не перезагружая файл."""
    _guard()
    with finance_session() as session:
        workspace = _workspace(session, member)
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
