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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bbc.config import bbc_settings
from app.bbc.db import bbc_session
from app.bbc.models import BbcUser, BbcUserSession

log = logging.getLogger(__name__)

_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 8
MIN_USERNAME_LENGTH = 3


class AuthError(Exception):
    """Authentication/validation failure carrying a user-facing message."""


@dataclass(frozen=True)
class AuthedUser:
    """Detached snapshot of the signed-in user (no live ORM object escapes)."""

    id: int
    username: str
    role: str


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


# ── Login / sessions ─────────────────────────────────────────────────────────────


def login(username: str, password: str, *, ip: str | None = None, user_agent: str | None = None) -> str:
    """Verify credentials and open a session. Returns the raw cookie token."""
    with bbc_session() as session:
        user = _find_user(session, username)
        # Verify even when the user is missing, so a wrong login and a wrong
        # password take the same time and cannot be told apart.
        if user is None:
            _hasher.hash(password or "x")
            raise AuthError("Неверный логин или пароль")
        if not user.is_active:
            raise AuthError("Учётная запись отключена")
        if not verify_password(user.password_hash, password or ""):
            raise AuthError("Неверный логин или пароль")

        token = new_token()
        session.add(
            BbcUserSession(
                id=secrets.token_hex(16),
                user_id=user.id,
                token_hash=hash_token(token),
                expires_at=datetime.now(UTC) + timedelta(hours=bbc_settings.session_ttl_hours),
                ip=ip,
                user_agent=(user_agent or "")[:255] or None,
            )
        )
        return token


def resolve_session(token: str | None) -> AuthedUser | None:
    """Return the user behind a cookie token, or None when it is absent/expired."""
    if not token:
        return None
    now = datetime.now(UTC)
    with bbc_session() as session:
        record = session.scalar(
            select(BbcUserSession).where(BbcUserSession.token_hash == hash_token(token))
        )
        if record is None:
            return None
        if _expired(record.expires_at, now):
            session.delete(record)
            return None
        record.last_seen_at = now
        user = session.get(BbcUser, record.user_id)
        if user is None or not user.is_active:
            return None
        return AuthedUser(id=user.id, username=user.username, role=user.role)


def logout(token: str | None) -> None:
    if not token:
        return
    with bbc_session() as session:
        record = session.scalar(
            select(BbcUserSession).where(BbcUserSession.token_hash == hash_token(token))
        )
        if record is not None:
            session.delete(record)


def _expired(expires_at: datetime | None, now: datetime) -> bool:
    if expires_at is None:
        return False
    # SQLite hands back naive datetimes; treat those as UTC.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= now


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
    "ensure_bootstrap_admin",
    "hash_password",
    "hash_token",
    "has_any_user",
    "login",
    "logout",
    "new_token",
    "purge_expired_sessions",
    "resolve_session",
    "verify_password",
]
