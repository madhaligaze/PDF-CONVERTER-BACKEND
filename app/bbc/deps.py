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

#: Токен ссылки, положенный в HttpOnly-cookie ради отдачи файлов.
#:
#: `<img src>` и `<a href>` заголовок `X-BBC-Link` поставить не могут, и токен
#: приходилось возвращать в адрес файла (`?k=…`). Оттуда он уезжал в историю
#: браузера, в Referer, в логи прокси и в «копировать адрес картинки» — а это
#: секрет на весь отдел: дебиторка, касания, календарь. Ссылки по умолчанию
#: бессрочные, так что утёкший адрес скрина открывал их до отзыва ссылки.
#:
#: Cookie читает только отдача файла (`file_scope`), а не `current_scope`:
#: забытая cookie не должна подменять область видимости админа на его же
#: экране. Границу держит код, а не путь cookie, — путь ломался бы о прокси.
FILE_COOKIE = "bbc_file_key"

#: Окружения, где `Secure` на cookie означал бы «войти нельзя»: локальный
#: запуск и тесты ходят по http, и cookie с этим флагом браузер бы не сохранил.
_INSECURE_ENVIRONMENTS = frozenset({"development", "dev", "local", "test", "testing"})


def cookie_secure(request: Request) -> bool:
    """Ставить ли `Secure` на cookie сессии.

    Не по схеме входящего запроса, как было. Браузер приходит на Next по HTTPS,
    Next проксирует на API по HTTP, и бэкенд видит `http://api:8000` — то есть
    схема здесь описывает внутреннюю сеть, а не то, как ходит человек. Cookie
    админа с тридцатидневным окном уезжала в браузер без `Secure`, и `SameSite`
    её не защищает: он закрывает CSRF, а не кражу по открытому HTTP.

    Поэтому решает окружение, а схема и `X-Forwarded-Proto` могут только
    добавить флаг, но не снять: ошибиться в сторону `Secure` безопасно, в
    обратную — нет.
    """
    if request.url.scheme == "https":
        return True
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    if forwarded.lower() == "https":
        return True
    from app.core.config import settings

    return (settings.environment or "").strip().lower() not in _INSECURE_ENVIRONMENTS


def current_user(request: Request) -> AuthedUser | None:
    user = resolve_session(request.cookies.get(SESSION_COOKIE))
    if user is not None:
        # Отметка для прослойки `bbc.middleware`: она продлит cookie ровно
        # тогда, когда сессия действительно жива. Держать это здесь, а не
        # спрашивать базу второй раз в прослойке, — единственное чтение сессии
        # на запрос остаётся единственным.
        request.state.bbc_session_alive = True
    return user


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

    За данными ходить через него нельзя — см. `require_active_user`.
    """
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Требуется вход")
    return user


def require_active_user(request: Request) -> AuthedUser:
    """Вошедший, чей временный пароль уже сменён. Дверь ко всему, что есть данные.

    `scope_for_user` закрывает `must_change_password` для всего, что ходит через
    область видимости, — а `require_user` этого флага не видел, и раздел «Книги»
    открывался паролем, который лежит в переписке в WhatsApp. Один флаг должен
    закрывать все двери, поэтому проверка стоит здесь, а не в каждом маршруте.
    """
    user = require_user(request)
    if user.must_change_password:
        raise HTTPException(
            status_code=401,
            detail="Сначала смените временный пароль",
        )
    return user


def require_block(block: str):
    """Guard a block-specific endpoint (`Depends(require_block("reports"))`)."""

    def _dependency(scope: Scope = Depends(require_scope)) -> Scope:
        if not scope.allows_block(block):
            raise HTTPException(status_code=403, detail="Этот раздел недоступен по вашей ссылке")
        return scope

    return _dependency


def file_scope(
    request: Request,
    link_header: str | None = Header(default=None, alias=LINK_HEADER),
) -> Scope:
    """Область видимости для отдачи файла касания.

    Отличается от `current_scope` ровно одним: `?k=` здесь не читается. Ради
    этого всё и сделано — токен ссылки не должен появляться в адресе картинки.
    Порядок прежний: заголовок (обычные запросы фронта), затем cookie ссылки
    (её ставит `/files/access` — оттуда её берёт `<img src>`), затем сессия.
    """
    token = link_header or request.cookies.get(FILE_COOKIE)
    if token:
        return links_module.resolve_link(token) or Scope.denied()
    user = current_user(request)
    return scope_for_user(user) if user is not None else Scope.denied()


def require_file_scope(scope: Scope = Depends(file_scope)) -> Scope:
    """Кому можно отдать файл: область видимости есть и журнал касаний ей открыт."""
    if scope.sees_nothing:
        raise HTTPException(status_code=401, detail="Требуется вход или действующая ссылка")
    if not scope.allows_block("touches"):
        raise HTTPException(status_code=403, detail="Этот раздел недоступен по вашей ссылке")
    return scope


def require_block_user(block: str):
    """Учётка с правом на раздел — в отличие от `require_block`, не ссылка.

    Разделу «Книги» мало области видимости: там пишут, и запись надо кем-то
    подписать. У ссылки отдела автора нет, поэтому её сюда не пускаем вовсе, а
    не полагаемся на то, что фронт не покажет кнопку.
    """

    def _dependency(request: Request) -> AuthedUser:
        user = require_active_user(request)
        if not scope_for_user(user).allows_block(block):
            raise HTTPException(status_code=403, detail="Этот раздел вам не открыт")
        return user

    return _dependency


__all__ = [
    "FILE_COOKIE",
    "LINK_HEADER",
    "SESSION_COOKIE",
    "cookie_secure",
    "current_scope",
    "current_user",
    "file_scope",
    "require_active_user",
    "require_file_scope",
    "require_admin",
    "require_block",
    "require_block_user",
    "require_scope",
    "require_user",
    "scope_for_user",
]
