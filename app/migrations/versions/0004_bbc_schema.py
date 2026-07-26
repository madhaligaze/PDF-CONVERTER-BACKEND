"""bbc schema: access control + sheet snapshot history

Creates a dedicated `bbc` schema so BBC data never mixes with the converter's
tables in `public` (which already owns `sessions`, `preferences`, `jobs`, …).

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-26 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "bbc"


def upgrade() -> None:
    bind = op.get_bind()
    # SQLite has no schemas; that path uses create_all instead (see app/main.py).
    if bind.dialect.name == "sqlite":
        return

    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')

    # Idempotent: the tables may already exist via the create_all fallback.
    existing = set(inspect(bind).get_table_names(schema=SCHEMA))

    if "users" not in existing:
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("username", sa.String(64), nullable=False, unique=True),
            sa.Column("password_hash", sa.String(255), nullable=False),
            sa.Column("role", sa.String(16), nullable=False, server_default="admin"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
            schema=SCHEMA,
        )
        op.create_index("ix_bbc_users_username", "users", ["username"], schema=SCHEMA)

    if "user_sessions" not in existing:
        op.create_table(
            "user_sessions",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ip", sa.String(64), nullable=True),
            sa.Column("user_agent", sa.String(255), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], [f"{SCHEMA}.users.id"], ondelete="CASCADE"),
            schema=SCHEMA,
        )
        op.create_index("ix_bbc_sessions_user", "user_sessions", ["user_id"], schema=SCHEMA)
        op.create_index("ix_bbc_sessions_token", "user_sessions", ["token_hash"], schema=SCHEMA)
        op.create_index("ix_bbc_sessions_expires", "user_sessions", ["expires_at"], schema=SCHEMA)

    if "access_links" not in existing:
        op.create_table(
            "access_links",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("label", sa.String(32), nullable=False),
            sa.Column("scope", sa.JSON(), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column("created_by", sa.Integer(), nullable=True),
            # NULL = permanent ("публичная") link; a value makes it temporary.
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
            sa.ForeignKeyConstraint(["created_by"], [f"{SCHEMA}.users.id"], ondelete="SET NULL"),
            schema=SCHEMA,
        )
        op.create_index("ix_bbc_links_label", "access_links", ["label"], schema=SCHEMA)
        op.create_index("ix_bbc_links_token", "access_links", ["token_hash"], schema=SCHEMA)

    if "audit_log" not in existing:
        op.create_table(
            "audit_log",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("actor_type", sa.String(16), nullable=False),
            sa.Column("actor_id", sa.String(64), nullable=True),
            sa.Column("action", sa.String(64), nullable=False),
            sa.Column("scope", sa.JSON(), nullable=True),
            sa.Column("ip", sa.String(64), nullable=True),
            sa.Column("user_agent", sa.String(255), nullable=True),
            sa.Column("detail", sa.Text(), nullable=True),
            schema=SCHEMA,
        )
        op.create_index("ix_bbc_audit_at", "audit_log", ["at"], schema=SCHEMA)
        op.create_index("ix_bbc_audit_actor", "audit_log", ["actor_id"], schema=SCHEMA)
        op.create_index("ix_bbc_audit_action", "audit_log", ["action"], schema=SCHEMA)

    if "sheet_snapshots" not in existing:
        op.create_table(
            "sheet_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("source", sa.String(32), nullable=False),
            sa.Column("spreadsheet_id", sa.String(64), nullable=False),
            sa.Column("worksheet", sa.String(128), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column(
                "fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("payload", sa.JSON(), nullable=False),
            schema=SCHEMA,
        )
        op.create_index("ix_bbc_snapshot_source", "sheet_snapshots", ["source"], schema=SCHEMA)
        op.create_index("ix_bbc_snapshot_hash", "sheet_snapshots", ["content_hash"], schema=SCHEMA)
        op.create_index("ix_bbc_snapshot_revision", "sheet_snapshots", ["revision"], schema=SCHEMA)
        op.create_index("ix_bbc_snapshot_fetched", "sheet_snapshots", ["fetched_at"], schema=SCHEMA)
        op.create_index(
            "ix_bbc_snapshot_source_revision",
            "sheet_snapshots",
            ["source", "revision"],
            schema=SCHEMA,
        )

    if "sync_runs" not in existing:
        op.create_table(
            "sync_runs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("changed_sources", sa.JSON(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            schema=SCHEMA,
        )
        op.create_index("ix_bbc_sync_started", "sync_runs", ["started_at"], schema=SCHEMA)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    # Dropping the schema removes every BBC table in one step — the whole point
    # of keeping them out of `public`.
    op.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
