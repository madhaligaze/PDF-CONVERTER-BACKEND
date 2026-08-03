"""bbc: учётки сотрудников и журнал касаний по долгам

Две связанные вещи, потому что одна без другой не работает.

1. `users` получает поля сотрудника. До этого не-админ в базе был мёртвой
   записью: `deps.current_scope` выдавал ему пустую область видимости, то есть
   он входил и не видел ничего. Теперь у пользователя есть имя, статус, набор
   разделов, область данных и список написаний в колонке «Сотрудник» — по ним
   и решается, чьи строки он видит.

2. `client_touches` + `touch_files` — первые в модуле таблицы под данные,
   которые ввёл человек, а не прочитал робот из Google Sheets. Касание висит
   на клиенте целиком, а не на договоре: звонят главбуху, а не «по договору
   №247».

Все новые колонки `users` — nullable или с DEFAULT: на проде таблица не пуста,
там живёт админ, и миграция не должна требовать backfill. `data_scope` по
умолчанию `own` — самый узкий вариант: ошибка в этой колонке должна отнимать
доступ, а не раздавать.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-03 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "bbc"

USER_COLUMNS = (
    ("full_name", sa.String(120), "''"),
    ("status", sa.String(16), "'active'"),
    ("must_change_password", sa.Boolean(), "false"),
    ("departments", sa.JSON(), None),
    ("blocks", sa.JSON(), None),
    ("data_scope", sa.String(16), "'own'"),
    ("employee_aliases", sa.JSON(), None),
)


def upgrade() -> None:
    bind = op.get_bind()
    # SQLite has no schemas; that path builds tables via create_all (app/bbc/db.py),
    # and added columns are handled by `_add_missing_sqlite_columns`.
    if bind.dialect.name == "sqlite":
        return

    inspector = inspect(bind)
    existing = {column["name"] for column in inspector.get_columns("users", schema=SCHEMA)}
    for name, type_, server_default in USER_COLUMNS:
        if name in existing:
            continue
        op.add_column(
            "users",
            sa.Column(name, type_, nullable=True, server_default=server_default),
            schema=SCHEMA,
        )

    tables = set(inspector.get_table_names(schema=SCHEMA))

    if "client_touches" not in tables:
        op.create_table(
            "client_touches",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            # Нормализованное имя клиента — тот же ключ, по которому реестр
            # дебиторки группирует договоры в одного должника.
            sa.Column("client_key", sa.String(255), nullable=False),
            sa.Column("client_name", sa.String(255), nullable=False),
            sa.Column(
                "author_user_id",
                sa.Integer(),
                sa.ForeignKey(f"{SCHEMA}.users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            # Дублирует имя автора намеренно: «Жанара написала главбуху» обязано
            # пережить удаление её учётки.
            sa.Column("author_name", sa.String(120), nullable=False),
            sa.Column("contacted_at", sa.Date(), nullable=False),
            sa.Column("contact_role", sa.String(32), nullable=False),
            sa.Column("contact_name", sa.String(120), nullable=False, server_default="''"),
            sa.Column("channel", sa.String(16), nullable=False, server_default="'whatsapp'"),
            sa.Column("summary", sa.Text(), nullable=False, server_default="''"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            schema=SCHEMA,
        )
        op.create_index(
            "ix_bbc_client_touches_client_key", "client_touches", ["client_key"], schema=SCHEMA
        )
        op.create_index(
            "ix_bbc_client_touches_contacted_at", "client_touches", ["contacted_at"], schema=SCHEMA
        )
        op.create_index(
            "ix_bbc_client_touches_contact_role", "client_touches", ["contact_role"], schema=SCHEMA
        )
        op.create_index(
            "ix_bbc_client_touches_author_user_id",
            "client_touches",
            ["author_user_id"],
            schema=SCHEMA,
        )
        # Основной запрос журнала — «история по одному клиенту в обратном
        # хронологическом порядке».
        op.create_index(
            "ix_bbc_touch_client_date",
            "client_touches",
            ["client_key", "contacted_at"],
            schema=SCHEMA,
        )

    if "touch_files" not in tables:
        op.create_table(
            "touch_files",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "touch_id",
                sa.Integer(),
                sa.ForeignKey(f"{SCHEMA}.client_touches.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("filename", sa.String(255), nullable=False),
            sa.Column("content_type", sa.String(128), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
            # s3 → байты в Cloudflare R2, ключ в storage_key;
            # postgres → байты прямо здесь, в data. Файловая система контейнера
            # не используется никогда: на Railway она стирается при деплое.
            sa.Column("storage_backend", sa.String(16), nullable=False, server_default="'postgres'"),
            sa.Column("storage_key", sa.String(255), nullable=True),
            sa.Column("data", sa.LargeBinary(), nullable=True),
            sa.Column(
                "uploaded_by",
                sa.Integer(),
                sa.ForeignKey(f"{SCHEMA}.users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
            schema=SCHEMA,
        )
        op.create_index(
            "ix_bbc_touch_files_touch_id", "touch_files", ["touch_id"], schema=SCHEMA
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    op.drop_table("touch_files", schema=SCHEMA)
    op.drop_table("client_touches", schema=SCHEMA)
    for name, _type, _default in USER_COLUMNS:
        op.drop_column("users", name, schema=SCHEMA)
