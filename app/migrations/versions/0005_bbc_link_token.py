"""bbc: keep the referral link address alongside the link

Until now only `sha256(token)` was stored and the address was returned exactly
once, at creation. That made the account page unusable in practice: reload the
page and the link you just issued was gone, with no way to see it again — only
to revoke and re-issue, which invalidated the address already sent out.

The trade-off is explicit and was chosen deliberately: a working link now lives
in the database in the clear, so database access equals access to the
departments' data. The hash stays and remains the lookup key, so token
resolution is unchanged.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-28 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "bbc"


def upgrade() -> None:
    bind = op.get_bind()
    # SQLite has no schemas; that path builds tables via create_all (app/main.py).
    if bind.dialect.name == "sqlite":
        return

    columns = {column["name"] for column in inspect(bind).get_columns("access_links", schema=SCHEMA)}
    if "token" not in columns:
        # Nullable on purpose: links issued before this migration keep working,
        # they simply have no address to show — the page offers «Выдать заново».
        op.add_column(
            "access_links",
            sa.Column("token", sa.String(64), nullable=True),
            schema=SCHEMA,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_column("access_links", "token", schema=SCHEMA)
