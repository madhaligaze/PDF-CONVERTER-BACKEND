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


def test_finance_roles_migrate_to_grants(scratch_database: str) -> None:
    """Ревизия 0019: прежние роли становятся правами, равными прежним способностям.

    Бухгалтер вёл учёт, но счета не заводил и людей не видел; наблюдатель
    только смотрел. После ревизии оба — сотрудники с личными правами, у
    каждого члена компании есть запись сотрудника, а совпавшее имя не
    склеивается с чужой записью. Откат возвращает роли.
    """
    from alembic import command
    import sqlalchemy as sa

    config = _alembic_config(scratch_database)
    command.upgrade(config, "head")
    command.downgrade(config, "0018")

    ws, owner, buh, viewer = (uuid.uuid4() for _ in range(4))
    engine = sa.create_engine(scratch_database)
    try:
        with engine.begin() as connection:
            run = lambda sql, **params: connection.execute(sa.text(sql), params)  # noqa: E731
            run("INSERT INTO finance.workspaces (id, slug, title) VALUES (:id, 'bbc-test', 'BBC (тест)')", id=ws)
            for user_id, email, name in (
                (owner, "owner@bbc.kz", "Ермеков Нурболат"),
                (buh, "buh@bbc.kz", "Сейтова Айдана"),
                (viewer, "view@bbc.kz", ""),
            ):
                run(
                    "INSERT INTO finance.users (id, email, email_normalized, password_hash, full_name) "
                    "VALUES (:id, :e, :e, 'x', :n)",
                    id=user_id, e=email, n=name,
                )
            for user_id, role in ((owner, "owner"), (buh, "accountant"), (viewer, "viewer")):
                run(
                    "INSERT INTO finance.memberships (id, workspace_id, user_id, role) VALUES (:id, :w, :u, :r)",
                    id=uuid.uuid4(), w=ws, u=user_id, r=role,
                )
            # Ответственный из реестра договоров с тем же именем, что у бухгалтера.
            run(
                "INSERT INTO finance.employees (id, workspace_id, full_name, normalized_name) "
                "VALUES (:id, :w, 'Сейтова Айдана', 'сейтова айдана')",
                id=uuid.uuid4(), w=ws,
            )
            run(
                "INSERT INTO finance.action_log (id, workspace_id, actor, kind, entity) "
                "VALUES (:id, :w, 'buh@bbc.kz', 'contract.export', 'contract')",
                id=uuid.uuid4(), w=ws,
            )

        command.upgrade(config, "head")
        with engine.connect() as connection:
            roles = dict(connection.execute(sa.text(
                "SELECT user_id, role FROM finance.memberships WHERE workspace_id = :w"), {"w": ws}).all())
            employees = dict(connection.execute(sa.text(
                "SELECT user_id, full_name FROM finance.employees WHERE workspace_id = :w AND user_id IS NOT NULL"),
                {"w": ws}).all())
            grants = {
                (user_id, resource): level
                for user_id, resource, level in connection.execute(sa.text(
                    "SELECT e.user_id, g.resource, g.level FROM finance.access_grants AS g "
                    "JOIN finance.employees AS e ON e.id = g.subject_id AND g.subject_kind = 'employee'"
                )).all()
            }
            log = connection.execute(sa.text(
                "SELECT category, user_id FROM finance.action_log WHERE workspace_id = :w"), {"w": ws}).one()

        assert roles == {owner: "owner", buh: "employee", viewer: "employee"}
        assert set(employees) == {owner, buh, viewer}, "у каждого члена компании — запись сотрудника"
        assert employees[buh] == "Сейтова Айдана · buh@bbc.kz", "чужая запись с тем же именем не склеивается"
        assert employees[viewer] == "view@bbc.kz"
        assert grants[(buh, "journal")] == "edit" and grants[(buh, "contracts")] == "edit"
        assert grants[(buh, "dictionaries")] == "view", "счета бухгалтер и раньше не заводил"
        assert grants[(buh, "integrations")] == "view"
        assert (buh, "people") not in grants and (buh, "audit") not in grants
        assert grants[(viewer, "journal")] == "view" and grants[(viewer, "reports.debts")] == "view"
        assert not any(level == "edit" for (user, _res), level in grants.items() if user == viewer)
        assert not any(user == owner for user, _res in grants), "владельцу права не записываются"
        assert log == ("export", buh), "старая запись журнала получила вид и автора"

        command.downgrade(config, "0018")
        with engine.connect() as connection:
            back = dict(connection.execute(sa.text(
                "SELECT user_id, role FROM finance.memberships WHERE workspace_id = :w"), {"w": ws}).all())
        assert back == {owner: "owner", buh: "accountant", viewer: "viewer"}
        command.upgrade(config, "head")
    finally:
        engine.dispose()


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
