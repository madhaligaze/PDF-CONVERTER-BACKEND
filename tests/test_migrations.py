"""Схема в базе и модели в коде обязаны совпадать.

Зачем этот тест существует
──────────────────────────
Схема расходилась с кодом дважды, и оба раза молча.

1. `_run_migrations` при падении alembic откатывалась на `create_all`: таблицы
   появлялись, `alembic_version` оставалась старой, приложение отвечало 200.
   Следующая ревизия падала уже на «таблица существует» и снова уходила в
   откат.
2. Схема `webexcel` вообще жила вне ревизий и создавалась при первом обращении
   к разделу.

Ни то, ни другое не давало ни ошибки, ни симптома — расхождение обнаруживалось
бы только тогда, когда на проде не хватило бы колонки. Пока в Postgres лежал
кэш прочитанного из Google, цена была невелика. Теперь там данные, которых
больше нигде нет.

Тест ловит ровно один класс ошибок: «модель поправили, ревизию написать
забыли». Он прогоняет миграции на чистой базе и спрашивает у alembic, видит ли
тот разницу с моделями. Любая разница — незаписанная ревизия.

Почему тест пропускается без Postgres
─────────────────────────────────────
Ревизии написаны под Postgres со схемами, которых у SQLite нет, и проверять их
на SQLite бессмысленно. Пропуск здесь — не молчание об ошибке: без базы тест
не может ни подтвердить, ни опровергнуть, и врать «прошло» он не должен.
Запускать так:

    docker compose up -d postgres
    TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5434/pdf_converter \\
        uv run pytest tests/test_migrations.py
"""
from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.migrations

ENV_VARS = ("TEST_DATABASE_URL", "DATABASE_URL")


def _base_url() -> str | None:
    for name in ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value.startswith("postgresql"):
            return value
    return None


def _alembic_config(url: str):
    from pathlib import Path

    from alembic.config import Config

    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "app" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture(scope="module")
def scratch_database() -> str:
    """Чистая база под прогон, удаляется после.

    Отдельная база, а не рабочая: миграции здесь гоняются с нуля, и делать это
    поверх данных, которые кому-то нужны, нельзя.
    """
    base = _base_url()
    if base is None:
        pytest.skip("нужен Postgres: задайте TEST_DATABASE_URL или DATABASE_URL")

    import sqlalchemy as sa

    admin_url = base.rsplit("/", 1)[0] + "/postgres"
    name = f"migrtest_{uuid.uuid4().hex[:12]}"
    engine = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")

    try:
        with engine.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # noqa: BLE001 — нет прав/нет сервера: не наш случай
        engine.dispose()
        pytest.skip(f"не удалось создать временную базу: {type(exc).__name__}: {exc}")

    yield base.rsplit("/", 1)[0] + f"/{name}"

    with engine.connect() as connection:
        connection.execute(
            sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :n AND pid <> pg_backend_pid()"
            ),
            {"n": name},
        )
        connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
    engine.dispose()


def test_migrations_apply_to_empty_database(scratch_database: str) -> None:
    """Цепочка ревизий проходит на чистой базе от начала до конца."""
    from alembic import command
    from alembic.runtime.migration import MigrationContext
    import sqlalchemy as sa

    command.upgrade(_alembic_config(scratch_database), "head")

    engine = sa.create_engine(scratch_database)
    try:
        with engine.connect() as connection:
            revision = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()

    assert revision is not None, "после upgrade head ревизия не проставилась"


def test_books_revision_is_reversible(scratch_database: str) -> None:
    """Ревизия «Книг» откатывается и накатывается заново.

    Проверяется именно она, а не вся цепочка до нуля: у ревизий 0008 и 0009
    откат намеренно неполный — они не удаляют таблицы, в которых лежат данные
    пользователей. Полный `downgrade base` на этом и споткнулся бы, причём по
    правильной причине.

    Обратимость важна не ради самого отката, а потому что схема «Книг» будет
    меняться часто: пока раздел строится, ревизии переписываются, и
    невозможность откатиться превращает каждую ошибку в пересоздание базы.
    """
    from alembic import command
    import sqlalchemy as sa

    config = _alembic_config(scratch_database)
    command.upgrade(config, "head")

    engine = sa.create_engine(scratch_database)
    try:
        with engine.connect() as connection:
            before = _schema_exists(connection, "books")

        command.downgrade(config, "0009")
        with engine.connect() as connection:
            after_down = _schema_exists(connection, "books")

        command.upgrade(config, "head")
        with engine.connect() as connection:
            after_up = _schema_exists(connection, "books")
    finally:
        engine.dispose()

    assert before, "после upgrade head схемы books нет"
    assert not after_down, "после отката схема books осталась"
    assert after_up, "повторный upgrade не восстановил схему books"


def _schema_exists(connection, name: str) -> bool:
    import sqlalchemy as sa

    found = connection.execute(
        sa.text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :n"),
        {"n": name},
    ).scalar()
    return found is not None


def test_models_and_migrations_agree(scratch_database: str) -> None:
    """После миграций alembic не находит разницы с моделями.

    Разница здесь означает ровно одно: модель поправили, а ревизию не написали.
    Сообщение печатает, что именно разошлось, — иначе по «assert not diff»
    непонятно, куда смотреть.
    """
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext
    import sqlalchemy as sa

    from app.migrations.metadata import target_metadata

    command.upgrade(_alembic_config(scratch_database), "head")

    engine = sa.create_engine(scratch_database)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "target_metadata": target_metadata,
                    # Чужие схемы в сравнение не берём: alembic иначе предложит
                    # удалить всё, чего нет в моделях, включая служебное.
                    "include_schemas": True,
                },
            )
            diff = compare_metadata(context, target_metadata)
    finally:
        engine.dispose()

    assert not diff, "модели разошлись с миграциями:\n" + "\n".join(
        f"  · {item}" for item in diff
    )
