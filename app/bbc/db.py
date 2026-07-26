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
    _initialized = True


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
