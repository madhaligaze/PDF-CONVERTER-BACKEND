"""books.fields.origin — откуда взялась колонка

Зачем колонка понадобилась
──────────────────────────
Колонки книги стало можно заводить прямо в таблице, а не только импортом из
Google. Отсюда сразу проблема, которой раньше не было: `sync_fields` помечает
удалённым всё, чего не оказалось в прочитанном листе, — а колонки, заведённой у
нас, в листе Google не окажется никогда. Первый же повторный импорт спрятал бы
всё, что добавили руками, и не сказал бы об этом ни слова.

Почему `server_default`, а потом снятие
───────────────────────────────────────
В таблице уже лежат колонки — все они пришли импортом, то есть `source`.
Добавить NOT NULL без умолчания на непустой таблице нельзя. Умолчание ставится
на время заполнения и снимается: дальше значение обязан задавать код, а не
база. Оставленное умолчание — это тихое согласие на «а мы забыли передать».

`sa.text('source')`, а не голая строка: SQLAlchemy цитирует строковый
`server_default` как есть, и в базу уезжает литерал вместе с кавычками. На этом
уже спотыкались (см. память проекта «Ловушки прода»).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "books"


def upgrade() -> None:
    op.add_column(
        "fields",
        sa.Column(
            "origin",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'source'"),
        ),
        schema=SCHEMA,
    )
    op.alter_column("fields", "origin", server_default=None, schema=SCHEMA)
    op.create_check_constraint(
        "ck_fields_origin",
        "fields",
        "origin IN ('source', 'app')",
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint("ck_fields_origin", "fields", schema=SCHEMA, type_="check")
    op.drop_column("fields", "origin", schema=SCHEMA)
