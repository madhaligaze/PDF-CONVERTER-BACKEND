"""Схема приводится к тому, что объявляют модели

Что случилось
─────────────
Первый же прогон `tests/test_migrations.py` на чистой базе нашёл 38
расхождений между тем, что создают ревизии, и тем, что объявлено в моделях.
Копились они долго и молча — ни одно не давало ни ошибки, ни симптома.

1. **Таблицы `fx_rates` нет ни в одной ревизии.** Модель `FxRateRecord`
   добавили, миграцию не написали. На чистом развёртывании таблицы не будет, и
   курсы валют отвалятся. Сейчас она существует только там, где когда-то
   отработал откат на `create_all` — тот самый, который убран в этой же серии
   правок. То есть починка отката без этой ревизии сломала бы новый деплой.

2. **Имена индексов.** Ревизии называли их коротко и по-своему
   (`ix_bbc_audit_at`), а модели через `index=True` порождают имя по правилу
   SQLAlchemy (`ix_bbc_audit_log_at`). Поведение одинаковое, но пока имена
   расходятся, `--autogenerate` будет вечно предлагать удалить одни индексы и
   создать другие, а проверка «модели не разошлись с миграциями» не сможет
   пройти никогда.

3. **UNIQUE: ограничение против индекса.** Ревизии делали
   `UniqueConstraint`, модели через `unique=True, index=True` просят
   уникальный индекс.

4. **NOT NULL у четырёх колонок `bbc.users`.** Вот это уже не косметика:
   модели считают `status` и `data_scope` непустыми и на этом строят область
   видимости, а база разрешала NULL. NULL в `data_scope` — это сотрудник без
   определённой области доступа.

Безопасность
────────────
Ревизия идемпотентна: каждое действие проверяет текущее состояние. Ни одного
`DROP TABLE` и ни одного удаления данных здесь нет. Переименование индекса в
Postgres меняет только метаданные. Перед `SET NOT NULL` пустые значения
заполняются тем же умолчанием, что стоит в модели, — на существующих строках
их и так быть не должно, но полагаться на это нельзя.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-03 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BBC = "bbc"

#: (таблица, старое имя, новое имя) — обычные, не уникальные индексы.
RENAMES: tuple[tuple[str, str, str], ...] = (
    ("access_links", "ix_bbc_links_label", "ix_bbc_access_links_label"),
    ("audit_log", "ix_bbc_audit_action", "ix_bbc_audit_log_action"),
    ("audit_log", "ix_bbc_audit_actor", "ix_bbc_audit_log_actor_id"),
    ("audit_log", "ix_bbc_audit_at", "ix_bbc_audit_log_at"),
    ("sheet_snapshots", "ix_bbc_snapshot_fetched", "ix_bbc_sheet_snapshots_fetched_at"),
    ("sheet_snapshots", "ix_bbc_snapshot_hash", "ix_bbc_sheet_snapshots_content_hash"),
    ("sheet_snapshots", "ix_bbc_snapshot_revision", "ix_bbc_sheet_snapshots_revision"),
    ("sheet_snapshots", "ix_bbc_snapshot_source", "ix_bbc_sheet_snapshots_source"),
    ("sync_runs", "ix_bbc_sync_started", "ix_bbc_sync_runs_started_at"),
    ("user_sessions", "ix_bbc_sessions_expires", "ix_bbc_user_sessions_expires_at"),
    ("user_sessions", "ix_bbc_sessions_user", "ix_bbc_user_sessions_user_id"),
)

#: (таблица, колонка, старый индекс, старое ограничение, новый уникальный индекс).
#: Модель просит `unique=True, index=True` — то есть один уникальный индекс, а
#: не пару «обычный индекс + отдельное ограничение».
UNIQUES: tuple[tuple[str, str, str | None, str, str], ...] = (
    (
        "access_links", "token_hash",
        "ix_bbc_links_token", "access_links_token_hash_key",
        "ix_bbc_access_links_token_hash",
    ),
    (
        "user_sessions", "token_hash",
        "ix_bbc_sessions_token", "user_sessions_token_hash_key",
        "ix_bbc_user_sessions_token_hash",
    ),
    (
        "users", "username",
        "ix_bbc_users_username", "users_username_key",
        "ix_bbc_users_username",
    ),
)

#: Колонки `bbc.users`, которые модель объявляет непустыми. Значение —
#: то же умолчание, что и в модели: им заполняется то, что успело стать NULL.
NOT_NULL: tuple[tuple[str, str], ...] = (
    ("full_name", "''"),
    ("status", "'active'"),
    ("must_change_password", "false"),
    ("data_scope", "'own'"),
)


def _indexes(inspector, table: str) -> set[str]:
    return {ix["name"] for ix in inspector.get_indexes(table, schema=BBC)}


def _constraints(inspector, table: str) -> set[str]:
    return {
        c["name"] for c in inspector.get_unique_constraints(table, schema=BBC)
    }


def upgrade() -> None:
    bind = op.get_bind()

    # У SQLite схем нет: там всё собирается через create_all из тех же моделей,
    # то есть расхождения, которое чинит эта ревизия, там не возникает.
    if bind.dialect.name == "sqlite":
        return

    inspector = inspect(bind)
    tables = set(inspector.get_table_names(schema=BBC))

    # ── 1. Пропущенная таблица курсов валют ────────────────────────────────
    if "fx_rates" not in set(inspector.get_table_names()):
        op.create_table(
            "fx_rates",
            sa.Column("id", sa.String(length=32), nullable=False),
            sa.Column("code", sa.String(length=8), nullable=False),
            sa.Column("rate_date", sa.String(length=10), nullable=False),
            sa.Column("rate", sa.Float(), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_fx_rates_code", "fx_rates", ["code"])
        op.create_index("ix_fx_rates_rate_date", "fx_rates", ["rate_date"])

    # ── 2. Имена индексов ──────────────────────────────────────────────────
    for table, old, new in RENAMES:
        if table not in tables:
            continue
        existing = _indexes(inspector, table)
        if old in existing and new not in existing:
            op.execute(f'ALTER INDEX "{BBC}"."{old}" RENAME TO "{new}"')

    # ── 3. UNIQUE: ограничение → уникальный индекс ─────────────────────────
    for table, column, old_index, old_constraint, new_index in UNIQUES:
        if table not in tables:
            continue
        if old_constraint in _constraints(inspector, table):
            op.drop_constraint(old_constraint, table, schema=BBC, type_="unique")
        existing = _indexes(inspector, table)
        if old_index and old_index in existing:
            op.drop_index(old_index, table_name=table, schema=BBC)
        if new_index not in _indexes(inspect(bind), table):
            op.create_index(new_index, table, [column], unique=True, schema=BBC)

    # ── 4. NOT NULL у колонок сотрудника ───────────────────────────────────
    if "users" in tables:
        for column, default in NOT_NULL:
            op.execute(
                f'UPDATE "{BBC}"."users" SET "{column}" = {default} '
                f'WHERE "{column}" IS NULL'
            )
            op.execute(
                f'ALTER TABLE "{BBC}"."users" ALTER COLUMN "{column}" SET NOT NULL'
            )


def downgrade() -> None:
    """Возврат к прежним именам. Данные не трогаются и здесь.

    `fx_rates` не удаляется: таблицу эта ревизия создала только потому, что её
    забыли создать раньше, и в ней лежит кэш курсов. Откат ревизии — не повод
    его терять.
    """
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    inspector = inspect(bind)
    tables = set(inspector.get_table_names(schema=BBC))

    if "users" in tables:
        for column, _ in NOT_NULL:
            op.execute(
                f'ALTER TABLE "{BBC}"."users" ALTER COLUMN "{column}" DROP NOT NULL'
            )

    for table, column, old_index, old_constraint, new_index in UNIQUES:
        if table not in tables:
            continue
        if new_index in _indexes(inspector, table):
            op.drop_index(new_index, table_name=table, schema=BBC)
        op.create_unique_constraint(old_constraint, table, [column], schema=BBC)
        if old_index and old_index != new_index:
            op.create_index(old_index, table, [column], schema=BBC)

    for table, old, new in RENAMES:
        if table not in tables:
            continue
        existing = _indexes(inspect(bind), table)
        if new in existing and old not in existing:
            op.execute(f'ALTER INDEX "{BBC}"."{new}" RENAME TO "{old}"')
