"""FastAPI dependencies that resolve *who is asking* into a visibility scope.

Every data endpoint must depend on `require_scope` (or `require_admin`). The
resolution order is: referral-link token → admin session cookie → denied.

`Scope.denied()` is the fallback on purpose: a request with no valid credential
gets an empty result set, never the full one.
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Query, Request

from app.bbc import links as links_module
from app.bbc.auth import AuthedUser, resolve_session
from app.bbc.scope import Scope

SESSION_COOKIE = "bbc_session"
# The dashboard URL carries ?k=<token>; the frontend replays it as a header.
LINK_HEADER = "X-BBC-Link"


def current_user(request: Request) -> AuthedUser | None:
    return resolve_session(request.cookies.get(SESSION_COOKIE))


def current_scope(
    request: Request,
    k: str | None = Query(default=None, description="Referral link token"),
    link_header: str | None = Header(default=None, alias=LINK_HEADER),
) -> Scope:
    """Resolve the caller's scope. Never raises — returns `denied()` instead."""
    token = link_header or k
    if token:
        scope = links_module.resolve_link(token)
        # An unknown/revoked/expired link must not silently fall through to the
        # session cookie, or a revoked link would still work for a logged-in admin.
        return scope or Scope.denied()

    user = current_user(request)
    if user is not None:
        return scope_for_user(user)
    return Scope.denied()


def scope_for_user(user: AuthedUser) -> Scope:
    """Область видимости учётки. Единственное место, где роль становится правами.

    Пароль, выданный админом и ещё не сменённый, не открывает ничего: пока
    `must_change_password`, любой запрос за данными получает пустую область.
    Пустить сюда — значит согласиться, что человек ходит по дашборду с паролем,
    который лежит в переписке в WhatsApp. Сменить его при этом можно: эндпоинт
    смены пароля область видимости не спрашивает.
    """
    if user.must_change_password:
        return Scope.denied()
    if user.is_admin:
        return Scope.admin()
    return Scope.for_employee(
        user_id=user.id,
        departments=user.departments,
        blocks=user.blocks,
        data_scope=user.data_scope,
        employee_aliases=user.employee_aliases,
        label=user.display_name,
    )


def require_scope(scope: Scope = Depends(current_scope)) -> Scope:
    """Scope that is allowed to see at least something."""
    if scope.sees_nothing:
        raise HTTPException(status_code=401, detail="Требуется вход или действующая ссылка")
    return scope


def require_admin(request: Request) -> AuthedUser:
    """Admin-only endpoints: account page, link and employee management."""
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Требуется вход")
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return user


def require_user(request: Request) -> AuthedUser:
    """Любой вошедший — админ или сотрудник.

    В отличие от `require_scope` не смотрит на область видимости: нужен там, где
    важен сам факт «кто спрашивает», а не «что ему видно». Прежде всего — смена
    пароля: сотрудник с `must_change_password` области видимости не имеет ровно
    до тех пор, пока не сменит пароль, и через `require_scope` не прошёл бы.
    """
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Требуется вход")
    return user


def require_block(block: str):
    """Guard a block-specific endpoint (`Depends(require_block("reports"))`)."""

    def _dependency(scope: Scope = Depends(require_scope)) -> Scope:
        if not scope.allows_block(block):
            raise HTTPException(status_code=403, detail="Этот раздел недоступен по вашей ссылке")
        return scope

    return _dependency


__all__ = [
    "LINK_HEADER",
    "SESSION_COOKIE",
    "current_scope",
    "current_user",
    "require_admin",
    "require_block",
    "require_scope",
    "require_user",
    "scope_for_user",
]
