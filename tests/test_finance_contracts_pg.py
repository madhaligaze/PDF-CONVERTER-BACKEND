"""Реестр договоров на настоящем Postgres: одновременная правка одного поля.

Зачем отдельный набор
─────────────────────
Конфликт правок считается по полю: второй, кто правит то же поле по
устаревшему номеру, получает 409. На SQLite эта проверка проходит всегда —
там нет ни настоящей параллельности, ни `SELECT … FOR UPDATE`. Прогон двух
окон на стенде показал, что без блокировки строки оба запроса читали старый
номер поля, оба проходили проверку, и правка первого пропадала молча. Ловить
это можно только на Postgres, поэтому набор пропускается без него — как
`test_migrations.py`.

    TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:5434/pdf_converter_dev \\
        uv run pytest tests/test_finance_contracts_pg.py
"""
from __future__ import annotations

import os
import threading
import time
import uuid

import pytest

from app.finance import service as finance_service
from app.finance.contracts import service, setup
from app.finance.db import finance_session

FULL = service.Access(view=True, edit=True, setup=True)


def _base_url() -> str | None:
    for name in ("TEST_DATABASE_URL", "DATABASE_URL"):
        value = os.environ.get(name, "").strip()
        if value.startswith("postgresql"):
            return value
    return None


@pytest.fixture
def pg(monkeypatch):
    base = _base_url()
    if base is None:
        pytest.skip("нужен Postgres: задайте TEST_DATABASE_URL")
    import sqlalchemy as sa

    from app.finance import db as finance_db_module

    admin = sa.create_engine(base.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT", future=True)
    name = f"contracts_pg_{uuid.uuid4().hex[:10]}"
    with admin.connect() as connection:
        connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    engine = sa.create_engine(base.rsplit("/", 1)[0] + f"/{name}", future=True, pool_size=5)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_odnovremennaya_pravka_odnogo_polya_ne_teryaetsya(pg):
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        setup.add_entity(session, workspace, name="BBC")
        contract = service.create(
            session, workspace, FULL, service.Actor(None, "a@test"),
            {"executor": "BBC", "customer": "ТОО Альфа", "note": "было"},
        )
        contract_id, seen, space_id = contract.id, contract.seq, workspace.id

    locked = threading.Event()
    outcome: dict[str, object] = {}

    def first() -> None:
        with finance_session() as session:
            workspace = finance_service.get_workspace(session, space_id)
            service.patch(session, workspace, FULL, service.Actor(None, "a@test"), contract_id,
                          {"note": "первый"}, known_seq=seen)
            locked.set()
            # Держим транзакцию открытой: второй запрос должен ждать, а не
            # читать старый номер поля мимо нас.
            time.sleep(0.6)
        outcome["first"] = "ok"

    def second() -> None:
        locked.wait(5)
        try:
            with finance_session() as session:
                workspace = finance_service.get_workspace(session, space_id)
                service.patch(session, workspace, FULL, service.Actor(None, "b@test"), contract_id,
                              {"note": "второй"}, known_seq=seen)
            outcome["second"] = "ok"
        except service.FieldConflict as exc:
            outcome["second"] = exc.conflicts

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert outcome["first"] == "ok"
    assert outcome["second"] == ["note"], "вторая правка того же поля должна получить конфликт"
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space_id)
        assert service.get_contract(session, workspace, contract_id).note == "первый"


def test_raznye_polya_odnovremenno_obe_prohodyat(pg):
    with finance_session() as session:
        workspace = finance_service.ensure_workspace(session)
        setup.add_entity(session, workspace, name="BBC")
        contract = service.create(
            session, workspace, FULL, service.Actor(None, "a@test"),
            {"executor": "BBC", "customer": "ТОО Бета"},
        )
        contract_id, seen, space_id = contract.id, contract.seq, workspace.id

    errors: list[Exception] = []

    def edit(field: str, value: str) -> None:
        try:
            with finance_session() as session:
                workspace = finance_service.get_workspace(session, space_id)
                service.patch(session, workspace, FULL, service.Actor(None, f"{field}@test"), contract_id,
                              {field: value}, known_seq=seen)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=edit, args=("note", "примечание")),
        threading.Thread(target=edit, args=("folder_url", "https://folder")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not errors
    with finance_session() as session:
        workspace = finance_service.get_workspace(session, space_id)
        contract = service.get_contract(session, workspace, contract_id)
        assert (contract.note, contract.folder_url) == ("примечание", "https://folder")
