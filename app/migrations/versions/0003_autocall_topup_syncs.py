"""autocall_topup_syncs table

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-20 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: the table may already exist via create_all fallback.
    bind = op.get_bind()
    if "autocall_topup_syncs" in inspect(bind).get_table_names():
        return
    op.create_table(
        "autocall_topup_syncs",
        sa.Column("topup_key", sa.String(32), primary_key=True),
        sa.Column("date_time", sa.String(32), nullable=True),
        sa.Column("amount", sa.String(32), nullable=True),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("autocall_topup_syncs")
