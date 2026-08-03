"""bbc: имя пользователя приезжало как «''» вместо пустого

В ревизии 0006 значения по умолчанию для новых колонок `users` заданы обычными
питоновскими строками — `server_default="''"`, `server_default="'active'"`.
SQLAlchemy принимает такую строку за литерал, который надо процитировать, и в
DDL уезжает `DEFAULT ''''''` и `DEFAULT '''active'''`. То есть в базу
записываются два апострофа и слово в апострофах, а не пустая строка и `active`.

Видно это стало по журналу касаний: `full_name.strip() or username` возвращал не
логин, а «''», и подпись под записью читалась как «''» вместо имени человека.

`data_scope` пострадал так же, но там это ничего не открыло: неизвестное
значение по правилу модуля схлопывается в самый узкий вариант, а не в широкий.

Ревизия 0006 исправлена для чистых баз, но она уже применена на проде и там не
перезапустится — поэтому здесь и переписываются значения по умолчанию и
чинятся уже записанные строки. На чистой базе все UPDATE ниже — пустые.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-03 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "bbc"

# sa.text(), а не голая строка: ровно из-за неё и понадобилась эта ревизия.
DEFAULTS = (
    ("full_name", sa.text("''")),
    ("status", sa.text("'active'")),
    ("data_scope", sa.text("'own'")),
)


def upgrade() -> None:
    bind = op.get_bind()
    # SQLite строит таблицы через create_all и питоновские значения по
    # умолчанию — там этой болезни нет.
    if bind.dialect.name == "sqlite":
        return

    for column, default in DEFAULTS:
        op.alter_column("users", column, server_default=default, schema=SCHEMA)

    # Те же процитированные литералы попали в таблицы касаний. Данных это не
    # испортило — приложение всегда пишет эти колонки явно, — но оставлять
    # заряженную мину в схеме нельзя: первый же INSERT без storage_backend
    # положил бы туда «'postgres'» вместе с апострофами, и файл перестал бы
    # находиться в хранилище.
    for table, column, default in (
        ("client_touches", "contact_name", sa.text("''")),
        ("client_touches", "channel", sa.text("'whatsapp'")),
        ("client_touches", "summary", sa.text("''")),
        ("touch_files", "size_bytes", sa.text("0")),
        ("touch_files", "storage_backend", sa.text("'postgres'")),
    ):
        op.alter_column(table, column, server_default=default, schema=SCHEMA)

    # Уже записанное. Сравнение с литералом из шести апострофов — это строка
    # ровно из двух апострофов, того самого мусора.
    op.execute(sa.text(f"""UPDATE "{SCHEMA}".users SET full_name = '' WHERE full_name = ''''''"""))
    op.execute(
        sa.text(
            f"""UPDATE "{SCHEMA}".users
                   SET status = 'active'
                 WHERE status IS NULL OR status NOT IN ('active', 'dismissed')"""
        )
    )
    op.execute(
        sa.text(
            f"""UPDATE "{SCHEMA}".users
                   SET data_scope = CASE WHEN role = 'admin' THEN 'all' ELSE 'own' END
                 WHERE data_scope IS NULL
                    OR data_scope NOT IN ('own', 'department', 'all')"""
        )
    )

    # Подписи под уже записанными касаниями — тот же мусор, скопированный в
    # момент записи. Восстанавливаем по логину автора; там, где учётки уже нет,
    # оставляем как есть — врать про авторство хуже, чем показать пустое.
    op.execute(
        sa.text(
            f"""UPDATE "{SCHEMA}".client_touches AS ct
                   SET author_name = u.username
                  FROM "{SCHEMA}".users AS u
                 WHERE ct.author_user_id = u.id
                   AND (ct.author_name = '''''' OR ct.author_name IS NULL OR ct.author_name = '')"""
        )
    )


def downgrade() -> None:
    # Возвращать битые значения по умолчанию незачем.
    pass
