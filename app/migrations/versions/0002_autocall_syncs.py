"""autocall_syncs table

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-19 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: the table may already exist via create_all fallback.
    bind = op.get_bind()
    if "autocall_syncs" in inspect(bind).get_table_names():
        return
    op.create_table(
        "autocall_syncs",
        sa.Column("autocall_id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("created_at_src", sa.String(32), nullable=True),
        sa.Column("final_cost", sa.String(32), nullable=True),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="success"),
    )


def downgrade() -> None:
    op.drop_table("autocall_syncs")
