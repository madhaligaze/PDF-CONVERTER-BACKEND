"""Учётки и компании раздела «Финансы» — модель.

Почему у раздела своя авторизация, а не общая с дашбордом
────────────────────────────────────────────────────────
Первая версия пускала в раздел по праву `finance` учётки BBC Dashboard. Это
было неверно по существу: в «Финансах» каждая компания регистрируется сама,
ведёт свой учёт и к BBC отношения не имеет. Более того, BBC сам может однажды
переехать сюда — и тогда зависимость смотрела бы в обратную сторону, от общего
к частному.

Поэтому здесь свои `users`, `memberships`, `sessions`, а `workspaces` из
`models.py` становится компанией: у неё появляется владелец. Пакет
`app.finance` после этого не импортирует `app.bbc` ни одной строкой — это
проверяется тестом `test_finance_isolated`.

Три решения, которые стоит объяснить
────────────────────────────────────
**1. Пользователь и компания связаны таблицей, а не колонкой.** Один человек
ведёт несколько компаний (бухгалтер на подряде — десяток), и в Finmap это
устроено так же: переключатель компаний в шапке. Колонка `company_id` у
пользователя заставила бы завести ему вторую учётку с тем же паролем.

**2. Сессия живёт на сервере, в cookie уходит только случайный токен.** В базе
лежит его SHA-256: дамп базы не должен давать работающих сессий. Тот же принцип,
что в дашборде, — он проверен и правильный.

**3. Роль хранится на членстве, а не на пользователе.** Один и тот же человек —
владелец своей компании и приглашённый бухгалтер в чужой. Роль на пользователе
означала бы, что права в одной компании тянутся в другую.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.finance.db import FinanceBase

#: Роли в компании. Список короткий намеренно: у Finmap роли настраиваются
#: галочками по разделам, и это следующий шаг. Пока четыре понятных уровня.
#:
#:   owner      — создал компанию: всё, включая счета, людей и удаление;
#:   admin      — всё, кроме удаления компании и смены владельца;
#:   accountant — ведёт учёт: операции, импорт, справочники; счета не заводит;
#:   viewer     — только смотрит отчёты.
MEMBER_ROLES = ("owner", "admin", "accountant", "viewer")


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class FinanceUser(FinanceBase):
    """Человек. Один на все компании, где он участвует."""

    __tablename__ = "users"
    __table_args__ = (
        # Почта — логин, поэтому уникальна в нормализованном виде.
        sa.UniqueConstraint("email_normalized", name="uq_users_email_normalized"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(sa.Text)
    #: Почта в нижнем регистре и без обрамляющих пробелов. Отдельной колонкой, а
    #: не функциональным индексом: на SQLite в тестах функциональные индексы
    #: ведут себя иначе, а поиск по логину обязан быть одинаковым везде.
    email_normalized: Mapped[str] = mapped_column(sa.Text)
    password_hash: Mapped[str] = mapped_column(sa.Text)
    full_name: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    #: `active` | `blocked`. Блокировка не удаляет: за человеком остаются
    #: подписи под операциями, и их нельзя обезличить задним числом.
    status: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'active'"))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    #: Пароль выдан владельцем и должен быть сменён при первом входе.
    must_change_password: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))


class FinanceMembership(FinanceBase):
    """Участие человека в компании и его роль там."""

    __tablename__ = "memberships"
    __table_args__ = (
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'accountant', 'viewer')", name="membership_role"
        ),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_memberships_workspace_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'accountant'"))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    #: Кто пригласил. Нужно, чтобы владелец понимал, откуда в его компании
    #: взялся человек, которого он не помнит.
    invited_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, sa.ForeignKey("users.id"))


class FinanceSession(FinanceBase):
    """Серверная сессия. В cookie уходит токен, здесь лежит его SHA-256."""

    __tablename__ = "sessions"
    __table_args__ = (
        sa.UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
        sa.Index("ix_sessions_user_id_expires_at", "user_id", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(sa.Text)
    #: Компания, выбранная в этой сессии. Переключатель компаний меняет её, а не
    #: заводит вторую сессию: человек остаётся тем же, меняется только контекст.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    #: Чем зашли — чтобы человек узнал свои сессии в списке и отозвал чужую.
    user_agent: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))


__all__ = ["MEMBER_ROLES", "FinanceMembership", "FinanceSession", "FinanceUser"]
