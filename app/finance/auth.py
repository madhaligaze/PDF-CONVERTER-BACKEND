"""Регистрация, вход и сессии раздела «Финансы».

Своя авторизация, а не общая с дашбордом — почему, объяснено в
`accounts_model.py`. Здесь механика.

Что сделано так же, как в дашборде, и это осознанно
───────────────────────────────────────────────────
* пароли — argon2id, открытый текст в базу не попадает никогда;
* сессии серверные: в cookie уходит случайный токен, в базе лежит его SHA-256,
  поэтому дамп базы не даёт работающих сессий;
* логин (почта) нечувствителен к регистру и обрамляющим пробелам, пароль —
  чувствителен;
* ограничение перебора: пять неудачных попыток на почту подряд — минута
  ожидания. Счётчик в памяти процесса, не в базе: защита от подбора не должна
  писать в базу на каждый неверный пароль.

Что сделано иначе
─────────────────
**Регистрация открыта.** В дашборде первого админа заводят переменными
окружения, и это правильно для одной компании. Здесь компания регистрируется
сама: почта, пароль, название компании — и человек сразу владелец своей
компании со своими счетами.

**Компания выбирается в сессии.** Один человек ведёт несколько компаний;
переключатель меняет `sessions.workspace_id`, а не заводит вторую сессию.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy.orm import Session

from app.finance.accounts_model import FinanceMembership, FinanceSession, FinanceUser
from app.finance.models import Workspace

log = logging.getLogger(__name__)

_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 8
SESSION_TTL = timedelta(days=30)
COOKIE_NAME = "fin_session"
#: Как часто обновлять «был в сети» у сессии.
LAST_SEEN_STEP = timedelta(minutes=1)

#: Почта проверяется грубо и намеренно: сложные выражения отсекают живые
#: адреса, а подтверждение всё равно приходит письмом (когда оно появится).
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")

#: Права ролей. Читается как «что можно делать», а не «кто ты»: проверка в
#: маршруте спрашивает `can(role, "accounts")`, и добавление роли не требует
#: правки каждого маршрута.
_ABILITIES: dict[str, frozenset[str]] = {
    "owner": frozenset({"read", "write", "accounts", "people", "company"}),
    "admin": frozenset({"read", "write", "accounts", "people"}),
    "accountant": frozenset({"read", "write"}),
    "viewer": frozenset({"read"}),
}


class AuthError(Exception):
    """Отказ со текстом, который можно показать человеку."""


@dataclass(frozen=True)
class Member:
    """Снимок вошедшего: кто, в какой компании и что ему можно.

    Снимок, а не живая строка ORM: объект переживает закрытие сессии базы и
    уходит в обработчик маршрута, где базы уже нет.
    """

    user_id: uuid.UUID
    email: str
    full_name: str
    workspace_id: uuid.UUID | None
    workspace_title: str
    role: str
    must_change_password: bool
    session_id: uuid.UUID

    def can(self, ability: str) -> bool:
        return ability in _ABILITIES.get(self.role, frozenset())

    @property
    def display_name(self) -> str:
        return self.full_name.strip() or self.email


# ── Ограничение перебора ─────────────────────────────────────────────────────

_ATTEMPTS: dict[str, list[float]] = {}
_ATTEMPTS_LOCK = threading.Lock()
_MAX_ATTEMPTS = 5
_WINDOW = 60.0
#: С какого размера словаря выбрасывать остывшие ключи. Перебор по списку
#: адресов — это новый ключ на каждую попытку, и назад его никто не спросит.
_SWEEP_AT = 1024


def _too_many_attempts(key: str) -> bool:
    now = time.monotonic()
    with _ATTEMPTS_LOCK:
        hits = [stamp for stamp in _ATTEMPTS.get(key, []) if now - stamp < _WINDOW]
        # Пустой список не храним: проверка идёт на каждом входе, и каждый
        # когда-либо введённый адрес оставался бы в словаре навсегда.
        if hits:
            _ATTEMPTS[key] = hits
        else:
            _ATTEMPTS.pop(key, None)
        return len(hits) >= _MAX_ATTEMPTS


def _note_failure(key: str) -> None:
    now = time.monotonic()
    with _ATTEMPTS_LOCK:
        if len(_ATTEMPTS) >= _SWEEP_AT:
            cold = [k for k, hits in _ATTEMPTS.items() if all(now - s >= _WINDOW for s in hits)]
            for stale in cold:
                del _ATTEMPTS[stale]
        _ATTEMPTS.setdefault(key, []).append(now)


def _forget_failures(key: str) -> None:
    with _ATTEMPTS_LOCK:
        _ATTEMPTS.pop(key, None)


# ── Пароли и токены ──────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Пароль короче {MIN_PASSWORD_LENGTH} символов")


# ── Регистрация и вход ───────────────────────────────────────────────────────


def register(
    session: Session,
    *,
    email: str,
    password: str,
    company: str,
    full_name: str = "",
    user_agent: str = "",
) -> tuple[Member, str]:
    """Новая компания с её владельцем. Возвращает снимок и токен сессии.

    Начальные справочники компании заводит `service.create_workspace` — тот же
    код, что раньше создавал единственное пространство: пустая компания без
    счетов не даёт сделать ни одного действия.
    """
    from app.finance import service  # локально: service импортирует auth не тут

    clean_email = normalize_email(email)
    if not _EMAIL.match(clean_email):
        raise AuthError("Это не похоже на адрес почты")
    _check_password(password)
    title = (company or "").strip()
    if len(title) < 2:
        raise AuthError("У компании должно быть название")

    exists = session.scalar(
        sa.select(FinanceUser).where(FinanceUser.email_normalized == clean_email)
    )
    if exists is not None:
        raise AuthError("Такая почта уже зарегистрирована — войдите или смените пароль")

    user = FinanceUser(
        email=(email or "").strip(),
        email_normalized=clean_email,
        password_hash=hash_password(password),
        full_name=(full_name or "").strip(),
    )
    session.add(user)
    session.flush()

    workspace = service.create_workspace(session, title=title)
    session.add(
        FinanceMembership(workspace_id=workspace.id, user_id=user.id, role="owner")
    )
    session.flush()

    token = _open_session(session, user=user, workspace_id=workspace.id, user_agent=user_agent)
    log.info("finance: зарегистрирована компания «%s»", title)
    return resolve(session, token) or _member_of(session, user, workspace, "owner", token), token


def login(
    session: Session, *, email: str, password: str, user_agent: str = ""
) -> tuple[Member, str]:
    """Вход по почте и паролю."""
    clean_email = normalize_email(email)
    if _too_many_attempts(clean_email):
        raise AuthError("Слишком много попыток. Подождите минуту и попробуйте снова")

    user = session.scalar(
        sa.select(FinanceUser).where(FinanceUser.email_normalized == clean_email)
    )
    # Одинаковый текст на «нет такой почты» и «неверный пароль»: разные
    # сообщения позволяют перебором узнать, кто зарегистрирован.
    if user is None or not verify_password(user.password_hash, password or ""):
        _note_failure(clean_email)
        raise AuthError("Неверная почта или пароль")
    if user.status != "active":
        raise AuthError("Учётная запись заблокирована")

    _forget_failures(clean_email)
    membership = session.scalar(
        sa.select(FinanceMembership)
        .where(FinanceMembership.user_id == user.id)
        .order_by(FinanceMembership.created_at)
    )
    user.last_login_at = datetime.now(UTC)
    token = _open_session(
        session,
        user=user,
        workspace_id=membership.workspace_id if membership else None,
        user_agent=user_agent,
    )
    member = resolve(session, token)
    if member is None:  # pragma: no cover — сессия только что создана
        raise AuthError("Не удалось открыть сессию")
    return member, token


def _open_session(
    session: Session, *, user: FinanceUser, workspace_id: uuid.UUID | None, user_agent: str
) -> str:
    token = secrets.token_urlsafe(32)
    session.add(
        FinanceSession(
            user_id=user.id,
            token_hash=_token_hash(token),
            workspace_id=workspace_id,
            expires_at=datetime.now(UTC) + SESSION_TTL,
            user_agent=(user_agent or "")[:400],
        )
    )
    session.flush()
    return token


def resolve(session: Session, token: str | None) -> Member | None:
    """Токен из cookie → снимок вошедшего. Просрочку удаляем сразу."""
    if not token:
        return None
    row = session.scalar(
        sa.select(FinanceSession).where(FinanceSession.token_hash == _token_hash(token))
    )
    if row is None:
        return None
    now = datetime.now(UTC)
    expires = row.expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires is not None and expires <= now:
        session.delete(row)
        session.flush()
        return None

    user = session.get(FinanceUser, row.user_id)
    if user is None or user.status != "active":
        return None

    workspace = session.get(Workspace, row.workspace_id) if row.workspace_id else None
    membership = None
    if workspace is not None:
        membership = session.scalar(
            sa.select(FinanceMembership).where(
                FinanceMembership.workspace_id == workspace.id,
                FinanceMembership.user_id == user.id,
            )
        )
        if membership is None:
            # Из компании исключили, пока сессия была открыта. Не молчим и не
            # оставляем доступ: контекст сбрасывается, человек выберет другую.
            workspace = None

    # «Был в сети» пишется не чаще раза в минуту. Реестр договоров опрашивает
    # сервер раз в две секунды из каждой открытой вкладки, и запись на каждый
    # запрос превращала бы чтение в поток обновлений одной строки.
    seen = row.last_seen_at
    if seen is not None and seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    if seen is None or (now - seen) >= LAST_SEEN_STEP:
        row.last_seen_at = now
        session.flush()
    return _member_of(session, user, workspace, membership.role if membership else "viewer", token, row.id)


def _member_of(
    session: Session,
    user: FinanceUser,
    workspace: Workspace | None,
    role: str,
    token: str,
    session_id: uuid.UUID | None = None,
) -> Member:
    del session, token  # подпись одна на две точки вызова
    return Member(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name or "",
        workspace_id=workspace.id if workspace else None,
        workspace_title=workspace.title if workspace else "",
        role=role,
        must_change_password=bool(user.must_change_password),
        session_id=session_id or uuid.uuid4(),
    )


def logout(session: Session, token: str | None) -> None:
    if not token:
        return
    row = session.scalar(
        sa.select(FinanceSession).where(FinanceSession.token_hash == _token_hash(token))
    )
    if row is not None:
        session.delete(row)
        session.flush()


def companies_of(session: Session, user_id: uuid.UUID) -> list[dict[str, object]]:
    """Компании человека — для переключателя в шапке."""
    rows = session.execute(
        sa.select(Workspace.id, Workspace.title, FinanceMembership.role)
        .join(FinanceMembership, FinanceMembership.workspace_id == Workspace.id)
        .where(FinanceMembership.user_id == user_id)
        .order_by(Workspace.created_at)
    )
    return [{"id": str(wid), "title": title, "role": role} for wid, title, role in rows]


def switch_company(session: Session, token: str, workspace_id: uuid.UUID) -> Member:
    """Сменить компанию в текущей сессии."""
    member = resolve(session, token)
    if member is None:
        raise AuthError("Сессия истекла")
    membership = session.scalar(
        sa.select(FinanceMembership).where(
            FinanceMembership.workspace_id == workspace_id,
            FinanceMembership.user_id == member.user_id,
        )
    )
    if membership is None:
        raise AuthError("Эта компания вам не открыта")
    row = session.scalar(
        sa.select(FinanceSession).where(FinanceSession.token_hash == _token_hash(token))
    )
    if row is None:  # pragma: no cover — resolve уже проверил
        raise AuthError("Сессия истекла")
    row.workspace_id = workspace_id
    session.flush()
    resolved = resolve(session, token)
    if resolved is None:  # pragma: no cover
        raise AuthError("Сессия истекла")
    return resolved


def add_company(session: Session, member: Member, *, title: str) -> dict[str, object]:
    """Ещё одна компания тому же человеку. Он её владелец."""
    from app.finance import service

    clean = (title or "").strip()
    if len(clean) < 2:
        raise AuthError("У компании должно быть название")
    workspace = service.create_workspace(session, title=clean)
    session.add(
        FinanceMembership(workspace_id=workspace.id, user_id=member.user_id, role="owner")
    )
    session.flush()
    return {"id": str(workspace.id), "title": workspace.title, "role": "owner"}


def invite(
    session: Session,
    member: Member,
    *,
    email: str,
    role: str,
    full_name: str = "",
    password: str,
) -> dict[str, object]:
    """Добавить человека в компанию.

    Пароль задаёт владелец и передаёт лично; флаг `must_change_password`
    заставит сменить его при первом входе. Это не «безопасно навсегда», а
    честный первый шаг: письма мы пока не отправляем, и придумывать вид
    приглашения по почте, которого нет, хуже, чем сказать прямо.
    """
    if not member.can("people"):
        raise AuthError("Добавлять людей может владелец или администратор")
    if member.workspace_id is None:
        raise AuthError("Сначала выберите компанию")
    if role not in ("admin", "accountant", "viewer"):
        raise AuthError("Такой роли нет")
    _check_password(password)

    clean_email = normalize_email(email)
    if not _EMAIL.match(clean_email):
        raise AuthError("Это не похоже на адрес почты")

    user = session.scalar(
        sa.select(FinanceUser).where(FinanceUser.email_normalized == clean_email)
    )
    if user is None:
        user = FinanceUser(
            email=(email or "").strip(),
            email_normalized=clean_email,
            password_hash=hash_password(password),
            full_name=(full_name or "").strip(),
            must_change_password=True,
        )
        session.add(user)
        session.flush()

    existing = session.scalar(
        sa.select(FinanceMembership).where(
            FinanceMembership.workspace_id == member.workspace_id,
            FinanceMembership.user_id == user.id,
        )
    )
    if existing is not None:
        raise AuthError("Этот человек уже в компании")

    session.add(
        FinanceMembership(
            workspace_id=member.workspace_id,
            user_id=user.id,
            role=role,
            invited_by=member.user_id,
        )
    )
    session.flush()
    return {"id": str(user.id), "email": user.email, "role": role}


def members_of(session: Session, workspace_id: uuid.UUID) -> list[dict[str, object]]:
    rows = session.execute(
        sa.select(
            FinanceUser.id,
            FinanceUser.email,
            FinanceUser.full_name,
            FinanceUser.status,
            FinanceUser.last_login_at,
            FinanceMembership.role,
        )
        .join(FinanceMembership, FinanceMembership.user_id == FinanceUser.id)
        .where(FinanceMembership.workspace_id == workspace_id)
        .order_by(FinanceMembership.created_at)
    )
    return [
        {
            "id": str(uid),
            "email": email,
            "full_name": full_name or "",
            "status": status,
            "last_login_at": last.isoformat() if last else None,
            "role": role,
        }
        for uid, email, full_name, status, last, role in rows
    ]


def change_role(session: Session, member: Member, *, user_id: uuid.UUID, role: str) -> None:
    if not member.can("people"):
        raise AuthError("Менять роли может владелец или администратор")
    if role not in ("admin", "accountant", "viewer"):
        raise AuthError("Такой роли нет")
    if user_id == member.user_id:
        raise AuthError("Свою роль менять нельзя — иначе можно остаться без прав")
    membership = session.scalar(
        sa.select(FinanceMembership).where(
            FinanceMembership.workspace_id == member.workspace_id,
            FinanceMembership.user_id == user_id,
        )
    )
    if membership is None:
        raise AuthError("Этот человек не в компании")
    if membership.role == "owner":
        raise AuthError("Владельца компании нельзя понизить")
    membership.role = role
    session.flush()


def remove_member(session: Session, member: Member, *, user_id: uuid.UUID) -> None:
    if not member.can("people"):
        raise AuthError("Убирать людей может владелец или администратор")
    membership = session.scalar(
        sa.select(FinanceMembership).where(
            FinanceMembership.workspace_id == member.workspace_id,
            FinanceMembership.user_id == user_id,
        )
    )
    if membership is None:
        raise AuthError("Этот человек не в компании")
    if membership.role == "owner":
        raise AuthError("Владельца компании убрать нельзя")
    session.delete(membership)
    session.flush()


def set_password(session: Session, member: Member, *, old: str, new: str) -> None:
    """Смена своего пароля. Старый спрашиваем всегда.

    Даже когда пароль временный: человек, получивший доступ к чужому открытому
    браузеру, не должен менять пароль, не зная прежнего.
    """
    user = session.get(FinanceUser, member.user_id)
    if user is None:
        raise AuthError("Учётная запись не найдена")
    if not verify_password(user.password_hash, old or ""):
        raise AuthError("Старый пароль не подошёл")
    _check_password(new)
    if new == old:
        raise AuthError("Новый пароль совпадает со старым")
    user.password_hash = hash_password(new)
    user.must_change_password = False
    session.flush()


def sessions_of(session: Session, user_id: uuid.UUID) -> list[dict[str, object]]:
    rows = session.scalars(
        sa.select(FinanceSession)
        .where(FinanceSession.user_id == user_id)
        .order_by(FinanceSession.last_seen_at.desc())
    )
    return [
        {
            "id": str(row.id),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
            "user_agent": row.user_agent or "",
        }
        for row in rows
    ]


def revoke_session(session: Session, member: Member, session_id: uuid.UUID) -> None:
    row = session.get(FinanceSession, session_id)
    if row is None or row.user_id != member.user_id:
        raise AuthError("Такой сессии нет")
    session.delete(row)
    session.flush()


__all__ = [
    "COOKIE_NAME",
    "MIN_PASSWORD_LENGTH",
    "AuthError",
    "Member",
    "add_company",
    "change_role",
    "companies_of",
    "hash_password",
    "invite",
    "login",
    "logout",
    "members_of",
    "normalize_email",
    "register",
    "remove_member",
    "resolve",
    "revoke_session",
    "sessions_of",
    "set_password",
    "switch_company",
    "verify_password",
]
