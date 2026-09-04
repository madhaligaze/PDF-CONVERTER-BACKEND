"""webexcel: схема раздела «Таблицы» переходит под управление alembic

Почему это делается только сейчас
─────────────────────────────────
Ревизии у `webexcel` не было намеренно, и причина была здравая: миграция на
проде выполняется до старта приложения, и её падение не даёт подняться вообще
ничему. Раздел выкатывали через `create_all`, чтобы не рисковать всем API ради
одной таблицы.

Цена этого решения — схема, которой нет ни в одной ревизии. Пока в `webexcel`
лежали личные черновики, это ничего не стоило: потеряли и импортировали заново.
Дальше в Postgres переезжают данные, которых больше нигде нет, и таблица вне
миграций становится тем самым местом, где схема тихо расходится с кодом.

Как снят риск, от которого защищались
──────────────────────────────────────
Ревизия идемпотентна: и схема, и таблица создаются только если их нет. На
проде, где `create_all` уже всё создал, она не делает ничего и упасть не может.
На чистой базе — создаёт. Ни одного разрушающего действия здесь нет вовсе.

`downgrade` не удаляет таблицу: в ней лежат сохранённые книги, а откат ревизии
не повод их терять. Он только снимает схему с учёта.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-03 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "webexcel"
TABLE = "books"


def upgrade() -> None:
    bind = op.get_bind()

    # У SQLite схем нет: там раздел поднимается через create_all
    # (app/webexcel/db.py), и эта ревизия ему не нужна.
    if bind.dialect.name == "sqlite":
        return

    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')

    inspector = inspect(bind)
    if TABLE in inspector.get_table_names(schema=SCHEMA):
        # Прод, где таблицу уже создал create_all. Ничего не трогаем — ревизия
        # здесь нужна только чтобы дальше схема жила под учётом.
        return

    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        # «Новая таблица» или «импортирована из Google» — различаются в списке.
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("origin_spreadsheet_id", sa.String(length=64), nullable=False),
        sa.Column("origin_title", sa.String(length=200), nullable=False),
        sa.Column("origin_tabs", sa.JSON(), nullable=True),
        sa.Column("snapshot", sa.JSON(), nullable=True),
        sa.Column("updated_by", sa.String(length=120), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    # Таблица не удаляется намеренно: в ней сохранённые книги пользователей, а
    # откат ревизии — не причина их терять. Снимаем только с учёта.
    pass
