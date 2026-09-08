"""Authentication for the BBC dashboard: users, passwords, server-side sessions.

Design notes:

* Passwords are argon2id hashes; the plaintext never touches the database.
* Sessions live server-side. The cookie carries a random token, the database
  stores only its SHA-256 — a database dump must not yield usable sessions.
* The first admin is seeded from `BBC_BOOTSTRAP_ADMIN` / `BBC_BOOTSTRAP_PASSWORD`
  and only while `bbc.users` is empty; afterwards those variables are ignored
  and the credentials are changed from the account page.
* Логин нечувствителен к регистру и обрамляющим пробелам — см. `_find_user`.
  Пароль, разумеется, чувствителен.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bbc.config import bbc_settings
from app.bbc.db import bbc_session
from app.bbc.devices import describe_device, is_mobile
from app.bbc.models import BbcUser, BbcUserSession

log = logging.getLogger(__name__)

_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 8
MIN_USERNAME_LENGTH = 3


class AuthError(Exception):
    """Authentication/validation failure carrying a user-facing message."""


@dataclass(frozen=True)
class AuthedUser:
    """Detached snapshot of the signed-in user (no live ORM object escapes).

    Несёт и права сотрудника: `deps.current_scope` строит из них область
    видимости на каждый запрос, не заглядывая в базу второй раз.
    """

    id: int
    username: str
    role: str
    full_name: str = ""
    status: str = "active"
    must_change_password: bool = False
    departments: tuple[str, ...] = ()
    blocks: tuple[str, ...] = ()
    data_scope: str = "all"
    employee_aliases: tuple[str, ...] = ()

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def display_name(self) -> str:
        return self.full_name.strip() or self.username


def _snapshot(user: BbcUser) -> AuthedUser:
    """ORM-строка → неизменяемый снимок. Списки из JSON могут быть None."""
    return AuthedUser(
        id=user.id,
        username=user.username,
        role=user.role,
        full_name=user.full_name or "",
        status=user.status or "active",
        must_change_password=bool(user.must_change_password),
        departments=tuple(user.departments or ()),
        blocks=tuple(user.blocks or ()),
        data_scope=user.data_scope or "own",
        employee_aliases=tuple(user.employee_aliases or ()),
    )


# ── Hashing ──────────────────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def hash_token(token: str) -> str:
    """SHA-256 of a session/link token. Fast on purpose — the token is already
    128 bits of entropy, so it needs no key-stretching."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


# ── Validation ───────────────────────────────────────────────────────────────────


def _validate_username(username: str) -> str:
    value = (username or "").strip()
    if len(value) < MIN_USERNAME_LENGTH:
        raise AuthError(f"Логин должен быть не короче {MIN_USERNAME_LENGTH} символов")
    return value


def _validate_password(password: str) -> str:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов")
    return password


def _find_user(session: Session, username: str) -> BbcUser | None:
    """Найти пользователя по логину, не придираясь к регистру.

    Логин — это имя человека, а не пароль: «Admin» и «admin» это один и тот же
    человек, и отказ со словами «неверный логин или пароль» на верных данных
    ничего не защищает, а только злит. Регистр пароля, разумеется, остаётся
    значимым.

    Сначала точное совпадение (индекс, обычный случай), и только потом перебор.
    Перебор здесь дешёвый: в `bbc.users` живёт пара администраторов. Так же и
    надёжнее, чем `lower()` в SQL: в SQLite он умеет только ASCII, и логин
    кириллицей вёл бы себя на тестах иначе, чем на проде.
    """
    value = (username or "").strip()
    if not value:
        return None

    exact = session.scalar(select(BbcUser).where(BbcUser.username == value))
    if exact is not None:
        return exact

    target = value.casefold()
    for candidate in session.scalars(select(BbcUser)):
        if candidate.username.casefold() == target:
            return candidate
    return None


# ── Bootstrap ────────────────────────────────────────────────────────────────────


def ensure_bootstrap_admin() -> None:
    """Seed the first admin, but only while no user exists at all."""
    username = (bbc_settings.bootstrap_admin or "").strip()
    password = bbc_settings.bootstrap_password or ""
    if not username or not password:
        return

    with bbc_session() as session:
        if session.scalar(select(BbcUser).limit(1)) is not None:
            return
        session.add(
            BbcUser(
                username=username,
                password_hash=hash_password(password),
                role="admin",
            )
        )
        log.info("BBC: bootstrap admin %r created", username)


def has_any_user() -> bool:
    with bbc_session() as session:
        return session.scalar(select(BbcUser).limit(1)) is not None


# ── Ограничение перебора ─────────────────────────────────────────────────────────
#
# argon2id делает одну попытку дорогой, но не запрещает миллион. Здесь пароли
# выдаёт админ и диктует по телефону, временный — вообще из трёх слогов и живёт
# в переписке; перебор с десятка процессов это реальный сценарий, а не теория.
#
# Счёт ведётся и по логину, и по IP: только по логину — и перебор идёт по
# списку имён, только по IP — и офис за одним адресом блокирует сам себя после
# пяти опечаток пятерых разных людей. Сработавший счётчик отвечает одинаково,
# существует такой логин или нет: подсказывать, какое имя угадано, незачем.

MAX_LOGIN_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 60.0

_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()


def _login_keys(username: str, ip: str | None) -> list[str]:
    keys = [f"user:{(username or '').strip().casefold()}"]
    if ip:
        keys.append(f"ip:{ip}")
    return keys


def _assert_not_throttled(username: str, ip: str | None) -> None:
    now = time.monotonic()
    with _failures_lock:
        for key in _login_keys(username, ip):
            recent = [at for at in _failures.get(key, ()) if now - at < LOGIN_LOCKOUT_SECONDS]
            if recent:
                _failures[key] = recent
            else:
                _failures.pop(key, None)
            if len(recent) >= MAX_LOGIN_FAILURES:
                raise AuthError(
                    "Слишком много попыток входа. Подождите минуту и попробуйте снова"
                )


def _record_failure(username: str, ip: str | None) -> None:
    now = time.monotonic()
    with _failures_lock:
        for key in _login_keys(username, ip):
            recent = [at for at in _failures.get(key, ()) if now - at < LOGIN_LOCKOUT_SECONDS]
            recent.append(now)
            _failures[key] = recent


def _clear_failures(username: str, ip: str | None) -> None:
    with _failures_lock:
        for key in _login_keys(username, ip):
            _failures.pop(key, None)


# ── Login / sessions ─────────────────────────────────────────────────────────────


def login(username: str, password: str, *, ip: str | None = None, user_agent: str | None = None) -> str:
    """Verify credentials and open a session. Returns the raw cookie token."""
    _assert_not_throttled(username, ip)
    with bbc_session() as session:
        user = _find_user(session, username)
        # Verify even when the user is missing, so a wrong login and a wrong
        # password take the same time and cannot be told apart.
        if user is None:
            _hasher.hash(password or "x")
            _record_failure(username, ip)
            raise AuthError("Неверный логин или пароль")
        if not user.is_active:
            raise AuthError("Учётная запись отключена")
        if not verify_password(user.password_hash, password or ""):
            _record_failure(username, ip)
            raise AuthError("Неверный логин или пароль")

        _clear_failures(username, ip)
        token = new_token()
        session.add(
            BbcUserSession(
                id=secrets.token_hex(16),
                user_id=user.id,
                token_hash=hash_token(token),
                expires_at=datetime.now(UTC) + session_window(),
                ip=ip,
                user_agent=(user_agent or "")[:255] or None,
            )
        )
        session.flush()
        _trim_sessions(session, user.id, keep=hash_token(token))
        return token


#: Сколько заходов одной учётки держим живыми одновременно.
#:
#: Ограничение появилось из-за списка в кабинете. Вход создаёт запись, а
#: браузер, который больше не вернулся, её не закрывает: cookie у него уже
#: другая, а строка живёт весь свой срок. Пока срок был 12 часов, такие
#: строки исчезали сами и никто их не видел. С окном в 30 суток список
#: превратился в полсотни одинаковых «Chrome на Windows» — и чужое устройство,
#: ради которого экран и сделан, в нём стало не найти.
MAX_LIVE_SESSIONS = 10


def _trim_sessions(session: Session, user_id: int, *, keep: str) -> int:
    """Оставить десять заходов; `keep` — хэш только что выданного, он неприкосновенен.

    Что здесь важно
    ───────────────
    1. **Только что созданный заход не трогаем.** Это cookie, которую человек
       получает прямо сейчас; выкинуть её значит не пустить его туда, куда он
       только что вошёл.

    2. **Сначала выкидываем те, которыми ни разу не воспользовались.** Заход
       без единого обращения после входа — это выданная и забытая cookie:
       браузер за ней не вернулся. Из всех записей она значит меньше всего.

    3. **Дальше — по последнему обращению, а не по времени входа.** Выкидывать
       надо забытые, а не старые: ноутбук, на котором работают каждый день
       полгода, — самый «старый» по входу, и считай мы по нему, вылетал бы
       именно он.

    Пункт 2 появился не из рассуждения. Без него проверка «долго работающее
    устройство переживает уборку» падала: заход, которым пользовались, и
    десяток свежих, которыми не пользовались, получали одинаковую отметку
    времени с точностью до секунды, и кого из них считать свежее — решал
    случай.

    Выкинуть лишнего не страшно: человек войдёт заново. Обратная ошибка —
    оставить живым заход, о котором никто не помнит, — стоит дороже.
    """
    long_ago = datetime.min.replace(tzinfo=UTC)

    def freshness(record: BbcUserSession) -> tuple[bool, datetime]:
        seen = _aware(record.last_seen_at)
        return (seen is not None, seen or _aware(record.created_at) or long_ago)

    others = [
        record
        for record in session.scalars(
            select(BbcUserSession).where(BbcUserSession.user_id == user_id)
        )
        if record.token_hash != keep
    ]
    others.sort(key=freshness, reverse=True)

    doomed = others[MAX_LIVE_SESSIONS - 1 :]
    for record in doomed:
        session.delete(record)
    return len(doomed)


def session_window() -> timedelta:
    """Сколько сессия живёт без обращений.

    Не «сколько живёт вообще»: окно сдвигается на каждом запросе, поэтому тот,
    кто заходит в дашборд каждый день, не встречает форму входа никогда, а
    забытая сессия на чужом ноутбуке умирает сама.
    """
    return timedelta(hours=bbc_settings.session_ttl_hours)


def resolve_session(token: str | None) -> AuthedUser | None:
    """Return the user behind a cookie token, or None when it is absent/expired.

    Заодно продлевает сессию. Раньше срок жизни отсчитывался от входа: ровно
    через `BBC_SESSION_TTL_HOURS` человека выбрасывало на форму входа посреди
    работы, независимо от того, что он всё это время в дашборде и сидел.
    Теперь окно отсчитывается от последнего обращения.

    Продление пишется в базу не на каждый запрос, а когда израсходована
    половина окна. Иначе каждый опрос дашборда — а он опрашивает сам —
    превращался бы в запись в таблицу сессий.
    """
    if not token:
        return None
    now = datetime.now(UTC)
    window = session_window()
    with bbc_session() as session:
        record = session.scalar(
            select(BbcUserSession).where(BbcUserSession.token_hash == hash_token(token))
        )
        if record is None:
            return None
        if _expired(record.expires_at, now):
            session.delete(record)
            return None
        user = session.get(BbcUser, record.user_id)
        if user is None or not user.is_active:
            return None

        record.last_seen_at = now
        if _left(record.expires_at, now) < window / 2:
            record.expires_at = now + window
        return _snapshot(user)


def logout(token: str | None) -> None:
    if not token:
        return
    with bbc_session() as session:
        record = session.scalar(
            select(BbcUserSession).where(BbcUserSession.token_hash == hash_token(token))
        )
        if record is not None:
            session.delete(record)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite отдаёт наивные отметки времени; считаем их UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _expired(expires_at: datetime | None, now: datetime) -> bool:
    moment = _aware(expires_at)
    if moment is None:
        return False
    return moment <= now


def _left(expires_at: datetime | None, now: datetime) -> timedelta:
    """Сколько осталось до конца окна. Бессрочная сессия — «очень много»."""
    moment = _aware(expires_at)
    if moment is None:
        return timedelta.max
    return moment - now


# ── Credential changes ───────────────────────────────────────────────────────────


def change_credentials(
    user_id: int,
    *,
    current_password: str,
    new_username: str | None = None,
    new_password: str | None = None,
) -> None:
    """Change login and/or password. The current password is always required."""
    if not new_username and not new_password:
        raise AuthError("Нечего менять: укажите новый логин или новый пароль")

    with bbc_session() as session:
        user = session.get(BbcUser, user_id)
        if user is None:
            raise AuthError("Пользователь не найден")
        if not verify_password(user.password_hash, current_password or ""):
            raise AuthError("Текущий пароль неверен")

        if new_username:
            username = _validate_username(new_username)
            # Сравниваем без учёта регистра: раз «Admin» и «admin» пускают к
            # одному и тому же человеку, то и занять их разными людьми нельзя.
            # Себе же поменять только регистр — можно, это то же самое имя.
            if username.casefold() != user.username.casefold():
                taken = _find_user(session, username)
                if taken is not None:
                    raise AuthError("Такой логин уже занят")
            user.username = username

        if new_password:
            user.password_hash = hash_password(_validate_password(new_password))
            user.password_changed_at = datetime.now(UTC)
            # A password change invalidates every other session of this user.
            for record in session.scalars(
                select(BbcUserSession).where(BbcUserSession.user_id == user_id)
            ):
                session.delete(record)

        user.updated_at = datetime.now(UTC)


# ── История заходов ──────────────────────────────────────────────────────────


def list_sessions(viewer: AuthedUser, current_token: str | None = None) -> list[dict]:
    """Живые сессии, которые этому человеку положено видеть.

    Правило видимости
    ─────────────────
    Администратор видит заходы сотрудников: это его работа — заметить, что в
    учётку бухгалтера кто-то зашёл с незнакомого устройства, и оборвать заход.
    Сотрудник видит только свои. Обратное направление закрыто намеренно:
    список админских сессий — это карта того, откуда и когда приходит человек с
    полным доступом, и выдавать её каждому сотруднику незачем.

    Отсюда же следует, что фильтр стоит здесь, а не в маршруте. Забыть его в
    одном из двух обработчиков — значит открыть список целиком, и ошибка эта
    выглядела бы как работающий экран.

    Просроченные не показываем: это список того, что прямо сейчас открыто, а не
    журнал за всё время. Их подчищает `purge_expired_sessions`, но между
    уборками они лежат в таблице и в ответе выглядели бы как живой заход.
    """
    now = datetime.now(UTC)
    wanted = hash_token(current_token) if current_token else None

    with bbc_session() as session:
        query = select(BbcUserSession)
        if not viewer.is_admin:
            query = query.where(BbcUserSession.user_id == viewer.id)

        records = [
            record
            for record in session.scalars(query)
            if not _expired(record.expires_at, now)
        ]
        names = {
            user.id: user
            for user in session.scalars(
                select(BbcUser).where(
                    BbcUser.id.in_({record.user_id for record in records})
                )
            )
        }

        rows = [
            {
                "id": record.id,
                "user_id": record.user_id,
                "username": getattr(names.get(record.user_id), "username", ""),
                "full_name": getattr(names.get(record.user_id), "full_name", "") or "",
                "role": getattr(names.get(record.user_id), "role", ""),
                "mine": record.user_id == viewer.id,
                "current": wanted is not None and record.token_hash == wanted,
                "device": describe_device(record.user_agent),
                "mobile": is_mobile(record.user_agent),
                "user_agent": record.user_agent or "",
                "ip": record.ip or "",
                "created_at": _iso(record.created_at),
                "last_seen_at": _iso(record.last_seen_at),
                "expires_at": _iso(record.expires_at),
            }
            for record in records
        ]

    # Свои сверху, внутри — свежие первыми. Два устойчивых прохода, а не один
    # составной ключ: по свежести порядок обратный, по «своим» — прямой, и в
    # одном ключе это пришлось бы выражать через отрицание строки.
    rows.sort(key=lambda row: row["last_seen_at"] or "", reverse=True)
    rows.sort(key=lambda row: not row["mine"])
    return rows


def end_session(
    viewer: AuthedUser, session_id: str, current_token: str | None = None
) -> bool:
    """Оборвать заход. Свой — всегда, чужой — только администратору.

    Возвращает, был ли это тот самый заход, из которого пришёл запрос: тогда
    вызывающему надо ещё и убрать cookie. Отвечает на это здесь, потому что
    здесь уже известен хэш токена; маршруту пришлось бы лезть в таблицу сессий
    вторым запросом и знать про её устройство.

    Чужая сессия и несуществующая отвечают одинаково. Сотрудник, перебирающий
    идентификаторы, иначе узнал бы по разнице ответов, какие из них настоящие,
    — а это и есть список админских заходов, только собранный по одному.
    """
    with bbc_session() as session:
        record = session.get(BbcUserSession, session_id)
        if record is None or (not viewer.is_admin and record.user_id != viewer.id):
            raise AuthError("Сессия не найдена")
        was_current = bool(current_token) and record.token_hash == hash_token(current_token or "")
        session.delete(record)
        return was_current


def _iso(value: datetime | None) -> str | None:
    moment = _aware(value)
    return moment.isoformat() if moment else None


def purge_expired_sessions() -> int:
    """Housekeeping for the background loop. Returns how many were removed."""
    now = datetime.now(UTC)
    removed = 0
    with bbc_session() as session:
        for record in session.scalars(select(BbcUserSession)):
            if _expired(record.expires_at, now):
                session.delete(record)
                removed += 1
    return removed


__all__ = [
    "AuthError",
    "AuthedUser",
    "change_credentials",
    "end_session",
    "ensure_bootstrap_admin",
    "hash_password",
    "hash_token",
    "has_any_user",
    "list_sessions",
    "login",
    "logout",
    "new_token",
    "purge_expired_sessions",
    "resolve_session",
    "session_window",
    "verify_password",
]
