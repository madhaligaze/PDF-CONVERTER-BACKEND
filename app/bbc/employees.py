"""Учётки сотрудников: заводит админ, пароль сотрудник меняет при первом входе.

Почему не приглашения по почте: у бухгалтерии нет корпоративной почты, по
которой можно было бы разослать ссылки, а WhatsApp есть у всех. Админ создаёт
учётку, получает пароль один раз на экране и передаёт его лично.

Почему не «пароль = номер телефона», как в референсе KORT: зная чей-то номер,
можно забрать его аккаунт. Пароль здесь случайный, живёт только в ответе на
создание и в argon2-хеше, и до первой смены не открывает вообще ничего —
`deps.scope_for_user` отдаёт таким учёткам пустую область видимости.

Права — три независимые оси, ни одна не выводится из другой:

* `blocks` — какие разделы открываются;
* `departments` — чьи отделы видно;
* `data_scope` — `own` (только свои клиенты по колонке «Сотрудник»),
  `department` (весь отдел), `all`.

Роли-пресеты ниже — только заготовки для галочек на экране. Сервер хранит и
проверяет итоговый набор, а не имя роли: иначе переименование пресета молча
меняло бы права у всех, кому он когда-то был проставлен.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable

from sqlalchemy import select

from app.bbc.auth import AuthError, AuthedUser, hash_password
from app.bbc.db import bbc_session
from app.bbc.models import BbcAuditEntry, BbcUser, BbcUserSession
from app.bbc.scope import BLOCKS, DATA_SCOPES, DEPARTMENTS, canonical_department

log = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 8
MAX_ALIASES = 12

# Пресеты для экрана: имя → что проставить галочками. В базу не пишутся.
ROLE_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "key": "debt_manager",
        "name": "Менеджер по долгам",
        "description": "Свои должники и работа по ним. Ничего лишнего на экране.",
        "blocks": ["receivables", "touches"],
        "data_scope": "own",
    },
    {
        "key": "accountant",
        "name": "Бухгалтер",
        "description": "Долги отдела, касания и платёжный календарь.",
        "blocks": ["receivables", "touches", "calendar"],
        "data_scope": "department",
    },
    {
        "key": "head",
        "name": "Руководитель отдела",
        "description": "Весь отдел целиком: долги, отчётность, аналитика.",
        "blocks": ["receivables", "touches", "calendar", "reports", "analytics", "warnings"],
        "data_scope": "department",
    },
    {
        "key": "viewer",
        "name": "Наблюдатель",
        "description": "Только смотрит. Касания оставлять может.",
        "blocks": ["receivables", "touches"],
        "data_scope": "department",
    },
)

# Пароль из троек: его диктуют по телефону и вбивают руками с бумажки.
# Алфавит без 0/O/1/l/I — «ноль или О» съедает больше времени, чем экономят
# два лишних символа энтропии.
_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"


def generate_password() -> str:
    groups = ["".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(3)]
    return "-".join(groups)


# ── Валидация ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EmployeeInput:
    """Разобранная и проверенная форма сотрудника."""

    full_name: str
    departments: list[str]
    blocks: list[str]
    data_scope: str
    employee_aliases: list[str]


def parse_employee_input(
    *,
    full_name: str | None,
    departments: Iterable[str] | None,
    blocks: Iterable[str] | None,
    data_scope: str | None,
    employee_aliases: Iterable[str] | None,
) -> EmployeeInput:
    """Привести форму к тому, что можно класть в базу. Иначе — AuthError.

    Неизвестные коды отделов и разделов молча отбрасываются, а не роняют запрос:
    список разделов приходит с экрана, и опечатка в нём не должна мешать завести
    человека. А вот пустой результат — уже ошибка: учётка, которой ничего не
    видно, выглядит как рабочая и им не является.
    """
    name = (full_name or "").strip()
    if len(name) < 2:
        raise AuthError("Укажите имя сотрудника")

    codes: list[str] = []
    for raw in departments or ():
        code = canonical_department(raw)
        if code and code not in codes:
            codes.append(code)
    if not codes:
        raise AuthError(f"Выберите хотя бы один отдел ({', '.join(DEPARTMENTS)})")

    allowed = [key for key in (blocks or ()) if key in BLOCKS]
    if not allowed:
        raise AuthError("Отметьте хотя бы один раздел")

    kind = data_scope if data_scope in DATA_SCOPES else None
    if kind is None:
        raise AuthError("Неизвестная область данных")

    aliases: list[str] = []
    for raw in employee_aliases or ():
        value = " ".join((raw or "").split())
        if value and value not in aliases:
            aliases.append(value)
    aliases = aliases[:MAX_ALIASES]

    # `own` без единого написания — учётка, которой не видно ни одной строки.
    # Это не «строгая настройка», это сломанный аккаунт, и сказать об этом надо
    # сейчас, а не когда сотрудник увидит пустой экран.
    if kind == "own" and not aliases:
        raise AuthError(
            "Для области «только свои клиенты» отметьте, как этот человек "
            "записан в колонке «Сотрудник»"
        )

    return EmployeeInput(
        full_name=name,
        departments=codes,
        blocks=allowed,
        data_scope=kind,
        employee_aliases=aliases,
    )


def _validate_username(username: str) -> str:
    value = (username or "").strip()
    if len(value) < 3:
        raise AuthError("Логин должен быть не короче 3 символов")
    if any(ch.isspace() for ch in value):
        raise AuthError("В логине не должно быть пробелов")
    return value


# ── Сериализация ─────────────────────────────────────────────────────────────────


def serialize(user: BbcUser) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name or "",
        "role": user.role,
        "status": user.status or "active",
        "must_change_password": bool(user.must_change_password),
        "departments": list(user.departments or ()),
        "blocks": list(user.blocks or ()),
        "data_scope": user.data_scope or "own",
        "employee_aliases": list(user.employee_aliases or ()),
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "password_changed_at": (
            user.password_changed_at.isoformat() if user.password_changed_at else None
        ),
    }


def _audit(session, actor: AuthedUser, action: str, target: BbcUser, detail: str = "") -> None:
    """Запись в bbc.audit_log.

    Таблица существовала с самого начала модуля и до сих пор не имела ни одного
    писателя. Управление учётками — ровно тот случай, ради которого она заведена:
    «кто кого завёл, кому сбросил пароль, кого уволил» должно оставаться следом,
    переживающим саму учётку.
    """
    session.add(
        BbcAuditEntry(
            actor_type="user",
            actor_id=str(actor.id),
            action=action,
            detail=f"{target.username} (#{target.id}) {detail}".strip(),
        )
    )


def _drop_sessions(session, user_id: int) -> None:
    for record in session.scalars(select(BbcUserSession).where(BbcUserSession.user_id == user_id)):
        session.delete(record)


# ── Операции ─────────────────────────────────────────────────────────────────────


def list_employees() -> list[dict[str, Any]]:
    """Все, кроме админов. Уволенные тоже — их показывают отдельным списком."""
    with bbc_session() as session:
        rows = session.scalars(
            select(BbcUser).where(BbcUser.role != "admin").order_by(BbcUser.full_name)
        ).all()
        return [serialize(user) for user in rows]


def create_employee(actor: AuthedUser, *, username: str, form: EmployeeInput) -> tuple[dict, str]:
    """Завести сотрудника. Возвращает (учётка, временный пароль).

    Пароль возвращается ровно здесь и больше нигде: в базе только argon2-хеш,
    и повторно узнать его нельзя — можно только сбросить.
    """
    login = _validate_username(username)
    password = generate_password()

    with bbc_session() as session:
        target = login.casefold()
        clash = any(row.username.casefold() == target for row in session.scalars(select(BbcUser)))
        if clash:
            raise AuthError("Такой логин уже занят")

        user = BbcUser(
            username=login,
            password_hash=hash_password(password),
            role="employee",
            is_active=True,
            status="active",
            must_change_password=True,
            full_name=form.full_name,
            departments=form.departments,
            blocks=form.blocks,
            data_scope=form.data_scope,
            employee_aliases=form.employee_aliases,
        )
        session.add(user)
        session.flush()
        _audit(session, actor, "employee.create", user, f"→ {', '.join(form.departments)}")
        payload = serialize(user)

    log.info("BBC: employee %r created by %r", login, actor.username)
    return payload, password


def update_employee(actor: AuthedUser, user_id: int, form: EmployeeInput) -> dict[str, Any]:
    with bbc_session() as session:
        user = session.get(BbcUser, user_id)
        if user is None or user.role == "admin":
            raise AuthError("Сотрудник не найден")

        user.full_name = form.full_name
        user.departments = form.departments
        user.blocks = form.blocks
        user.data_scope = form.data_scope
        user.employee_aliases = form.employee_aliases
        user.updated_at = datetime.now(UTC)

        # Права изменились — старые сессии несут старую область видимости в
        # своих открытых вкладках. Пусть перезайдёт.
        _drop_sessions(session, user.id)
        _audit(session, actor, "employee.update", user)
        return serialize(user)


def reset_password(actor: AuthedUser, user_id: int) -> tuple[dict, str]:
    """Выдать новый временный пароль и разлогинить сотрудника отовсюду."""
    password = generate_password()
    with bbc_session() as session:
        user = session.get(BbcUser, user_id)
        if user is None or user.role == "admin":
            raise AuthError("Сотрудник не найден")

        user.password_hash = hash_password(password)
        user.must_change_password = True
        user.password_changed_at = None
        user.updated_at = datetime.now(UTC)
        _drop_sessions(session, user.id)
        _audit(session, actor, "employee.reset_password", user)
        payload = serialize(user)

    return payload, password


def dismiss_employee(actor: AuthedUser, user_id: int) -> dict[str, Any]:
    """Уволить. Мягко: касания остаются в журнале, войти больше нельзя.

    `is_active` — единственная точка, где вход действительно закрывается;
    `status` рядом объясняет причину и держит человека в отдельном списке на
    экране. Две колонки, но одна проверка — иначе однажды забудешь вторую.
    """
    with bbc_session() as session:
        user = session.get(BbcUser, user_id)
        if user is None or user.role == "admin":
            raise AuthError("Сотрудник не найден")

        user.status = "dismissed"
        user.is_active = False
        user.updated_at = datetime.now(UTC)
        _drop_sessions(session, user.id)
        _audit(session, actor, "employee.dismiss", user)
        return serialize(user)


def restore_employee(actor: AuthedUser, user_id: int) -> tuple[dict, str]:
    """Вернуть уволенного. Пароль всегда новый — старый утёк вместе с уходом."""
    password = generate_password()
    with bbc_session() as session:
        user = session.get(BbcUser, user_id)
        if user is None or user.role == "admin":
            raise AuthError("Сотрудник не найден")

        user.status = "active"
        user.is_active = True
        user.password_hash = hash_password(password)
        user.must_change_password = True
        user.password_changed_at = None
        user.updated_at = datetime.now(UTC)
        _audit(session, actor, "employee.restore", user)
        payload = serialize(user)

    return payload, password


def delete_employee(actor: AuthedUser, user_id: int) -> None:
    """Удалить насовсем. Касания остаются: у них FK SET NULL и своя копия имени."""
    with bbc_session() as session:
        user = session.get(BbcUser, user_id)
        if user is None or user.role == "admin":
            raise AuthError("Сотрудник не найден")
        _audit(session, actor, "employee.delete", user)
        _drop_sessions(session, user.id)
        session.delete(user)


def set_own_password(user_id: int, *, current_password: str, new_password: str) -> None:
    """Смена пароля самим сотрудником — в том числе принудительная, при первом входе.

    Текущий пароль спрашивается всегда, включая первый вход: он у человека на
    руках, а без проверки чужая открытая вкладка становится способом сменить
    пароль и забрать учётку.
    """
    from app.bbc.auth import verify_password  # локально: иначе цикл импорта

    if len(new_password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов")

    with bbc_session() as session:
        user = session.get(BbcUser, user_id)
        if user is None:
            raise AuthError("Пользователь не найден")
        if not verify_password(user.password_hash, current_password or ""):
            raise AuthError("Текущий пароль неверен")
        if current_password == new_password:
            raise AuthError("Новый пароль совпадает со старым")

        user.password_hash = hash_password(new_password)
        user.must_change_password = False
        user.password_changed_at = datetime.now(UTC)
        user.updated_at = datetime.now(UTC)
        # Все прочие сессии гасим: временный пароль мог остаться в переписке.
        _drop_sessions(session, user.id)


__all__ = [
    "MIN_PASSWORD_LENGTH",
    "ROLE_PRESETS",
    "EmployeeInput",
    "create_employee",
    "delete_employee",
    "dismiss_employee",
    "generate_password",
    "list_employees",
    "parse_employee_input",
    "reset_password",
    "restore_employee",
    "serialize",
    "set_own_password",
    "update_employee",
]
