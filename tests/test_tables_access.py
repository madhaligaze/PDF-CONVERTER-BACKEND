"""«Таблицы» закрыты входом дашборда BBC — и только для админа.

До этого маршруты `/api/v1/web-excel/*` не спрашивали никого: на проде голый
запрос отдавал список всех книг сервисного аккаунта (реестр клиентов, журнал,
продажи с ФОТ) и любую вкладку целиком, а заодно заставлял сервер тянуть из
Google по 240 МБ на вкладку.

Проверяется по HTTP — cookie → зависимость, — как и остальные границы доступа:
ровно так эта дыра и выглядела, экран при ней работал.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.bbc import auth as auth_module
from app.bbc.auth import hash_password
from app.bbc.db import bbc_session
from app.bbc.models import BbcAccessLink, BbcUser, BbcUserSession
from app.main import app
from app.webexcel import routes as webexcel_routes

PASSWORD = "secret123"
LOGIN = "/api/v1/bbc/auth/login"
TABLES = "/api/v1/web-excel"


@pytest.fixture(autouse=True)
def clean_access(monkeypatch):
    monkeypatch.setattr(auth_module.bbc_settings, "bootstrap_admin", "", raising=False)
    monkeypatch.setattr(auth_module.bbc_settings, "bootstrap_password", "", raising=False)

    def _wipe():
        with bbc_session() as session:
            session.query(BbcUserSession).delete()
            session.query(BbcAccessLink).delete()
            session.query(BbcUser).delete()

    _wipe()
    yield
    _wipe()


@pytest.fixture
def google(monkeypatch) -> list[str]:
    """Походы раздела в Google. Закрытый раздел не должен сделать ни одного."""
    calls: list[str] = []
    monkeypatch.setattr(webexcel_routes.webexcel_settings, "enabled", True, raising=False)
    monkeypatch.setattr(
        type(webexcel_routes.webexcel_settings), "credentials_available", property(lambda self: True)
    )
    monkeypatch.setattr(webexcel_routes, "list_spreadsheets", lambda: calls.append("list") or [])
    return calls


def _user(username: str, *, role: str, must_change_password: bool = False, blocks=None) -> None:
    with bbc_session() as session:
        session.add(
            BbcUser(
                username=username,
                password_hash=hash_password(PASSWORD),
                role=role,
                full_name="Тестовый",
                departments=["ОБО"],
                blocks=blocks or [],
                data_scope="all",
                must_change_password=must_change_password,
            )
        )


def _signed_in(username: str) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(LOGIN, json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


def _tables_routes() -> list[tuple[str, str]]:
    """Все маршруты раздела — чтобы новый не остался открытым по забывчивости."""
    found = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith(TABLES):
            for method in sorted(getattr(route, "methods", ()) - {"HEAD", "OPTIONS"}):
                found.append((method, path.replace("{spreadsheet_id}", "book").replace("{book_id}", "1")))
    return found


def test_razdel_nashelsya_tselikom() -> None:
    routes = _tables_routes()
    assert ("GET", f"{TABLES}/sources") in routes
    assert ("GET", f"{TABLES}/sources/book/tab") in routes
    assert len(routes) >= 9


def test_bez_vhoda_zakryt_kazhdyy_marshrut(google) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    for method, path in _tables_routes():
        response = client.request(method, path, params={"title": "Журнал"}, json={})
        assert response.status_code == 401, f"{method} {path} → {response.status_code}"
    assert google == [], "закрытый раздел всё-таки сходил в Google"


def test_sotrudniku_razdel_ne_otkryvaetsya_dazhe_so_vsemi_pravami(google) -> None:
    """Журнал и продажи с ФОТ дашборд сотруднику не выдаёт — «Таблицы» тоже."""
    _user("emp", role="employee", blocks=["receivables", "registries", "journal", "sales"])
    client = _signed_in("emp")

    for method, path in _tables_routes():
        response = client.request(method, path, params={"title": "Журнал"}, json={})
        assert response.status_code == 403, f"{method} {path} → {response.status_code}"
    assert google == []


def test_vremennyy_parol_admina_ne_otkryvaet(google) -> None:
    """Пароль, который лежит в переписке, не открывает ничего — и здесь тоже."""
    _user("tmpadmin", role="admin", must_change_password=True)
    client = _signed_in("tmpadmin")

    assert client.get(f"{TABLES}/sources").status_code == 401
    assert google == []


def test_admin_prohodit(google) -> None:
    _user("admin", role="admin")
    client = _signed_in("admin")

    response = client.get(f"{TABLES}/sources")

    assert response.status_code == 200, response.text
    assert response.json() == {"books": []}
    assert google == ["list"]


def test_vyhod_snova_zakryvaet(google) -> None:
    _user("admin", role="admin")
    client = _signed_in("admin")
    assert client.get(f"{TABLES}/sources").status_code == 200

    client.post("/api/v1/bbc/auth/logout")

    assert client.get(f"{TABLES}/sources").status_code == 401
