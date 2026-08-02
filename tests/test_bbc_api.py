"""End-to-end access control over HTTP.

These exercise the real chain — cookie / link token → dependency → scope — rather
than the scope helpers in isolation, so a route that forgets its guard fails here.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.bbc import auth as auth_module
from app.bbc.auth import hash_password
from app.bbc.db import bbc_session
from app.bbc.deps import SESSION_COOKIE
from app.bbc.models import BbcAccessLink, BbcUser, BbcUserSession
from app.main import app

PASSWORD = "secret123"
BASE = "/api/v1/bbc"


@pytest.fixture(autouse=True)
def clean_access(monkeypatch):
    # Keep bootstrap out of the way: these tests create their own admin.
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
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin_client(client: TestClient) -> TestClient:
    with bbc_session() as session:
        session.add(BbcUser(username="admin", password_hash=hash_password(PASSWORD), role="admin"))

    response = client.post(f"{BASE}/auth/login", json={"username": "admin", "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


def _issue_link(admin_client: TestClient, department: str, **payload) -> dict:
    response = admin_client.post(
        f"{BASE}/links", json={"department": department, **payload}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _token_of(link: dict) -> str:
    return link["url"].split("k=", 1)[1]


# ── Login ────────────────────────────────────────────────────────────────────────


def test_login_sets_an_httponly_cookie(client: TestClient) -> None:
    with bbc_session() as session:
        session.add(BbcUser(username="admin", password_hash=hash_password(PASSWORD), role="admin"))

    response = client.post(f"{BASE}/auth/login", json={"username": "admin", "password": PASSWORD})

    assert response.status_code == 200
    assert response.json()["is_admin"] is True
    assert "httponly" in response.headers["set-cookie"].lower()


def test_login_with_wrong_password_is_401(client: TestClient) -> None:
    with bbc_session() as session:
        session.add(BbcUser(username="admin", password_hash=hash_password(PASSWORD), role="admin"))

    response = client.post(f"{BASE}/auth/login", json={"username": "admin", "password": "nope"})
    assert response.status_code == 401


def test_me_without_credentials_reports_anonymous(client: TestClient) -> None:
    response = client.get(f"{BASE}/me")

    assert response.status_code == 200
    payload = response.json()
    assert payload["authenticated"] is False
    assert payload["departments"] == []
    assert payload["needs_setup"] is True


def test_logout_clears_the_session(admin_client: TestClient) -> None:
    assert admin_client.post(f"{BASE}/auth/logout").status_code == 200
    assert admin_client.get(f"{BASE}/me").json()["authenticated"] is False


# ── Admin-only endpoints ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/links"),
        ("get", "/sheets"),
        ("get", "/snapshot"),
        ("post", "/account/credentials"),
        ("post", "/update"),
    ],
)
def test_admin_endpoints_reject_anonymous_callers(client: TestClient, method, path) -> None:
    kwargs = {"json": {}} if method == "post" else {}
    response = getattr(client, method)(f"{BASE}{path}", **kwargs)
    assert response.status_code in (401, 403), f"{path} -> {response.status_code}"


def test_link_holder_cannot_manage_links(admin_client: TestClient) -> None:
    """A referral link must not reach the account/link management surface."""
    token = _token_of(_issue_link(admin_client, "НО"))
    admin_client.post(f"{BASE}/auth/logout")

    response = admin_client.get(f"{BASE}/links", headers={"X-BBC-Link": token})
    assert response.status_code in (401, 403)


def test_link_holder_cannot_read_the_raw_snapshot(admin_client: TestClient) -> None:
    """The raw grid carries every department's rows — links must not reach it."""
    token = _token_of(_issue_link(admin_client, "НО"))
    admin_client.post(f"{BASE}/auth/logout")

    response = admin_client.get(f"{BASE}/snapshot", headers={"X-BBC-Link": token})
    assert response.status_code in (401, 403)


def test_status_stays_public(client: TestClient) -> None:
    """Config probe must never fail — the UI relies on it to show a hint."""
    assert client.get(f"{BASE}/status").status_code == 200


def test_update_is_refused_even_for_admin(admin_client: TestClient) -> None:
    response = admin_client.post(f"{BASE}/update", json={"worksheet": None, "updates": []})

    assert response.status_code == 403
    assert "только на чтение" in response.json()["detail"]


# ── Referral links ───────────────────────────────────────────────────────────────


def test_issued_link_reports_its_department_scope(admin_client: TestClient) -> None:
    token = _token_of(_issue_link(admin_client, "НО"))
    admin_client.post(f"{BASE}/auth/logout")

    payload = admin_client.get(f"{BASE}/me", headers={"X-BBC-Link": token}).json()

    assert payload["authenticated"] is True
    assert payload["departments"] == ["НО"]
    assert payload["is_admin"] is False
    assert payload["link_label"] == "НО"
    assert set(payload["blocks"]) == {"receivables", "analytics", "calendar"}


def test_link_token_also_works_as_a_query_parameter(admin_client: TestClient) -> None:
    token = _token_of(_issue_link(admin_client, "ЮО"))
    admin_client.post(f"{BASE}/auth/logout")

    assert admin_client.get(f"{BASE}/me", params={"k": token}).json()["departments"] == ["ЮО"]


def test_permanent_link_has_no_expiry(admin_client: TestClient) -> None:
    assert _issue_link(admin_client, "ОБО")["expires_at"] is None


def test_temporary_link_carries_an_expiry(admin_client: TestClient) -> None:
    assert _issue_link(admin_client, "ОБО", expires_in_hours=3)["expires_at"] is not None


def test_revoked_link_stops_working_immediately(admin_client: TestClient) -> None:
    link = _issue_link(admin_client, "HR")
    token = _token_of(link)

    assert admin_client.delete(f"{BASE}/links/{link['id']}").status_code == 200
    admin_client.post(f"{BASE}/auth/logout")

    assert admin_client.get(f"{BASE}/me", headers={"X-BBC-Link": token}).json()["authenticated"] is False


def test_revoked_link_does_not_fall_back_to_an_active_admin_session(
    admin_client: TestClient,
) -> None:
    """A dead link must stay dead even when the browser also holds an admin cookie."""
    link = _issue_link(admin_client, "HR")
    token = _token_of(link)
    admin_client.delete(f"{BASE}/links/{link['id']}")

    payload = admin_client.get(f"{BASE}/me", headers={"X-BBC-Link": token}).json()
    assert payload["authenticated"] is False
    assert payload["departments"] == []


def test_forged_token_grants_nothing(admin_client: TestClient) -> None:
    _issue_link(admin_client, "НО")
    admin_client.post(f"{BASE}/auth/logout")

    payload = admin_client.get(f"{BASE}/me", headers={"X-BBC-Link": "forged"}).json()
    assert payload["authenticated"] is False


def test_unknown_department_is_rejected(admin_client: TestClient) -> None:
    response = admin_client.post(f"{BASE}/links", json={"department": "Маркетинг"})
    assert response.status_code == 400


def test_listing_links_returns_the_address_again(admin_client: TestClient) -> None:
    """Адрес переживает перезагрузку кабинета — иначе выданную ссылку не показать."""
    created = _issue_link(admin_client, "НО")

    listed = admin_client.get(f"{BASE}/links").json()
    assert listed and listed[0]["url"] == created["url"]


def test_listing_hides_the_address_of_a_revoked_link(admin_client: TestClient) -> None:
    link = _issue_link(admin_client, "НО")
    admin_client.delete(f"{BASE}/links/{link['id']}")

    listed = admin_client.get(f"{BASE}/links").json()
    assert listed and listed[0]["url"] is None


def test_expiry_can_be_changed_after_the_link_was_handed_out(admin_client: TestClient) -> None:
    link = _issue_link(admin_client, "ЮО")
    assert link["expires_at"] is None

    response = admin_client.patch(f"{BASE}/links/{link['id']}", json={"expires_in_minutes": 15})

    assert response.status_code == 200
    updated = response.json()
    assert updated["expires_at"] is not None
    # Тот же адрес: у получателя на руках ссылка не должна протухнуть от того,
    # что администратор передумал насчёт срока.
    assert updated["url"] == link["url"]
    assert admin_client.get(f"{BASE}/me", params={"k": _token_of(link)}).json()[
        "link_expires_at"
    ] == updated["expires_at"]


def test_expiry_can_be_removed_again(admin_client: TestClient) -> None:
    link = _issue_link(admin_client, "ЮО", expires_in_hours=2)

    response = admin_client.patch(f"{BASE}/links/{link['id']}", json={"expires_in_minutes": None})

    assert response.status_code == 200
    assert response.json()["expires_at"] is None


def test_changing_expiry_of_a_missing_link_is_404(admin_client: TestClient) -> None:
    assert (
        admin_client.patch(f"{BASE}/links/nope", json={"expires_in_minutes": 5}).status_code == 404
    )


def test_non_positive_expiry_is_rejected(admin_client: TestClient) -> None:
    link = _issue_link(admin_client, "ЮО")
    assert (
        admin_client.patch(
            f"{BASE}/links/{link['id']}", json={"expires_in_minutes": 0}
        ).status_code
        == 400
    )


def test_revoking_a_missing_link_is_404(admin_client: TestClient) -> None:
    assert admin_client.delete(f"{BASE}/links/nope").status_code == 404


# ── Credentials ──────────────────────────────────────────────────────────────────


def test_password_change_requires_the_current_one(admin_client: TestClient) -> None:
    response = admin_client.post(
        f"{BASE}/account/credentials",
        json={"current_password": "wrong", "new_password": "brandnew123"},
    )
    assert response.status_code == 400


def test_password_change_logs_the_admin_out(admin_client: TestClient) -> None:
    response = admin_client.post(
        f"{BASE}/account/credentials",
        json={"current_password": PASSWORD, "new_password": "brandnew123"},
    )

    assert response.status_code == 200
    assert admin_client.get(f"{BASE}/me").json()["authenticated"] is False
    assert (
        admin_client.post(
            f"{BASE}/auth/login", json={"username": "admin", "password": "brandnew123"}
        ).status_code
        == 200
    )


def test_username_change_keeps_the_session(admin_client: TestClient) -> None:
    response = admin_client.post(
        f"{BASE}/account/credentials",
        json={"current_password": PASSWORD, "new_username": "director"},
    )

    assert response.status_code == 200
    assert admin_client.get(f"{BASE}/me").json()["username"] == "director"


def test_session_cookie_is_the_only_admin_credential(admin_client: TestClient) -> None:
    """Dropping the cookie must drop admin rights — no other implicit trust."""
    admin_client.cookies.delete(SESSION_COOKIE)
    assert admin_client.get(f"{BASE}/links").status_code in (401, 403)


# ── Scoped data endpoints ────────────────────────────────────────────────────────


@pytest.fixture
def fake_rows(monkeypatch):
    """A tiny dataset standing in for the sheet, so these tests stay offline."""
    from app.bbc import live, service
    from app.bbc.dataset import SUBSCRIPTION, ContractRow

    def row(index: int, departments: tuple[str, ...], amount: float) -> ContractRow:
        return ContractRow(
            index=index,
            month=6,
            period_label="ИЮНЬ 2026",
            client=f"Клиент {index}",
            contract_no=f"№{index}",
            subject="Сопровождение",
            firm="BBC",
            firm_name="Big Business Consulting",
            departments=departments,
            employee="Айдос",
            service_kind=SUBSCRIPTION,
            status="Продление",
            contract_amount=amount,
            paid_amount=None,
            avr_amount=None,
            saldo_start=None,
            saldo_end=None,
            diff_avr_paid=None,
            invoiced=True,
            invoice_no="1",
            invoice_date=None,
            paid=False,
        )

    rows = [
        row(2, ("НО",), 100.0),
        row(3, ("ЮО",), 200.0),
        row(4, ("НО", "ЮО"), 300.0),  # shared between departments
        row(5, (), 400.0),  # unassigned → admin only
    ]
    snapshot = live.Snapshot(revision=7, changed_at="2026-07-26T00:00:00+00:00", rows=rows)
    # `/dataset` goes through ensure_loaded, `/revision` reads the snapshot directly.
    monkeypatch.setattr(live, "ensure_loaded", lambda: snapshot)
    monkeypatch.setattr(live, "get_snapshot", lambda: snapshot)
    monkeypatch.setattr(service.bbc_settings, "spreadsheet_id", "sheet-id", raising=False)
    monkeypatch.setattr(service.bbc_settings, "enabled", True, raising=False)
    return rows


def test_admin_dataset_returns_every_row(admin_client: TestClient, fake_rows) -> None:
    payload = admin_client.get(f"{BASE}/dataset").json()

    assert payload["revision"] == 7
    assert {row["index"] for row in payload["rows"]} == {2, 3, 4, 5}


def test_link_dataset_returns_only_its_department(admin_client: TestClient, fake_rows) -> None:
    """Rows outside the scope must not reach the browser at all."""
    token = _token_of(_issue_link(admin_client, "НО"))
    admin_client.post(f"{BASE}/auth/logout")

    payload = admin_client.get(f"{BASE}/dataset", headers={"X-BBC-Link": token}).json()

    assert {row["index"] for row in payload["rows"]} == {2, 4}
    assert all("ЮО" not in row["departments"] or "НО" in row["departments"] for row in payload["rows"])


def test_unassigned_rows_never_reach_a_department_link(
    admin_client: TestClient, fake_rows
) -> None:
    token = _token_of(_issue_link(admin_client, "НО"))
    admin_client.post(f"{BASE}/auth/logout")

    payload = admin_client.get(f"{BASE}/dataset", headers={"X-BBC-Link": token}).json()
    assert 5 not in {row["index"] for row in payload["rows"]}


def test_dimensions_are_narrowed_to_the_scope(admin_client: TestClient, fake_rows) -> None:
    """A filter list must not reveal clients the caller cannot see."""
    token = _token_of(_issue_link(admin_client, "НО"))
    admin_client.post(f"{BASE}/auth/logout")

    payload = admin_client.get(f"{BASE}/dataset", headers={"X-BBC-Link": token}).json()

    assert payload["dimensions"]["clients"] == ["Клиент 2", "Клиент 4"]
    assert payload["dimensions"]["departments"] == ["НО", "ЮО"]  # from the shared row only


def test_coverage_counts_only_visible_rows(admin_client: TestClient, fake_rows) -> None:
    """Totals computed on the full set would leak the size of the hidden data."""
    token = _token_of(_issue_link(admin_client, "НО"))
    admin_client.post(f"{BASE}/auth/logout")

    payload = admin_client.get(f"{BASE}/dataset", headers={"X-BBC-Link": token}).json()
    assert payload["coverage"]["rows"] == 2


def test_dataset_requires_a_credential(client: TestClient, fake_rows) -> None:
    assert client.get(f"{BASE}/dataset").status_code == 401


def test_revision_requires_a_credential(client: TestClient, fake_rows) -> None:
    assert client.get(f"{BASE}/revision").status_code == 401


def test_revision_is_served_to_a_link_holder(admin_client: TestClient, fake_rows) -> None:
    token = _token_of(_issue_link(admin_client, "НО"))
    admin_client.post(f"{BASE}/auth/logout")

    payload = admin_client.get(f"{BASE}/revision", headers={"X-BBC-Link": token}).json()
    assert payload["revision"] == 7


def test_warnings_block_is_admin_only(admin_client: TestClient, fake_rows) -> None:
    """Link scopes cover receivables/analytics/calendar — not the warnings block."""
    assert admin_client.get(f"{BASE}/warnings").status_code == 200

    token = _token_of(_issue_link(admin_client, "НО"))
    admin_client.post(f"{BASE}/auth/logout")
    assert admin_client.get(f"{BASE}/warnings", headers={"X-BBC-Link": token}).status_code == 403


def test_warnings_report_unassigned_rows_to_the_admin(
    admin_client: TestClient, fake_rows
) -> None:
    payload = admin_client.get(f"{BASE}/warnings").json()

    assert payload["summary"]["total"] > 0
    assert "no_department" in {item["code"] for item in payload["warnings"]}
