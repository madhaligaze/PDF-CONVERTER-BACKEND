"""Границы доступа и денежные правила, которых не было в разборе BUGS.md.

Все проверки здесь идут по-настоящему через HTTP — cookie → зависимость →
область видимости, — потому что ровно так эти дыры и открывались: код, тип и
линтер на них молчали, а экраны выглядели работающими.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.bbc import auth as auth_module
from app.bbc.auth import hash_password
from app.bbc.calendar import build_calendar, outstanding
from app.bbc.dataset import ContractRow, assign_carry_in
from app.bbc.db import bbc_session
from app.bbc.models import BbcAccessLink, BbcUser, BbcUserSession
from app.main import app

PASSWORD = "secret123"
BASE = "/api/v1/bbc"
BOOKS = "/api/v1/books"


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


def _employee(
    username: str,
    *,
    blocks: list[str],
    must_change_password: bool = False,
) -> None:
    with bbc_session() as session:
        session.add(
            BbcUser(
                username=username,
                password_hash=hash_password(PASSWORD),
                role="employee",
                full_name="Сотрудник Тестовый",
                departments=["ОБО"],
                blocks=blocks,
                data_scope="department",
                must_change_password=must_change_password,
            )
        )


def _login(client: TestClient, username: str) -> TestClient:
    response = client.post(
        f"{BASE}/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return client


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin_client(client: TestClient) -> TestClient:
    with bbc_session() as session:
        session.add(
            BbcUser(username="admin", password_hash=hash_password(PASSWORD), role="admin")
        )
    return _login(client, "admin")


# ── B1. Книги обходили область видимости дашборда ────────────────────────────────


def test_temporary_password_does_not_open_the_books(client: TestClient) -> None:
    """Пароль из переписки не должен открывать финансовую книгу компании.

    Именно так дыра и выглядела: `/dataset` отвечал 401, а `/books` работал —
    `require_user` про `must_change_password` не знал.
    """
    _employee("temp", blocks=["receivables", "registries"], must_change_password=True)
    _login(client, "temp")

    assert client.get(f"{BASE}/dataset").status_code == 401
    assert client.get(BOOKS).status_code == 401


def test_employee_without_the_registries_block_cannot_read_books(client: TestClient) -> None:
    _employee("noreg", blocks=["receivables", "touches"])
    _login(client, "noreg")

    assert client.get(BOOKS).status_code == 403


def test_employee_with_the_registries_block_is_let_in() -> None:
    """Право есть — значит пускаем: иначе проверялось бы «никому», а не «кому надо».

    Отказ сервера дальше по маршруту здесь не важен и не подавляет проверку:
    вопрос ровно один — прошла ли зависимость. Поэтому исключения обработчика
    не поднимаются, а разбирается только код ответа.
    """
    _employee("reg", blocks=["receivables", "registries"])
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, "reg")
        assert client.get(BOOKS).status_code not in (401, 403)


def test_anonymous_cannot_read_books(client: TestClient) -> None:
    assert client.get(BOOKS).status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("put", f"{BOOKS}/tables/{'0' * 8}-0000-0000-0000-{'0' * 12}/bindings",
         {"field_key": "f", "role_key": "saldo_end"}),
        ("post", f"{BOOKS}/import/preview", {"spreadsheet_id": "1234567890", "tab": "Журнал"}),
        ("get", f"{BOOKS}/sources", None),
    ],
)
def test_employee_cannot_rewire_or_import_a_book(
    client: TestClient, method, path, payload
) -> None:
    """Привязка колонки к роли — это то, какие деньги считает дашборд.

    Сотрудник, пересадивший роль «Сальдо конец» на соседнюю колонку, менял бы
    цифру на всех экранах, и никакой ошибки при этом не возникало бы.
    """
    _employee("reg2", blocks=["receivables", "registries"])
    _login(client, "reg2")

    call = getattr(client, method)
    response = call(path, json=payload) if payload is not None else call(path)
    assert response.status_code == 403, response.text


# ── B2. Журнал и продажи не знали про отдел ──────────────────────────────────────


@pytest.mark.parametrize("path", [f"{BASE}/journal", f"{BASE}/sales"])
def test_journal_and_sales_are_admin_only(client: TestClient, path: str) -> None:
    """В ответе продаж — ФОТ с фамилиями и ставками по всей компании.

    Раньше их открывала галочка в карточке сотрудника, стоящая рядом с
    «Дебиторкой», а `scope` в сервисе не использовался вовсе.
    """
    _employee("head", blocks=["receivables", "journal", "sales"])
    _login(client, "head")

    assert client.get(path).status_code == 403


def test_employee_form_no_longer_offers_journal_and_sales(admin_client: TestClient) -> None:
    payload = admin_client.get(f"{BASE}/employees").json()

    assert "journal" not in payload["blocks"]
    assert "sales" not in payload["blocks"]
    # Сказать об этом на экране: пропавший без объяснения раздел — это вопрос,
    # который админ задаст разработчику.
    assert set(payload["admin_blocks"]) == {"journal", "sales"}


def test_admin_block_cannot_be_granted_through_the_api(admin_client: TestClient) -> None:
    """Форма их не показывает — но решает не форма."""
    response = admin_client.post(
        f"{BASE}/employees",
        json={
            "username": "sneaky",
            "full_name": "Обходной Путь",
            "departments": ["ОБО"],
            "blocks": ["receivables", "journal", "sales"],
            "data_scope": "department",
            "employee_aliases": [],
        },
    )
    assert response.status_code == 201, response.text
    granted = response.json()["employee"]["blocks"]
    assert "journal" not in granted and "sales" not in granted


# ── B9. Ручное обновление жгло квоту Google ──────────────────────────────────────


def test_manual_refresh_is_admin_only(client: TestClient) -> None:
    _employee("dept", blocks=["receivables"])
    _login(client, "dept")

    assert client.get(f"{BASE}/dataset", params={"refresh": "true"}).status_code == 403
    # Без refresh тот же маршрут работает: закрыт поход в Google, а не данные.
    assert client.get(f"{BASE}/dataset").status_code != 403


# ── B10. Служебные утечки ────────────────────────────────────────────────────────


def test_status_does_not_leak_the_spreadsheet_id_to_anonymous(client: TestClient) -> None:
    payload = client.get(f"{BASE}/status").json()

    assert payload["spreadsheet_id"] is None
    # Экрану «не настроено» этого хватает — ради него маршрут и публичный.
    assert "configured" in payload and "detail" in payload


def test_revision_does_not_report_the_size_of_the_whole_book(client: TestClient) -> None:
    _employee("dept2", blocks=["receivables"])
    _login(client, "dept2")

    payload = client.get(f"{BASE}/revision").json()

    assert "rows" not in payload
    assert "revision" in payload and "changed_at" in payload


def test_admin_still_sees_the_row_count(admin_client: TestClient) -> None:
    assert "rows" in admin_client.get(f"{BASE}/revision").json()


def test_login_is_throttled_after_repeated_failures(client: TestClient) -> None:
    """argon2id делает попытку дорогой, но не запрещает миллион.

    Пароли здесь диктуют по телефону, временный — из трёх слогов и живёт в
    переписке; перебор с десятка процессов это не теория.
    """
    with bbc_session() as session:
        session.add(
            BbcUser(username="victim", password_hash=hash_password(PASSWORD), role="admin")
        )

    for _ in range(auth_module.MAX_LOGIN_FAILURES):
        assert (
            client.post(f"{BASE}/auth/login", json={"username": "victim", "password": "nope"})
        ).status_code == 401

    blocked = client.post(f"{BASE}/auth/login", json={"username": "victim", "password": PASSWORD})
    assert blocked.status_code == 401
    assert "попыток" in blocked.json()["detail"]

    # Счёт ведётся и по логину, и по IP — снимаем оба, иначе «отпустило» не
    # наступит и проверка расскажет про клиента, а не про правило.
    auth_module._failures.clear()
    assert (
        client.post(f"{BASE}/auth/login", json={"username": "victim", "password": PASSWORD})
    ).status_code == 200


# ── B5. Знак входящего сальдо ────────────────────────────────────────────────────


def _row(index: int, *, saldo_start: float | None, debt: float | None) -> ContractRow:
    return ContractRow(
        index=index,
        month=1,
        period_label="",
        client="ТОО Тест",
        contract_no="№1",
        subject="",
        firm="BBC",
        firm_name="BBC",
        departments=("ОБО",),
        employee="",
        service_kind="Абонентская плата",
        status="",
        contract_amount=100_000.0,
        paid_amount=None,
        avr_amount=None,
        saldo_start=saldo_start,
        saldo_end=None,
        diff_avr_paid=None,
        invoiced=None,
        invoice_no="",
        invoice_date=None,
        paid=None,
        debt=debt,
    )


def test_negative_opening_balance_is_debt() -> None:
    """Живой пример из книги: −66 000 в первой строке значит «должен 66 000»."""
    rows = [_row(2, saldo_start=-66_000.0, debt=99_000.0)]
    assign_carry_in(rows)

    assert rows[0].carry_in == 66_000.0
    assert rows[0].total_debt == 165_000.0


def test_positive_opening_balance_is_not_debt() -> None:
    """Переплата не долг. `abs` складывал одно с другим, и это выглядело обычной цифрой."""
    rows = [_row(2, saldo_start=50_000.0, debt=100_000.0)]
    assign_carry_in(rows)

    assert rows[0].carry_in is None
    assert rows[0].carry_in_credit == 50_000.0
    assert rows[0].total_debt == 100_000.0


def test_opening_balance_lands_on_the_first_row_only() -> None:
    rows = [_row(3, saldo_start=-85_000.0, debt=85_000.0), _row(2, saldo_start=-66_000.0, debt=99_000.0)]
    assign_carry_in(rows)

    first = next(row for row in rows if row.index == 2)
    later = next(row for row in rows if row.index == 3)
    assert first.carry_in == 66_000.0
    assert later.carry_in is None


# ── B4. Календарь и частичная оплата ─────────────────────────────────────────────


def _awaiting(paid_amount: float | None, paid: bool | None, in_force: bool = True) -> ContractRow:
    row = _row(2, saldo_start=None, debt=None)
    row.contract_amount = 500_000.0
    row.paid_amount = paid_amount
    row.paid = paid
    row.in_force = in_force
    return row


def test_partial_payment_leaves_only_the_remainder() -> None:
    """Договор 500 000, оплачено 200 000, флаг оплаты пуст — ждём 300 000, не 500 000."""
    assert outstanding(_awaiting(200_000.0, None)) == 300_000.0


def test_fully_paid_row_is_out_of_the_calendar() -> None:
    assert outstanding(_awaiting(500_000.0, True)) == 0.0
    assert outstanding(_awaiting(500_000.0, None)) == 0.0


def test_overpayment_is_not_a_negative_expectation() -> None:
    assert outstanding(_awaiting(600_000.0, None)) == 0.0


def test_contract_not_in_force_is_not_expected_money() -> None:
    """«Вид Услуги» = «нет»: сумма зафиксирована, платить по ней пока не за что."""
    assert outstanding(_awaiting(None, None, in_force=False)) == 0.0


def test_calendar_total_counts_the_remainder(monkeypatch) -> None:
    from datetime import date

    row = _awaiting(200_000.0, None)
    row.invoice_date = date(2026, 7, 1)

    calendar = build_calendar([row], "contract", today=date(2026, 7, 10)).to_dict()
    assert calendar["total"] == 300_000.0
