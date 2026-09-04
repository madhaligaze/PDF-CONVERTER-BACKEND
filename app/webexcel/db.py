"""Схема `webexcel` — правки финансистов живут здесь, а не в Google.

Отдельная схема по той же причине, что и `bbc`: удалить фичу должно быть можно
одним `DROP SCHEMA webexcel CASCADE`, не задев ни одну чужую таблицу.

Ревизии alembic у схемы сначала не было намеренно: миграция на проде
выполняется до старта приложения, и её падение не даёт подняться вообще ничему.
Раздел выкатывали через `create_all`, чтобы не рисковать всем API ради одной
таблицы.

Ревизия 0008 завела схему под учёт, а риск сняла иначе: она идемпотентна — и
схема, и таблица создаются только если их нет, разрушающих действий в ней нет
вовсе, и на проде, где `create_all` уже отработал, она не делает ничего.

`create_all` ниже остаётся и продолжает работать — он нужен для SQLite, у
которого схем нет и под который ревизии не написаны. На Postgres он теперь
приходит к уже готовой таблице и молча ничего не меняет.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import MetaData, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.database import get_engine

log = logging.getLogger(__name__)

WEBEXCEL_SCHEMA = "webexcel"


class WebExcelBase(DeclarativeBase):
    metadata = MetaData(schema=WEBEXCEL_SCHEMA)


_session_factory: sessionmaker[Session] | None = None
_initialized = False


def webexcel_engine():
    engine = get_engine()
    if engine.dialect.name == "sqlite":
        return engine.execution_options(schema_translate_map={WEBEXCEL_SCHEMA: None})
    return engine


def init_webexcel_database() -> None:
    global _initialized
    if _initialized:
        return
    engine = get_engine()
    if engine.dialect.name != "sqlite":
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{WEBEXCEL_SCHEMA}"'))
    WebExcelBase.metadata.create_all(bind=webexcel_engine())
    _initialized = True


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=webexcel_engine(), autoflush=False, autocommit=False, future=True
        )
    init_webexcel_database()
    return _session_factory


@contextmanager
def webexcel_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = [
    "WEBEXCEL_SCHEMA",
    "WebExcelBase",
    "init_webexcel_database",
    "webexcel_engine",
    "webexcel_session",
]
