"""Учётки сотрудников: создание, временный пароль, принудительная смена, увольнение.

Проверяется весь путь по HTTP, а не отдельные функции: смысл этой фичи в том,
кому что доступно, и роняться она будет на забытом guard'е в маршруте, а не в
чистой логике.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.bbc import auth as auth_module
from app.bbc.auth import hash_password
from app.bbc.db import bbc_session
from app.bbc.models import BbcAccessLink, BbcAuditEntry, BbcUser, BbcUserSession
from app.main import app

PASSWORD = "secret123"
BASE = "/api/v1/bbc"


@pytest.fixture(autouse=True)
def clean_access(monkeypatch):
    monkeypatch.setattr(auth_module.bbc_settings, "bootstrap_admin", "", raising=False)
    monkeypatch.setattr(auth_module.bbc_settings, "bootstrap_password", "", raising=False)

    def _wipe():
        with bbc_session() as session:
            session.query(BbcUserSession).delete()
            session.query(BbcAccessLink).delete()
            session.query(BbcAuditEntry).delete()
            session.query(BbcUser).delete()

    _wipe()
    yield
    _wipe()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin_client(client: TestClient) -> TestClient:
    with bbc_session() as session:
        session.add(BbcUser(username="admin", password_hash=hash_password(PASSWORD), role="admin"))
    response = client.post(f"{BASE}/auth/login", json={"username": "admin", "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


FORM = {
    "username": "dana",
    "full_name": "Дана Жумабекова",
    "departments": ["ОБО"],
    "blocks": ["receivables", "touches"],
    "data_scope": "own",
    "employee_aliases": ["Дана", "Дана Ж."],
}


def _create(admin_client: TestClient, **overrides) -> dict:
    response = admin_client.post(f"{BASE}/employees", json={**FORM, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


# ── Создание ─────────────────────────────────────────────────────────────────────


def test_created_employee_gets_a_temporary_password_once(admin_client: TestClient) -> None:
    payload = _create(admin_client)

    # Пароль возвращается ровно здесь. Он и есть весь смысл ответа: админ
    # передаёт его человеку и больше нигде увидеть не может.
    assert payload["temp_password"]
    assert payload["employee"]["must_change_password"] is True
    assert payload["employee"]["role"] == "employee"
    assert payload["employee"]["data_scope"] == "own"

    # В списке пароля нет и быть не может — в базе только argon2-хеш.
    listing = admin_client.get(f"{BASE}/employees").json()
    assert "temp_password" not in listing["employees"][0]


def test_own_scope_without_aliases_is_refused(admin_client: TestClient) -> None:
    """Учётка, которой не видно ни одной строки, выглядит рабочей и ей не является."""
    response = admin_client.post(f"{BASE}/employees", json={**FORM, "employee_aliases": []})

    assert response.status_code == 400
    assert "Сотрудник" in response.json()["detail"]


def test_employee_without_blocks_is_refused(admin_client: TestClient) -> None:
    response = admin_client.post(f"{BASE}/employees", json={**FORM, "blocks": []})
    assert response.status_code == 400


def test_duplicate_login_is_refused_case_insensitively(admin_client: TestClient) -> None:
    _create(admin_client)
    response = admin_client.post(f"{BASE}/employees", json={**FORM, "username": "DANA"})
    assert response.status_code == 400


# ── Принудительная смена пароля ──────────────────────────────────────────────────


def test_employee_sees_nothing_until_the_password_is_changed(
    admin_client: TestClient, client: TestClient
) -> None:
    created = _create(admin_client)
    admin_client.post(f"{BASE}/auth/logout")

    login = client.post(
        f"{BASE}/auth/login",
        json={"username": "dana", "password": created["temp_password"]},
    )
    assert login.status_code == 200

    # Вошла — но за данные не пускают: пароль лежит в чужой переписке.
    me = client.get(f"{BASE}/me").json()
    assert me["authenticated"] is True
    assert me["must_change_password"] is True
    assert me["blocks"] == []
    assert client.get(f"{BASE}/dataset").status_code == 401

    # Сменить пароль при этом можно — иначе выйти из этого состояния нечем.
    changed = client.post(
        f"{BASE}/auth/set-password",
        json={"current_password": created["temp_password"], "new_password": "новый-пароль-1"},
    )
    assert changed.status_code == 200

    # Смена гасит сессию: временный пароль мог остаться в переписке.
    assert client.get(f"{BASE}/me").json()["authenticated"] is False

    again = client.post(
        f"{BASE}/auth/login", json={"username": "dana", "password": "новый-пароль-1"}
    )
    assert again.status_code == 200
    after = client.get(f"{BASE}/me").json()
    assert after["must_change_password"] is False
    assert set(after["blocks"]) == {"receivables", "touches"}
    assert after["is_admin"] is False


def test_set_password_requires_the_current_one(
    admin_client: TestClient, client: TestClient
) -> None:
    created = _create(admin_client)
    admin_client.post(f"{BASE}/auth/logout")
    client.post(f"{BASE}/auth/login", json={"username": "dana", "password": created["temp_password"]})

    response = client.post(
        f"{BASE}/auth/set-password",
        json={"current_password": "не тот", "new_password": "новый-пароль-1"},
    )
    assert response.status_code == 400


def test_short_password_is_refused(admin_client: TestClient, client: TestClient) -> None:
    created = _create(admin_client)
    admin_client.post(f"{BASE}/auth/logout")
    client.post(f"{BASE}/auth/login", json={"username": "dana", "password": created["temp_password"]})

    response = client.post(
        f"{BASE}/auth/set-password",
        json={"current_password": created["temp_password"], "new_password": "коротк"},
    )
    assert response.status_code == 400


# ── Жизненный цикл ───────────────────────────────────────────────────────────────


def test_reset_password_issues_a_new_one_and_drops_sessions(
    admin_client: TestClient, client: TestClient
) -> None:
    created = _create(admin_client)
    employee_id = created["employee"]["id"]

    reset = admin_client.post(f"{BASE}/employees/{employee_id}/reset-password")
    assert reset.status_code == 200
    assert reset.json()["temp_password"] != created["temp_password"]
    assert reset.json()["employee"]["must_change_password"] is True

    # Старый пароль больше не работает.
    admin_client.post(f"{BASE}/auth/logout")
    stale = client.post(
        f"{BASE}/auth/login", json={"username": "dana", "password": created["temp_password"]}
    )
    assert stale.status_code == 401


def test_dismissed_employee_cannot_sign_in_but_stays_in_the_list(
    admin_client: TestClient, client: TestClient
) -> None:
    created = _create(admin_client)
    employee_id = created["employee"]["id"]

    dismissed = admin_client.post(f"{BASE}/employees/{employee_id}/dismiss")
    assert dismissed.status_code == 200
    assert dismissed.json()["status"] == "dismissed"

    # Остаётся в списке: его касания никуда не делись, и по ним видно автора.
    listing = admin_client.get(f"{BASE}/employees").json()["employees"]
    assert [item["status"] for item in listing] == ["dismissed"]

    admin_client.post(f"{BASE}/auth/logout")
    response = client.post(
        f"{BASE}/auth/login", json={"username": "dana", "password": created["temp_password"]}
    )
    assert response.status_code == 401


def test_restore_returns_the_employee_with_a_fresh_password(admin_client: TestClient) -> None:
    created = _create(admin_client)
    employee_id = created["employee"]["id"]
    admin_client.post(f"{BASE}/employees/{employee_id}/dismiss")

    restored = admin_client.post(f"{BASE}/employees/{employee_id}/restore")

    assert restored.status_code == 200
    assert restored.json()["employee"]["status"] == "active"
    # Старый пароль ушёл вместе с человеком — обратно его не возвращают.
    assert restored.json()["temp_password"] != created["temp_password"]
    assert restored.json()["employee"]["must_change_password"] is True


def test_update_changes_rights_and_drops_open_sessions(
    admin_client: TestClient, client: TestClient
) -> None:
    created = _create(admin_client)
    employee_id = created["employee"]["id"]

    updated = admin_client.patch(
        f"{BASE}/employees/{employee_id}",
        json={**FORM, "data_scope": "department", "blocks": ["receivables"]},
    )

    assert updated.status_code == 200
    assert updated.json()["data_scope"] == "department"
    assert updated.json()["blocks"] == ["receivables"]

    with bbc_session() as session:
        # Открытые вкладки несли старую область видимости — их гасят.
        assert session.query(BbcUserSession).filter_by(user_id=employee_id).count() == 0


def test_employee_actions_are_written_to_the_audit_log(admin_client: TestClient) -> None:
    """Таблица существовала с начала модуля и не имела ни одного писателя."""
    created = _create(admin_client)
    admin_client.post(f"{BASE}/employees/{created['employee']['id']}/dismiss")

    with bbc_session() as session:
        actions = [entry.action for entry in session.query(BbcAuditEntry).all()]

    assert "employee.create" in actions
    assert "employee.dismiss" in actions


# ── Права ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/employees"),
        ("post", "/employees"),
        ("get", "/employees/aliases"),
        ("patch", "/employees/1"),
        ("delete", "/employees/1"),
        ("post", "/employees/1/dismiss"),
        ("post", "/employees/1/reset-password"),
    ],
)
def test_employee_management_is_admin_only(client: TestClient, method: str, path: str) -> None:
    # request(), а не get()/delete(): у тех нет параметра json, а тело нужно
    # только чтобы запрос не спотыкался о валидацию раньше проверки прав.
    assert client.request(method, f"{BASE}{path}", json={}).status_code in (401, 403)


def test_employee_cannot_manage_other_employees(
    admin_client: TestClient, client: TestClient
) -> None:
    created = _create(admin_client)
    admin_client.post(f"{BASE}/auth/logout")
    client.post(f"{BASE}/auth/login", json={"username": "dana", "password": created["temp_password"]})
    client.post(
        f"{BASE}/auth/set-password",
        json={"current_password": created["temp_password"], "new_password": "новый-пароль-1"},
    )
    client.post(f"{BASE}/auth/login", json={"username": "dana", "password": "новый-пароль-1"})

    assert client.get(f"{BASE}/employees").status_code == 403
    assert client.post(f"{BASE}/employees", json=FORM).status_code == 403
