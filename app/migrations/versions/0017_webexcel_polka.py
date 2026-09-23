"""webexcel: полка вместо зеркала книг Google

«Таблицы» перестали быть зеркалом книг BBC и стали местом для своих таблиц.
Прежняя `webexcel.books` хранила снимки, собранные из Google сервисным
аккаунтом дашборда; на проде в ней не было ничего нужного (подтверждено
владельцем 23.09.2026), поэтому она удаляется, а не переносится.

Новая `webexcel.shelf` держит снимок строкой (`Text`), а не JSON: сервер снимок
не разбирает, см. `app/webexcel/models.py`. Имя `shelf`, а не `books`, — чтобы
на SQLite не сталкиваться с одноимённой таблицей модуля «Книги».

Ревизия идемпотентна в ту же сторону, что и 0008: таблица создаётся, только
если её нет.

Revision ID: 0017
Revises: 0016
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "webexcel"


def upgrade() -> None:
    bind = op.get_bind()
    # У SQLite схем нет: там раздел поднимается через create_all (app/webexcel/db.py).
    if bind.dialect.name == "sqlite":
        return

    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
    op.execute(f'DROP TABLE IF EXISTS "{SCHEMA}"."books"')

    if "shelf" in inspect(bind).get_table_names(schema=SCHEMA):
        return

    op.create_table(
        "shelf",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("source_ref", sa.String(length=500), nullable=False),
        sa.Column("sheets", sa.JSON(), nullable=True),
        sa.Column("snapshot", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_table("shelf", schema=SCHEMA)
