"""Заходы: кто их видит, кто может оборвать и когда сессия продлевается.

Главное здесь — не список на экране, а граница. Сотрудник не должен видеть
заходы администратора: это карта того, откуда и когда приходит человек с полным
доступом. Проверок на границу поэтому больше, чем на всё остальное вместе, и
написаны они так, чтобы падать при попытке границу ослабить.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.bbc import auth as auth_module
from app.bbc.auth import (
    AuthError,
    AuthedUser,
    end_session,
    hash_password,
    hash_token,
    list_sessions,
    login,
    resolve_session,
    session_window,
)
from app.bbc.db import bbc_session
from app.bbc.devices import describe_device, is_mobile
from app.bbc.models import BbcUser, BbcUserSession

PASSWORD = "secret123"

CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


@pytest.fixture(autouse=True)
def clean_users():
    def _wipe():
        with bbc_session() as session:
            session.query(BbcUserSession).delete()
            session.query(BbcUser).delete()

    _wipe()
    yield
    _wipe()


def _make(username: str, role: str) -> AuthedUser:
    with bbc_session() as session:
        user = BbcUser(username=username, password_hash=hash_password(PASSWORD), role=role)
        session.add(user)
        session.flush()
        return AuthedUser(id=user.id, username=user.username, role=user.role)


@pytest.fixture
def admin() -> AuthedUser:
    return _make("admin", "admin")


@pytest.fixture
def worker() -> AuthedUser:
    return _make("buhgalter", "employee")


# ── Кто что видит ────────────────────────────────────────────────────────────


def test_employee_sees_only_their_own_logins(admin: AuthedUser, worker: AuthedUser) -> None:
    """Сотруднику не показывают заходы администратора. Это и есть граница."""
    login("admin", PASSWORD, ip="10.0.0.1", user_agent=CHROME)
    login("buhgalter", PASSWORD, ip="10.0.0.2", user_agent=IPHONE)

    rows = list_sessions(worker)

    assert [row["user_id"] for row in rows] == [worker.id]
    assert all(row["mine"] for row in rows)


def test_admin_sees_the_logins_of_employees(admin: AuthedUser, worker: AuthedUser) -> None:
    """Обратное направление открыто: заметить чужой заход в учётку бухгалтера
    и оборвать его — работа администратора."""
    login("admin", PASSWORD, ip="10.0.0.1", user_agent=CHROME)
    login("buhgalter", PASSWORD, ip="10.0.0.2", user_agent=IPHONE)

    rows = list_sessions(admin)

    assert {row["user_id"] for row in rows} == {admin.id, worker.id}
    assert {row["username"] for row in rows} == {"admin", "buhgalter"}


def test_own_logins_come_first_for_the_admin(admin: AuthedUser, worker: AuthedUser) -> None:
    """Свой заход админ ищет чаще, чем чужой, и листать за ним не должен."""
    login("buhgalter", PASSWORD, user_agent=IPHONE)
    login("admin", PASSWORD, user_agent=CHROME)

    rows = list_sessions(admin)

    assert rows[0]["mine"] is True


def test_the_current_login_is_marked(admin: AuthedUser) -> None:
    """Тот заход, из которого пришёл запрос, помечен — его обрыв это выход."""
    token = login("admin", PASSWORD, user_agent=CHROME)
    login("admin", PASSWORD, user_agent=IPHONE)

    rows = list_sessions(admin, current_token=token)

    assert [row["current"] for row in rows].count(True) == 1


def test_expired_logins_are_not_shown(admin: AuthedUser) -> None:
    """Список — это то, что открыто сейчас, а не журнал за всё время.

    Просроченные лежат в таблице до ближайшей уборки, и показывать их значило
    бы звать человека обрывать то, что уже оборвалось само.
    """
    login("admin", PASSWORD, user_agent=CHROME)
    with bbc_session() as session:
        record = session.scalars(select(BbcUserSession)).one()
        record.expires_at = datetime.now(UTC) - timedelta(minutes=1)

    assert list_sessions(admin) == []


def test_the_password_hash_never_leaves_with_the_list(admin: AuthedUser) -> None:
    login("admin", PASSWORD, user_agent=CHROME)
    row = list_sessions(admin)[0]
    assert "token_hash" not in row
    assert "password_hash" not in row


# ── Кто что может оборвать ───────────────────────────────────────────────────


def test_employee_cannot_end_someone_elses_login(admin: AuthedUser, worker: AuthedUser) -> None:
    login("admin", PASSWORD, user_agent=CHROME)
    victim = list_sessions(admin)[0]["id"]

    with pytest.raises(AuthError):
        end_session(worker, victim)

    with bbc_session() as session:
        assert session.get(BbcUserSession, victim) is not None


def test_a_stranger_session_answers_like_a_missing_one(worker: AuthedUser, admin: AuthedUser) -> None:
    """Иначе перебор идентификаторов выдал бы список чужих заходов по одному:
    «не найдена» на выдуманный и «нельзя» на настоящий — уже ответ."""
    login("admin", PASSWORD, user_agent=CHROME)
    real = list_sessions(admin)[0]["id"]

    with pytest.raises(AuthError) as stranger:
        end_session(worker, real)
    with pytest.raises(AuthError) as missing:
        end_session(worker, "00000000000000000000000000000000")

    assert str(stranger.value) == str(missing.value)


def test_employee_ends_their_own_login(worker: AuthedUser) -> None:
    login("buhgalter", PASSWORD, user_agent=IPHONE)
    mine = list_sessions(worker)[0]["id"]

    end_session(worker, mine)

    assert list_sessions(worker) == []


def test_admin_ends_an_employee_login(admin: AuthedUser, worker: AuthedUser) -> None:
    token = login("buhgalter", PASSWORD, user_agent=IPHONE)
    target = [row for row in list_sessions(admin) if row["user_id"] == worker.id][0]["id"]

    end_session(admin, target)

    # Оборванный заход перестаёт пускать немедленно, а не после истечения.
    assert resolve_session(token) is None


def test_ending_the_current_login_is_reported(admin: AuthedUser) -> None:
    """Маршрут по этому признаку убирает cookie — иначе браузер продолжил бы
    слать мёртвый токен, а форму входа человек увидел бы неизвестно когда."""
    token = login("admin", PASSWORD, user_agent=CHROME)
    other = login("admin", PASSWORD, user_agent=IPHONE)
    rows = {row["id"]: row for row in list_sessions(admin, current_token=token)}
    mine = [key for key, row in rows.items() if row["current"]][0]
    theirs = [key for key, row in rows.items() if not row["current"]][0]

    assert end_session(admin, theirs, current_token=token) is False
    assert end_session(admin, mine, current_token=token) is True
    assert resolve_session(other) is None


# ── Продление ────────────────────────────────────────────────────────────────


def test_a_session_slides_while_it_is_being_used(admin: AuthedUser) -> None:
    """Окно отсчитывается от последнего обращения, а не от входа.

    Раньше человека выбрасывало на форму ровно через `BBC_SESSION_TTL_HOURS`
    после входа — независимо от того, что он всё это время работал.
    """
    token = login("admin", PASSWORD, user_agent=CHROME)
    with bbc_session() as session:
        record = session.scalars(select(BbcUserSession)).one()
        # Половина окна уже прошла — продление положено.
        record.expires_at = datetime.now(UTC) + session_window() / 4
        was = record.expires_at

    assert resolve_session(token) is not None

    with bbc_session() as session:
        record = session.scalars(select(BbcUserSession)).one()
        assert record.expires_at.replace(tzinfo=UTC) > was


def test_a_fresh_session_is_not_rewritten_on_every_request(admin: AuthedUser) -> None:
    """Дашборд опрашивает бэкенд сам; запись в таблицу сессий на каждый опрос
    была бы платой ни за что."""
    token = login("admin", PASSWORD, user_agent=CHROME)
    with bbc_session() as session:
        was = session.scalars(select(BbcUserSession)).one().expires_at

    assert resolve_session(token) is not None

    with bbc_session() as session:
        assert session.scalars(select(BbcUserSession)).one().expires_at == was


def test_an_expired_session_is_not_revived_by_sliding(admin: AuthedUser) -> None:
    """Продление — для живых. Просроченную сессию оно обязано не воскрешать."""
    token = login("admin", PASSWORD, user_agent=CHROME)
    with bbc_session() as session:
        session.scalars(select(BbcUserSession)).one().expires_at = datetime.now(
            UTC
        ) - timedelta(seconds=1)

    assert resolve_session(token) is None


def test_a_disabled_account_stops_being_let_in(admin: AuthedUser) -> None:
    """Долгая сессия не должна пережить отключение учётки."""
    token = login("admin", PASSWORD, user_agent=CHROME)
    with bbc_session() as session:
        session.get(BbcUser, admin.id).is_active = False

    assert resolve_session(token) is None


def test_changing_the_password_ends_every_session(admin: AuthedUser) -> None:
    """Свойство, которое длинные сессии могли бы незаметно отменить."""
    token = login("admin", PASSWORD, user_agent=CHROME)
    auth_module.change_credentials(
        admin.id, current_password=PASSWORD, new_password="another-secret"
    )
    assert resolve_session(token) is None


def test_the_cookie_token_is_not_stored_as_is(admin: AuthedUser) -> None:
    """Дамп базы не должен выдавать живые сессии — свойство осталось прежним."""
    token = login("admin", PASSWORD, user_agent=CHROME)
    with bbc_session() as session:
        record = session.scalars(select(BbcUserSession)).one()
        assert record.token_hash != token
        assert record.token_hash == hash_token(token)


# ── Как называется устройство ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "agent, expected",
    [
        (CHROME, "Chrome на Windows"),
        (IPHONE, "Safari на iPhone"),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like "
            "Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
            "Edge на Windows",
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
            "Safari на macOS",
        ),
        (
            "Mozilla/5.0 (Linux; Android 13; SM-S911B) AppleWebKit/537.36 (KHTML, like "
            "Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
            "Chrome на Android",
        ),
        ("", "Неизвестное устройство"),
        (None, "Неизвестное устройство"),
        ("curl/8.4.0", "Неизвестное устройство"),
    ],
)
def test_the_device_is_named_or_honestly_unknown(agent: str | None, expected: str) -> None:
    """Придуманное название хуже честного незнания: по нему человек решит, что
    заход был его, и не станет обрывать чужой."""
    assert describe_device(agent) == expected


def test_edge_is_not_mistaken_for_chrome() -> None:
    """Edge и Opera представляются в том числе как Chrome. Поймай мы Chrome
    первым — они не нашлись бы никогда, и все заходы выглядели бы одинаково."""
    edge = "Mozilla/5.0 (Windows NT 10.0) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
    opera = "Mozilla/5.0 (Windows NT 10.0) Chrome/119.0.0.0 Safari/537.36 OPR/105.0.0.0"
    assert describe_device(edge).startswith("Edge")
    assert describe_device(opera).startswith("Opera")


def test_phones_are_told_apart_from_desktops() -> None:
    assert is_mobile(IPHONE) is True
    assert is_mobile(CHROME) is False


# ── Сколько заходов держим живыми ────────────────────────────────────────────


def test_forgotten_logins_do_not_pile_up(admin: AuthedUser) -> None:
    """Список должен оставаться списком, в котором видно чужое устройство.

    Вход создаёт запись, а браузер, который больше не вернулся, её не
    закрывает. Пока окно было 12 часов, такие записи исчезали сами; с окном в
    30 суток кабинет показывал полсотни одинаковых «Chrome на Windows», и
    заметить среди них чужой заход было нельзя — то есть экран не делал ровно
    того, ради чего сделан.
    """
    for _ in range(auth_module.MAX_LIVE_SESSIONS + 8):
        login("admin", PASSWORD, user_agent=CHROME)

    assert len(list_sessions(admin)) == auth_module.MAX_LIVE_SESSIONS


def test_the_most_recently_used_device_survives_the_trim(admin: AuthedUser) -> None:
    """Выкидываем забытые, а не старые.

    Ноутбук, на котором работают каждый день полгода, — самый «старый» по
    времени входа. Считай мы по нему, вылетал бы именно он.
    """
    oldest = login("admin", PASSWORD, user_agent=CHROME)
    # Им пользуются: последнее обращение сдвигается вперёд.
    assert resolve_session(oldest) is not None

    for _ in range(auth_module.MAX_LIVE_SESSIONS + 3):
        login("admin", PASSWORD, user_agent=IPHONE)
        # Прочие заходы созданы, но ими никто не пользовался.

    assert resolve_session(oldest) is not None


def test_trimming_touches_only_the_same_account(admin: AuthedUser, worker: AuthedUser) -> None:
    """Чужие заходы не должны страдать от того, что кто-то часто входит."""
    worker_token = login("buhgalter", PASSWORD, user_agent=IPHONE)
    for _ in range(auth_module.MAX_LIVE_SESSIONS + 5):
        login("admin", PASSWORD, user_agent=CHROME)

    assert resolve_session(worker_token) is not None
    assert len(list_sessions(worker)) == 1
