"""Схема `books` — внутренние книги компании.

Отдельная схема по той же причине, что и `bbc` с `webexcel`: удалить модуль
должно быть можно одним `DROP SCHEMA books CASCADE`, не задев ни одну чужую
таблицу.

Чем этот модуль отличается от соседей по устройству
───────────────────────────────────────────────────
У `bbc/db.py` есть `_add_missing_sqlite_columns()` — список колонок, который
ведут руками, потому что `create_all` создаёт только недостающие *таблицы*, а
колонку, добавленную в модель, в существующий файл SQLite не доносит. Список
приходится помнить и дополнять; забыть про него — значит сломать тесты молча.

Здесь такого списка нет и не будет. Схемой владеет alembic, а `create_all`
ниже нужен ровно для одного: собрать таблицы в чистой временной базе теста.
Правило простое: **колонка, добавленная в модель, попадает в ревизию — другого
пути нет.** Если сюда однажды захочется дописать список колонок, это будет
означать, что ревизию не написали.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import MetaData, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.database import get_engine

log = logging.getLogger(__name__)

BOOKS_SCHEMA = "books"

#: Единая схема имён для ограничений и индексов.
#:
#: Заведена сразу, а не потом. У соседних модулей её нет, и цена этого уже
#: известна: ревизии называли индексы по-своему, модели — по правилу
#: SQLAlchemy, и 24 индекса разошлись так, что `--autogenerate` предлагал
#: удалить одни и создать другие на каждом прогоне. Правки это не ломало, но
#: проверка «модели совпадают с миграциями» не могла пройти никогда.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}


class BooksBase(DeclarativeBase):
    """Declarative base, привязанный к схеме `books`."""

    metadata = MetaData(schema=BOOKS_SCHEMA, naming_convention=NAMING_CONVENTION)


_session_factory: sessionmaker[Session] | None = None
_initialized = False


def books_engine() -> Engine:
    """Общий движок; на SQLite схема стирается — там её не существует."""
    engine = get_engine()
    if engine.dialect.name == "sqlite":
        return engine.execution_options(schema_translate_map={BOOKS_SCHEMA: None})
    return engine


def init_books_database() -> None:
    """Создать схему и недостающие таблицы. Только для чистой базы.

    На Postgres схемой владеет alembic, и эта функция там приходит к уже
    готовым таблицам. Нужна она для SQLite в тестах, где базу создают с нуля.
    """
    global _initialized
    if _initialized:
        return

    engine = get_engine()
    if engine.dialect.name != "sqlite":
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{BOOKS_SCHEMA}"'))

    BooksBase.metadata.create_all(bind=books_engine())
    _initialized = True


def get_books_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=books_engine(), autoflush=False, autocommit=False, future=True
        )
    init_books_database()
    return _session_factory


@contextmanager
def books_session() -> Iterator[Session]:
    """Транзакция вокруг сессии модуля."""
    session = get_books_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = [
    "BOOKS_SCHEMA",
    "BooksBase",
    "NAMING_CONVENTION",
    "books_engine",
    "books_session",
    "init_books_database",
]
