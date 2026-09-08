"""Продление cookie сессии на каждом обращении.

Зачем это нужно отдельно от базы
─────────────────────────────────
Срок жизни сессии живёт в двух местах сразу, и они обязаны совпадать. В базе —
`expires_at`, его сдвигает `auth.resolve_session`. В браузере — `Max-Age`
cookie, и он ставится один раз, при входе.

Если сдвигать только базу, получается тихая нелепость: сессия в базе жива и
продлевается хоть год, а cookie в браузере умирает ровно через окно от входа.
Человек, работающий каждый день, всё равно однажды утром видит форму входа — и
причина этого не видна ниоткуда.

Почему прослойкой, а не в зависимости маршрута
──────────────────────────────────────────────
Класть `Response` в зависимость и ставить cookie там — короче, но работает не
везде: обработчики, возвращающие собственный `Response` (выгрузка файла,
например), заголовки зависимости не подхватывают. Прослойка стоит после всех
обработчиков и видит настоящий ответ.

Лишних запросов к базе она не делает: признак «сессия жива» ставит
`deps.current_user`, который и так читает её на каждом защищённом маршруте.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

from app.bbc.auth import session_window
from app.bbc.deps import SESSION_COOKIE, cookie_secure

#: Флаг на `request.state`: сессия по cookie была прочитана и оказалась живой.
SESSION_ALIVE = "bbc_session_alive"


def _already_decided(response: Response) -> bool:
    """Обработчик сам распорядился этой cookie — не трогаем.

    Единственный по-настоящему важный случай — выход. `/auth/logout` и обрыв
    собственного захода из кабинета удаляют cookie; продлив её следом, мы бы
    воскресили только что завершённую сессию, и «Выйти» перестало бы работать.
    """
    prefix = f"{SESSION_COOKIE}=".encode()
    return any(
        name.lower() == b"set-cookie" and value.startswith(prefix)
        for name, value in response.raw_headers
    )


async def keep_session_cookie(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)

    if not getattr(request.state, SESSION_ALIVE, False):
        return response
    if _already_decided(response):
        return response

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return response

    # Флаги те же, что при входе, и берутся из одного места: разойдись они —
    # продление молча сняло бы `Secure` с cookie, которую логин поставил
    # правильно, и дыра открылась бы на втором запросе, а не на первом.
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=cookie_secure(request),
        samesite="lax",
        max_age=int(session_window().total_seconds()),
        path="/",
    )
    return response


__all__ = ["SESSION_ALIVE", "keep_session_cookie"]
