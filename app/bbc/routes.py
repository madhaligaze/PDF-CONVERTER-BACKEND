"""BBC Dashboard HTTP endpoints (all under /bbc).

Access
    POST   /bbc/auth/login          — sign in, sets the session cookie
    POST   /bbc/auth/logout         — sign out
    GET    /bbc/me                  — who is asking + what they may see
    POST   /bbc/account/credentials — change login/password (admin)
    GET    /bbc/links               — list referral links (admin)
    POST   /bbc/links               — issue a link for one department (admin)
    DELETE /bbc/links/{link_id}     — revoke a link (admin)

Data
    GET    /bbc/status              — is the module enabled/configured (never fails)
    GET    /bbc/sheets              — worksheet list (admin)
    GET    /bbc/snapshot            — headers + rows + metrics (admin)
    POST   /bbc/update              — disabled in v1: sources are read-only
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.bbc import auth as auth_module
from app.bbc import links as links_module
from app.bbc import service
from app.bbc.auth import AuthError, AuthedUser
from app.bbc.config import bbc_settings
from app.bbc.deps import SESSION_COOKIE, current_scope, require_admin, require_block, require_scope
from app.bbc.links import LinkError
from app.bbc.schemas import (
    BbcCredentialsRequest,
    BbcLink,
    BbcLinkCreateRequest,
    BbcLoginRequest,
    BbcMe,
    BbcOk,
    BbcSheetInfo,
    BbcSnapshot,
    BbcStatus,
    BbcUpdateRequest,
    BbcUpdateResult,
)
from app.bbc.scope import Scope
from app.bbc.sheets import BbcError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/bbc")


def _require_configured() -> None:
    if not bbc_settings.configured:
        status = service.get_status()
        raise HTTPException(
            status_code=400,
            detail=status.detail or "BBC Dashboard не настроен",
        )


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


def _me_for_user(user: AuthedUser | None) -> BbcMe:
    if user is None:
        return BbcMe(needs_setup=not auth_module.has_any_user())
    scope = Scope.admin() if user.role == "admin" else Scope.for_departments([])
    return BbcMe(
        authenticated=True,
        username=user.username,
        role=user.role,
        departments=list(scope.departments),
        blocks=list(scope.blocks),
        is_admin=scope.is_admin,
    )


# ── Access ───────────────────────────────────────────────────────────────────────


@router.post("/auth/login", response_model=BbcMe)
async def auth_login(payload: BbcLoginRequest, request: Request, response: Response) -> BbcMe:
    auth_module.ensure_bootstrap_admin()
    try:
        token = auth_module.login(
            payload.username,
            payload.password,
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=int(bbc_settings.session_ttl_hours * 3600),
        path="/",
    )
    return _me_for_user(auth_module.resolve_session(token))


@router.post("/auth/logout", response_model=BbcOk)
async def auth_logout(request: Request, response: Response) -> BbcOk:
    auth_module.logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return BbcOk(detail="Вы вышли")


@router.get("/me", response_model=BbcMe)
async def me(request: Request, scope: Scope = Depends(current_scope)) -> BbcMe:
    """Never 401s — the shell uses this to choose between login and dashboard."""
    auth_module.ensure_bootstrap_admin()
    user = auth_module.resolve_session(request.cookies.get(SESSION_COOKIE))

    if scope.is_admin and user is not None:
        return _me_for_user(user)
    if not scope.sees_nothing:
        # Arrived through a referral link: no account, just a scoped view.
        return BbcMe(
            authenticated=True,
            link_label=scope.label or None,
            departments=list(scope.departments),
            blocks=list(scope.blocks),
        )
    return BbcMe(needs_setup=not auth_module.has_any_user())


@router.post("/account/credentials", response_model=BbcOk)
async def change_credentials(
    payload: BbcCredentialsRequest,
    response: Response,
    user: AuthedUser = Depends(require_admin),
) -> BbcOk:
    try:
        auth_module.change_credentials(
            user.id,
            current_password=payload.current_password,
            new_username=payload.new_username,
            new_password=payload.new_password,
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if payload.new_password:
        # Changing the password drops every session, including this one.
        response.delete_cookie(SESSION_COOKIE, path="/")
        return BbcOk(detail="Пароль изменён, войдите заново")
    return BbcOk(detail="Данные обновлены")


@router.get("/links", response_model=list[BbcLink])
async def list_links(_: AuthedUser = Depends(require_admin)) -> list[BbcLink]:
    return [BbcLink(**vars(item)) for item in links_module.list_links()]


@router.post("/links", response_model=BbcLink)
async def create_link(
    payload: BbcLinkCreateRequest,
    request: Request,
    user: AuthedUser = Depends(require_admin),
) -> BbcLink:
    try:
        view = links_module.create_link(
            payload.department,
            expires_in_hours=payload.expires_in_hours,
            created_by=user.id,
            base_url=bbc_settings.public_base_url or str(request.base_url).rstrip("/"),
        )
    except LinkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BbcLink(**vars(view))


@router.delete("/links/{link_id}", response_model=BbcOk)
async def revoke_link(link_id: str, _: AuthedUser = Depends(require_admin)) -> BbcOk:
    if not links_module.revoke_link(link_id):
        raise HTTPException(status_code=404, detail="Ссылка не найдена")
    return BbcOk(detail="Доступ по ссылке отозван")


# ── Data ─────────────────────────────────────────────────────────────────────────


@router.get("/status", response_model=BbcStatus)
async def status() -> BbcStatus:
    return service.get_status()


@router.get("/revision")
async def revision(_: Scope = Depends(require_scope)) -> dict:
    """Cheap change probe the browser polls. Served from memory, no Google call."""
    return service.get_revision()


@router.get("/dataset")
async def dataset(
    refresh: bool = Query(default=False),
    scope: Scope = Depends(require_scope),
) -> dict:
    """Rows + dimensions + coverage + warnings, narrowed to the caller's scope.

    Filtering happens server-side before serialization: rows outside the scope
    never reach the browser at all, and every aggregate is computed on what is
    left, so totals cannot leak either.
    """
    _require_configured()
    try:
        return service.get_dataset(scope, refresh=refresh)
    except BbcError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/calendar")
async def calendar(
    method: str = Query(default="predictive"),
    scope: Scope = Depends(require_block("calendar")),
) -> dict:
    """Платёжный календарь: договорной / статистический / предиктивный."""
    _require_configured()
    try:
        return service.get_calendar(scope, method)
    except BbcError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/sales")
async def sales_report(
    worksheet: str | None = Query(default=None),
    scope: Scope = Depends(require_block("sales")),
) -> dict:
    """Отдел продаж: план/факт, KPI и бонусы, отдача на маркетинговый канал."""
    _require_configured()
    try:
        return service.get_sales(scope, worksheet)
    except BbcError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/journal")
async def journal(
    group: str = Query(default="counterparty"),
    measure: str = Query(default="outflow"),
    scope: Scope = Depends(require_block("journal")),
) -> dict:
    """Журнал операций и конструктор мини-сводок."""
    _require_configured()
    try:
        return service.get_journal(scope, group, measure)
    except BbcError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/export/msfo")
async def export_msfo(_: AuthedUser = Depends(require_admin)) -> dict:
    """Собрать отчётность по МСФО в Google-таблицу: лист на каждую логику + сверка."""
    _require_configured()
    try:
        return service.export_msfo(Scope.admin())
    except BbcError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/warnings")
async def warnings(scope: Scope = Depends(require_block("warnings"))) -> dict:
    """The «Предупреждения» block on its own."""
    _require_configured()
    try:
        return service.get_warnings(scope)
    except BbcError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/sheets", response_model=list[BbcSheetInfo])
async def sheets(_: AuthedUser = Depends(require_admin)) -> list[BbcSheetInfo]:
    _require_configured()
    try:
        return service.list_sheets()
    except BbcError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/snapshot", response_model=BbcSnapshot)
async def snapshot(
    worksheet: str | None = Query(default=None),
    refresh: bool = Query(default=False),
    _: AuthedUser = Depends(require_admin),
) -> BbcSnapshot:
    """Raw grid of a worksheet — admin only, it carries every department's rows."""
    _require_configured()
    try:
        return service.get_snapshot(worksheet, refresh=refresh)
    except BbcError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/update", response_model=BbcUpdateResult)
async def update(
    request: BbcUpdateRequest,
    _: AuthedUser = Depends(require_admin),
) -> BbcUpdateResult:
    """Kept for contract compatibility; writing to the sources is off in v1.

    The Google credentials now carry `spreadsheets.readonly`, so this would fail
    upstream anyway — an explicit 403 beats a confusing Google API error.
    """
    raise HTTPException(
        status_code=403,
        detail="Запись в исходные таблицы отключена: дашборд работает только на чтение",
    )
