from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import settings


log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_engine = None
_session_factory = None
_resolved_database_url = None
_initialized = False


def _default_sqlite_url() -> str:
    db_path = Path(__file__).resolve().parents[2] / "data" / "pdf_converter.db"
    return f"sqlite:///{db_path.as_posix()}"


def _pick_database_url() -> str:
    if os.getenv("PYTEST_CURRENT_TEST"):
        return _default_sqlite_url()
    return settings.database_url


def _build_engine(url: str) -> Engine:
    """Create an engine with production-grade pooling for the given URL."""
    if url.startswith("sqlite"):
        return create_engine(
            url,
            future=True,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False},
        )
    return create_engine(
        url,
        future=True,
        pool_pre_ping=True,      # validate connections before use (drops stale ones)
        pool_recycle=1800,       # recycle after 30 min — Railway closes idle connections
        pool_size=5,
        max_overflow=10,
        pool_timeout=30,
        connect_args={"connect_timeout": 10},
    )


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def _connect_primary(url: str) -> Engine:
    """Connect to the primary database, retrying with exponential backoff.

    Cold starts on Railway can race the internal `*.railway.internal` DNS /
    Postgres readiness; retrying avoids a spurious fallback to SQLite.
    """
    engine = _build_engine(url)
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return engine


def _is_production() -> bool:
    return settings.environment.strip().lower() == "production"


def get_engine() -> Engine:
    global _engine, _resolved_database_url
    if _engine is not None:
        return _engine

    primary_url = _pick_database_url()
    safe_url = primary_url.split("@")[-1] if "@" in primary_url else "database"
    log.info("Connecting to database at %s", safe_url)

    try:
        _engine = _connect_primary(primary_url)
        _resolved_database_url = primary_url
        log.info("Primary database connection established")
        return _engine
    except Exception as exc:
        # In production we must NOT silently fall back to an ephemeral SQLite file:
        # that hides the real history/sessions and loses new writes on restart.
        # Fail hard so Railway restarts the container (restartPolicy = ON_FAILURE).
        if _is_production():
            log.error("Primary DB unreachable in production after retries: %s", exc)
            raise
        log.warning("Primary DB connection failed, falling back to SQLite (dev only): %s", exc)
        fallback_url = _default_sqlite_url()
        _engine = _build_engine(fallback_url)
        _resolved_database_url = fallback_url
        return _engine


def get_resolved_database_url() -> str:
    get_engine()
    return _resolved_database_url or _default_sqlite_url()


def get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False, future=True)
    init_database()
    return _session_factory


@contextmanager
def db_session() -> Session:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_database() -> None:
    global _initialized
    if _initialized:
        return

    Base.metadata.create_all(bind=get_engine())
    _initialized = True
