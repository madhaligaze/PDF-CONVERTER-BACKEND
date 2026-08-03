"""BBC Dashboard HTTP endpoints (all under /bbc).

Access
    POST   /bbc/auth/login          — sign in, sets the session cookie
    POST   /bbc/auth/logout         — sign out
    GET    /bbc/me                  — who is asking + what they may see
    POST   /bbc/account/credentials — change login/password (admin)
    GET    /bbc/links               — list referral links (admin)
    POST   /bbc/links               — issue a link for one department (admin)
    PATCH  /bbc/links/{link_id}     — change how long a link lives (admin)
    DELETE /bbc/links/{link_id}     — revoke a link (admin)

Data
    GET    /bbc/status              — is the module enabled/configured (never fails)
    GET    /bbc/sheets              — worksheet list (admin)
    GET    /bbc/snapshot            — headers + rows + metrics (admin)
    POST   /bbc/update              — disabled in v1: sources are read-only
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile

from app.bbc import auth as auth_module
from app.bbc import employees as employees_module
from app.bbc import links as links_module
from app.bbc import service
from app.bbc import storage
from app.bbc import touches as touches_module
from app.bbc.auth import AuthError, AuthedUser
from app.bbc.config import bbc_settings
from app.bbc.deps import (
    LINK_HEADER,
    SESSION_COOKIE,
    current_scope,
    require_admin,
    require_block,
    require_scope,
    require_user,
    scope_for_user,
)
from app.bbc.links import LinkError
from app.bbc.schemas import (
    BbcCredentialsRequest,
    BbcEmployee,
    BbcEmployeeCreated,
    BbcEmployeeRequest,
    BbcLink,
    BbcLinkCreateRequest,
    BbcLinkExpiryRequest,
    BbcLoginRequest,
    BbcMe,
    BbcOk,
    BbcSetPasswordRequest,
    BbcSheetInfo,
    BbcSnapshot,
    BbcStatus,
    BbcTouch,
    BbcTouchOptions,
    BbcTouchRequest,
    BbcUpdateRequest,
    BbcUpdateResult,
)
from app.bbc.scope import BLOCKS, DATA_SCOPES, DEPARTMENTS, Scope
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
    scope = scope_for_user(user)
    return BbcMe(
        authenticated=True,
        username=user.username,
        role=user.role,
        full_name=user.full_name,
        departments=list(scope.departments),
        blocks=list(scope.blocks),
        is_admin=scope.is_admin,
        data_scope=scope.data_scope,
        # Оболочка по этому признаку показывает экран смены пароля вместо
        # дашборда. Область видимости у такой учётки всё равно пустая, так что
        # это подсказка интерфейсу, а не граница доступа.
        must_change_password=user.must_change_password,
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

    # Порядок тот же, что в `deps.current_scope`, и это обязательно: ссылка
    # старше cookie. Иначе админ, открывший чужую ссылку, увидел бы себя вместо
    # неё — и не смог бы проверить, что именно по ней видно.
    # Токен приходит либо заголовком, либо ?k=.
    token = request.headers.get(LINK_HEADER) or request.query_params.get("k")
    if token:
        if scope.sees_nothing:
            return BbcMe(needs_setup=not auth_module.has_any_user())
        link = links_module.describe_token(token)
        return BbcMe(
            authenticated=True,
            link_label=scope.label or None,
            link_expires_at=link.expires_at if link else None,
            departments=list(scope.departments),
            blocks=list(scope.blocks),
        )

    # Учётка отвечает за себя всегда — в том числе когда области видимости ещё
    # нет: сотрудник с невыданным паролем должен увидеть экран смены пароля, а
    # не форму входа, в которую он только что вошёл.
    user = auth_module.resolve_session(request.cookies.get(SESSION_COOKIE))
    if user is not None:
        return _me_for_user(user)
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
async def list_links(request: Request, _: AuthedUser = Depends(require_admin)) -> list[BbcLink]:
    # База обязательна: адрес отсюда копируют и отправляют человеку, а
    # относительный «/bbc-dashboard?k=…» вне этой вкладки никуда не ведёт.
    base_url = bbc_settings.public_base_url or str(request.base_url).rstrip("/")
    return [BbcLink(**vars(item)) for item in links_module.list_links(base_url)]


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


@router.patch("/links/{link_id}", response_model=BbcLink)
async def set_link_expiry(
    link_id: str,
    payload: BbcLinkExpiryRequest,
    request: Request,
    _: AuthedUser = Depends(require_admin),
) -> BbcLink:
    """Make a live link temporary — or permanent again — without reissuing it.

    Reissuing would hand back a different address, so everyone who already has
    the old one would silently lose access. The point of this call is that the
    address survives the change.
    """
    try:
        view = links_module.set_link_expiry(
            link_id,
            expires_in_minutes=payload.expires_in_minutes,
            base_url=bbc_settings.public_base_url or str(request.base_url).rstrip("/"),
        )
    except LinkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if view is None:
        raise HTTPException(status_code=404, detail="Ссылка не найдена")
    return BbcLink(**vars(view))


@router.delete("/links/{link_id}", response_model=BbcOk)
async def revoke_link(link_id: str, _: AuthedUser = Depends(require_admin)) -> BbcOk:
    if not links_module.revoke_link(link_id):
        raise HTTPException(status_code=404, detail="Ссылка не найдена")
    return BbcOk(detail="Доступ по ссылке отозван")


# ── Сотрудники ───────────────────────────────────────────────────────────────────


@router.get("/employees")
async def employees_list(_: AuthedUser = Depends(require_admin)) -> dict:
    return {
        "employees": employees_module.list_employees(),
        "presets": list(employees_module.ROLE_PRESETS),
        "departments": list(DEPARTMENTS),
        "blocks": list(BLOCKS),
        "data_scopes": list(DATA_SCOPES),
    }


@router.get("/employees/aliases")
async def employee_aliases(_: AuthedUser = Depends(require_admin)) -> dict:
    """Написания из колонки «Сотрудник» — чтобы привязать учётку к её клиентам.

    Список, а не свободный ввод: в таблице встречаются «Дана», «Дана Ж.» и
    «Жумабекова Д.», и угадать их с клавиатуры нельзя — можно только отметить.
    """
    _require_configured()
    try:
        return {"names": service.employee_names()}
    except BbcError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/employees", response_model=BbcEmployeeCreated, status_code=201)
async def employees_create(
    payload: BbcEmployeeRequest,
    actor: AuthedUser = Depends(require_admin),
) -> BbcEmployeeCreated:
    try:
        form = employees_module.parse_employee_input(
            full_name=payload.full_name,
            departments=payload.departments,
            blocks=payload.blocks,
            data_scope=payload.data_scope,
            employee_aliases=payload.employee_aliases,
        )
        employee, password = employees_module.create_employee(
            actor, username=payload.username or "", form=form
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BbcEmployeeCreated(employee=BbcEmployee(**employee), temp_password=password)


@router.patch("/employees/{user_id}", response_model=BbcEmployee)
async def employees_update(
    user_id: int,
    payload: BbcEmployeeRequest,
    actor: AuthedUser = Depends(require_admin),
) -> BbcEmployee:
    try:
        form = employees_module.parse_employee_input(
            full_name=payload.full_name,
            departments=payload.departments,
            blocks=payload.blocks,
            data_scope=payload.data_scope,
            employee_aliases=payload.employee_aliases,
        )
        return BbcEmployee(**employees_module.update_employee(actor, user_id, form))
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/employees/{user_id}/reset-password", response_model=BbcEmployeeCreated)
async def employees_reset_password(
    user_id: int,
    actor: AuthedUser = Depends(require_admin),
) -> BbcEmployeeCreated:
    try:
        employee, password = employees_module.reset_password(actor, user_id)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BbcEmployeeCreated(employee=BbcEmployee(**employee), temp_password=password)


@router.post("/employees/{user_id}/dismiss", response_model=BbcEmployee)
async def employees_dismiss(
    user_id: int,
    actor: AuthedUser = Depends(require_admin),
) -> BbcEmployee:
    try:
        return BbcEmployee(**employees_module.dismiss_employee(actor, user_id))
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/employees/{user_id}/restore", response_model=BbcEmployeeCreated)
async def employees_restore(
    user_id: int,
    actor: AuthedUser = Depends(require_admin),
) -> BbcEmployeeCreated:
    try:
        employee, password = employees_module.restore_employee(actor, user_id)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BbcEmployeeCreated(employee=BbcEmployee(**employee), temp_password=password)


@router.delete("/employees/{user_id}", response_model=BbcOk)
async def employees_delete(
    user_id: int,
    actor: AuthedUser = Depends(require_admin),
) -> BbcOk:
    try:
        employees_module.delete_employee(actor, user_id)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BbcOk(detail="Сотрудник удалён")


@router.post("/auth/set-password", response_model=BbcOk)
async def set_password(
    payload: BbcSetPasswordRequest,
    response: Response,
    user: AuthedUser = Depends(require_user),
) -> BbcOk:
    """Смена собственного пароля, в том числе принудительная при первом входе.

    `require_user`, а не `require_scope`: у сотрудника с невыданным паролем
    области видимости нет по определению, и через проверку области он бы сюда
    не прошёл — то есть не смог бы сменить пароль, из-за которого её и нет.
    """
    try:
        employees_module.set_own_password(
            user.id,
            current_password=payload.current_password,
            new_password=payload.new_password,
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    response.delete_cookie(SESSION_COOKIE, path="/")
    return BbcOk(detail="Пароль изменён, войдите заново")


# ── Касания ──────────────────────────────────────────────────────────────────────


@router.get("/touches/options", response_model=BbcTouchOptions)
async def touch_options(_: Scope = Depends(require_block("touches"))) -> BbcTouchOptions:
    return BbcTouchOptions(
        contact_roles=list(touches_module.CONTACT_ROLES),
        channels=list(touches_module.CHANNELS),
        max_file_bytes=storage.MAX_FILE_BYTES,
        max_files=storage.MAX_FILES_PER_TOUCH,
    )


@router.get("/touches", response_model=list[BbcTouch])
async def touches_list(
    client: str | None = Query(default=None),
    author_id: int | None = Query(default=None),
    contact_role: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    scope: Scope = Depends(require_block("touches")),
) -> list[BbcTouch]:
    """Журнал в границах области видимости.

    Видно все касания по своим клиентам — включая чужие. Дана обязана знать,
    что директор туда уже писал, иначе журнал не выполняет ту работу, ради
    которой заведён.
    """
    _require_configured()
    try:
        allowed = service.visible_client_keys(scope)
    except BbcError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return [
        BbcTouch(**item)
        for item in touches_module.list_touches(
            allowed,
            client=client,
            author_id=author_id,
            contact_role=contact_role,
            date_from=date_from,
            date_to=date_to,
        )
    ]


@router.get("/touches/counts")
async def touches_counts(scope: Scope = Depends(require_block("touches"))) -> dict:
    """Карта «клиент → сколько касаний» для значков в реестре дебиторки."""
    _require_configured()
    try:
        allowed = service.visible_client_keys(scope)
    except BbcError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"counts": touches_module.count_by_client(allowed)}


@router.post("/touches", response_model=BbcTouch, status_code=201)
async def touches_create(
    payload: BbcTouchRequest,
    user: AuthedUser = Depends(require_user),
    scope: Scope = Depends(require_block("touches")),
) -> BbcTouch:
    _require_configured()
    try:
        form = touches_module.parse_touch_input(
            client=payload.client,
            contacted_at=payload.contacted_at,
            contact_role=payload.contact_role,
            contact_name=payload.contact_name,
            channel=payload.channel,
            summary=payload.summary,
        )
        _assert_client_visible(scope, form["client_key"])
        return BbcTouch(**touches_module.create_touch(user, form))
    except touches_module.TouchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/touches/{touch_id}", response_model=BbcTouch)
async def touches_update(
    touch_id: int,
    payload: BbcTouchRequest,
    user: AuthedUser = Depends(require_user),
    scope: Scope = Depends(require_block("touches")),
) -> BbcTouch:
    _require_configured()
    try:
        form = touches_module.parse_touch_input(
            client=payload.client,
            contacted_at=payload.contacted_at,
            contact_role=payload.contact_role,
            contact_name=payload.contact_name,
            channel=payload.channel,
            summary=payload.summary,
        )
        _assert_client_visible(scope, form["client_key"])
        return BbcTouch(**touches_module.update_touch(user, touch_id, form))
    except touches_module.TouchError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/touches/{touch_id}", response_model=BbcOk)
async def touches_delete(
    touch_id: int,
    user: AuthedUser = Depends(require_user),
    _: Scope = Depends(require_block("touches")),
) -> BbcOk:
    try:
        touches_module.delete_touch(user, touch_id)
    except touches_module.TouchError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return BbcOk(detail="Касание убрано из журнала")


@router.post("/touches/{touch_id}/files", status_code=201)
async def touches_attach(
    touch_id: int,
    file: UploadFile = File(...),
    user: AuthedUser = Depends(require_user),
    _: Scope = Depends(require_block("touches")),
) -> dict:
    blob = await file.read()
    try:
        return touches_module.attach_file(
            user, touch_id, blob, file.filename or "", file.content_type
        )
    except storage.StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except touches_module.TouchError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/files/{file_id}")
async def touch_file(
    file_id: int,
    scope: Scope = Depends(require_block("touches")),
) -> Response:
    """Отдача файла своим эндпоинтом, а не ссылкой на бакет.

    Скрин переписки о долге не должен открываться по угаданному или
    пересланному адресу — только тому, кому виден сам клиент.
    """
    _require_configured()
    try:
        allowed = service.visible_client_keys(scope)
        blob, content_type, filename = touches_module.read_file(file_id, allowed)
    except BbcError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (touches_module.TouchError, storage.StorageError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    quoted = quote(filename)
    return Response(
        content=blob,
        media_type=content_type,
        headers={
            # inline: скрин открывается в соседней вкладке, а не падает в
            # «Загрузки». Имя — в RFC 5987, иначе кириллица приезжает мусором.
            "Content-Disposition": f"inline; filename*=UTF-8''{quoted}",
            "Cache-Control": "private, max-age=300",
        },
    )


@router.delete("/files/{file_id}", response_model=BbcOk)
async def touch_file_delete(
    file_id: int,
    user: AuthedUser = Depends(require_user),
    _: Scope = Depends(require_block("touches")),
) -> BbcOk:
    try:
        touches_module.delete_file(user, file_id)
    except touches_module.TouchError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return BbcOk(detail="Файл удалён")


def _assert_client_visible(scope: Scope, key: str) -> None:
    """Писать можно только по клиенту, которого видно.

    Иначе сотрудник с областью «только свои» завёл бы касание по чужому
    должнику — и сам же его больше не увидел бы.
    """
    allowed = service.visible_client_keys(scope)
    if allowed is not None and key not in allowed:
        raise touches_module.TouchError("Этот клиент вам не виден")


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
