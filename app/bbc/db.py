"""Database foundation for the BBC module — an isolated `bbc` Postgres schema.

Why a separate schema instead of more tables next to the others: the converter
already owns 13 tables in `public` (`sessions`, `preferences`, `jobs`, `fx_rates`,
`autocall_syncs`, …). BBC needs its own `sessions` and will eventually hold large
domain tables, so keeping everything under `bbc.*` means the two data sets can
never collide — and dropping the feature is `DROP SCHEMA bbc CASCADE`.

The engine and its connection pool are reused from `app.core.database`; we never
open a second connection. SQLite (used by the test suite) has no schemas, so the
schema name is translated away with `schema_translate_map`.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from collections.abc import Iterator

from sqlalchemy import MetaData, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.database import get_engine

log = logging.getLogger(__name__)

BBC_SCHEMA = "bbc"


class BbcBase(DeclarativeBase):
    """Declarative base bound to the `bbc` schema.

    Separate from `app.core.database.Base` on purpose — the two metadata objects
    are created and migrated independently.
    """

    metadata = MetaData(schema=BBC_SCHEMA)


_session_factory: sessionmaker[Session] | None = None
_initialized = False


def bbc_engine() -> Engine:
    """Shared engine, with the schema translated away on SQLite."""
    engine = get_engine()
    if engine.dialect.name == "sqlite":
        # SQLite has no schemas: render `bbc.users` as plain `users` in that file.
        return engine.execution_options(schema_translate_map={BBC_SCHEMA: None})
    return engine


def init_bbc_database() -> None:
    """Create the schema and any missing tables. Safe to call repeatedly.

    Mirrors `app.core.database.init_database`: alembic owns the real migrations,
    this is the create_all fallback that also keeps the SQLite test path working.
    """
    global _initialized
    if _initialized:
        return

    engine = get_engine()
    if engine.dialect.name != "sqlite":
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{BBC_SCHEMA}"'))

    BbcBase.metadata.create_all(bind=bbc_engine())
    if engine.dialect.name == "sqlite":
        _add_missing_sqlite_columns()
    _initialized = True


def _add_missing_sqlite_columns() -> None:
    """Bring an existing SQLite file up to the current model.

    `create_all` only creates missing *tables* — a column added to a model never
    reaches a database file that already exists. On Postgres alembic handles
    that; the SQLite path (dev machine and the test suite) had no equivalent, so
    the first added column silently broke every insert into that table. This
    closes the gap for the one case it applies to, in the same spirit as the
    create_all fallback itself.

    **Каждая новая колонка на существующей таблице должна попасть сюда** — и
    отдельно в ревизию alembic для Postgres. Забыть про этот список значит
    сломать тесты на уже существующем `backend/data/pdf_converter.db`, причём
    молча: create_all пройдёт, а INSERT упадёт на «no such column».
    """
    engine = bbc_engine()
    with engine.begin() as connection:
        for table, column, ddl in (
            ("access_links", "token", "VARCHAR(64)"),
            ("users", "full_name", "VARCHAR(120) DEFAULT ''"),
            ("users", "status", "VARCHAR(16) DEFAULT 'active'"),
            ("users", "must_change_password", "BOOLEAN DEFAULT 0"),
            ("users", "departments", "JSON"),
            ("users", "blocks", "JSON"),
            ("users", "data_scope", "VARCHAR(16) DEFAULT 'own'"),
            ("users", "employee_aliases", "JSON"),
        ):
            rows = connection.execute(text(f"PRAGMA table_info({table})")).fetchall()
            if not rows:  # table not there at all — create_all owns that case
                continue
            if any(row[1] == column for row in rows):
                continue
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            log.info("BBC/sqlite: added missing column %s.%s", table, column)


def get_bbc_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=bbc_engine(), autoflush=False, autocommit=False, future=True
        )
    init_bbc_database()
    return _session_factory


@contextmanager
def bbc_session() -> Iterator[Session]:
    """Transactional scope around a BBC database session."""
    session = get_bbc_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = ["BBC_SCHEMA", "BbcBase", "bbc_engine", "bbc_session", "init_bbc_database"]
