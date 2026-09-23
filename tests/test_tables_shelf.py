"""Полка «Таблиц»: открыта без входа, снимок проходит насквозь, BBC не знает.

Раздел был зеркалом книг BBC за входом дашборда. Теперь это место для своих
таблиц: ни сервисного аккаунта, ни чужих книг, ни учёток. Проверки ниже держат
три обещания этой переделки:

* раздел открыт — голый запрос без cookie получает полку, а не форму входа;
* сервер снимок не разбирает — что пришло, то и ушло, байт в байт, а список
  полки не поднимает снимки из базы вовсе;
* модуль не знает про BBC — ни пакет, ни место его подключения.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, event

from app.api.router import api_router
from app.core.database import get_engine
from app.webexcel import routes as shelf_routes
from app.webexcel.db import webexcel_session
from app.webexcel.models import ShelfTable

BACKEND = Path(__file__).resolve().parents[1]
BASE = "/api/v1/web-excel"

#: Снимок с тем, что сервер обязан вернуть нетронутым: порядок ключей, пробелы,
#: кириллица, формула. Разбери сервер его и собери заново — хоть что-то из этого
#: поменялось бы.
SNAPSHOT = (
    '{"sheetOrder": ["s1"],  "id":"book", "name":"Касса",'
    ' "sheets":{"s1":{"id":"s1","name":"Июль","cellData":{"0":{"0":{"v":"Аренда"},'
    '"1":{"v":95323.5,"t":2},"2":{"f":"=SUM(B1:B3)"}}}}}}'
)


@pytest.fixture
def client():
    def wipe() -> None:
        with webexcel_session() as session:
            session.execute(delete(ShelfTable))

    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    wipe()
    yield TestClient(app)
    wipe()


def _save(client: TestClient, **fields) -> dict:
    body = {
        "name": "Касса",
        "source": "blank",
        "sheets": [{"name": "Июль", "rows": 3, "cols": 3}],
        "snapshot": SNAPSHOT,
        **fields,
    }
    response = client.post(f"{BASE}/shelf", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# ── Открыта ──────────────────────────────────────────────────────────────────


def test_polka_otkryta_bez_vhoda(client: TestClient) -> None:
    """Ни cookie, ни заголовка — и полка отвечает, а не просит войти."""
    assert client.get(f"{BASE}/shelf").json() == {"tables": []}
    saved = _save(client)
    assert client.get(f"{BASE}/shelf/{saved['id']}").status_code == 200


# ── Снимок насквозь ──────────────────────────────────────────────────────────


def test_snimok_vozvrashchaetsya_bayt_v_bayt(client: TestClient) -> None:
    saved = _save(client)
    table = client.get(f"{BASE}/shelf/{saved['id']}").json()

    assert table["snapshot"] == SNAPSHOT
    assert table["size_bytes"] == len(SNAPSHOT.encode("utf-8"))
    assert table["sheets"] == [{"name": "Июль", "rows": 3, "cols": 3}]


def test_spisok_polki_ne_podnimaet_snimki(client: TestClient) -> None:
    """У десятка больших таблиц снимки весят сотни мегабайт, а полке нужно оглавление."""
    _save(client)
    _save(client, name="Склад")

    statements: list[str] = []

    def remember(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    # Слушатель — на базовом движке: `webexcel_engine()` на SQLite отдаёт его
    # копию с переводом схем, и события копии туда не доходят.
    engine = get_engine()
    event.listen(engine, "before_cursor_execute", remember)
    try:
        tables = client.get(f"{BASE}/shelf").json()["tables"]
    finally:
        event.remove(engine, "before_cursor_execute", remember)

    assert [table["name"] for table in tables] == ["Склад", "Касса"]
    assert all("snapshot" not in table for table in tables)
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert selects, "список полки не дошёл до базы"
    assert not any("snapshot" in s for s in selects), "список поднял снимки из базы"


def test_pereimenovanie_ne_trogaet_snimok(client: TestClient) -> None:
    saved = _save(client)
    renamed = client.put(f"{BASE}/shelf/{saved['id']}", json={"name": "  Касса 2026  "}).json()

    assert renamed["name"] == "Касса 2026"
    assert client.get(f"{BASE}/shelf/{saved['id']}").json()["snapshot"] == SNAPSHOT


def test_sohranenie_zamenyaet_snimok_i_oglavlenie(client: TestClient) -> None:
    saved = _save(client)
    changed = SNAPSHOT.replace("Аренда", "Коммуналка")
    client.put(
        f"{BASE}/shelf/{saved['id']}",
        json={"snapshot": changed, "sheets": [{"name": "Июль", "rows": 3, "cols": 3}, {"name": "Август"}]},
    )
    table = client.get(f"{BASE}/shelf/{saved['id']}").json()

    assert table["snapshot"] == changed
    assert [sheet["name"] for sheet in table["sheets"]] == ["Июль", "Август"]


def test_kopiya_nezavisima_ot_originala(client: TestClient) -> None:
    saved = _save(client)
    copy = client.post(f"{BASE}/shelf/{saved['id']}/copy").json()
    client.put(f"{BASE}/shelf/{copy['id']}", json={"snapshot": '{"id":"other"}'})

    assert copy["name"] == "Касса (копия)"
    assert client.get(f"{BASE}/shelf/{saved['id']}").json()["snapshot"] == SNAPSHOT


def test_udalennaya_tablica_ischezaet(client: TestClient) -> None:
    saved = _save(client)
    assert client.delete(f"{BASE}/shelf/{saved['id']}").json() == {"ok": True}

    missing = client.get(f"{BASE}/shelf/{saved['id']}")
    assert missing.status_code == 404
    assert "полке" in missing.json()["detail"]


# ── Границы ──────────────────────────────────────────────────────────────────


def test_slishkom_bolshaya_tablica_ne_lozhitsya_na_polku(client: TestClient, monkeypatch) -> None:
    """Раздел открыт без входа — одна загрузка не должна класть в базу что угодно."""
    monkeypatch.setattr(shelf_routes.webexcel_settings, "max_table_mb", 1, raising=False)
    huge = json.dumps({"id": "x", "pad": "я" * 700_000})  # ~1,3 МБ в UTF-8

    response = client.post(f"{BASE}/shelf", json={"name": "Большая", "snapshot": huge})

    assert response.status_code == 413
    assert "до 1 МБ" in response.json()["detail"]
    assert client.get(f"{BASE}/shelf").json() == {"tables": []}


@pytest.mark.parametrize("broken", ["", "Internal Server Error", "[]"])
def test_povrezhdennyy_snimok_ne_lozhitsya(client: TestClient, broken: str) -> None:
    response = client.post(f"{BASE}/shelf", json={"name": "Пусто", "snapshot": broken})
    assert response.status_code == 422


# ── BBC здесь нет ────────────────────────────────────────────────────────────


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return {name for name in found if name.startswith("app.")}


def test_paket_tablic_ne_znaet_chuzhih_moduley() -> None:
    allowed = ("app.core", "app.webexcel")
    leaks = {
        path.name: sorted(
            name for name in _imports(path) if not any(name == p or name.startswith(p + ".") for p in allowed)
        )
        for path in sorted((BACKEND / "app" / "webexcel").glob("*.py"))
    }
    assert not {name: items for name, items in leaks.items() if items}


def test_podklyuchenie_tablic_bez_ohrany_bbc() -> None:
    """Раньше роутер раздела подключался с `require_tables_admin` — входом дашборда."""
    source = (BACKEND / "app" / "api" / "router.py").read_text(encoding="utf-8")
    assert "require_tables_admin" not in source
    assert "from app.bbc.deps" not in source
    assert "include_router(webexcel_router)" in source
