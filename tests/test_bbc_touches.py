"""Журнал касаний: область видимости, авторство, файлы.

Две вещи, которые эта фича обязана делать правильно и которые ломаются молча:

1. Дана видит касания по **своим** клиентам, но по ним — **все**, включая чужие.
   Первое без второго — утечка, второе без первого — бесполезный журнал.
2. Файл, приложенный к чужому долгу, не отдаётся по угаданному id.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.bbc import touches as touches_module
from app.bbc.auth import AuthedUser
from app.bbc.db import bbc_session
from app.bbc.models import BbcClientTouch, BbcTouchFile
from app.bbc.scope import Scope, filter_rows

DANA = AuthedUser(id=1, username="dana", role="employee", full_name="Дана Жумабекова")
BOSS = AuthedUser(id=2, username="admin", role="admin", full_name="Бисултан")


@pytest.fixture(autouse=True)
def clean_touches():
    def _wipe():
        with bbc_session() as session:
            session.query(BbcTouchFile).delete()
            session.query(BbcClientTouch).delete()

    _wipe()
    yield
    _wipe()


def _form(client: str = "ТОО Окстрой", **overrides) -> dict:
    return touches_module.parse_touch_input(
        client=client,
        contacted_at=overrides.get("contacted_at", "2026-07-12"),
        contact_role=overrides.get("contact_role", "chief_accountant"),
        contact_name=overrides.get("contact_name", "Айгуль"),
        channel=overrides.get("channel", "whatsapp"),
        summary=overrides.get("summary", "Обещали оплатить до пятницы"),
    )


# ── Ключ клиента ─────────────────────────────────────────────────────────────────


def test_client_key_matches_the_registry_grouping() -> None:
    """Ключ обязан совпадать с clientKey() фронтенда — иначе касание, записанное
    из реестра, не найдётся в журнале по тому же клиенту."""
    assert touches_module.client_key("ТОО Окстрой") == touches_module.client_key("тоо  окстрой ")
    assert touches_module.client_key("  ") == ""


# ── Разбор формы ─────────────────────────────────────────────────────────────────


def test_summary_is_required() -> None:
    """Касание без итога — строка «я звонил», по которой ничего не решить."""
    with pytest.raises(touches_module.TouchError):
        _form(summary="   ")


def test_unknown_contact_role_is_refused() -> None:
    """Должность из справочника, иначе «3 раза главбуху» не посчитать."""
    with pytest.raises(touches_module.TouchError):
        _form(contact_role="кто-то там")


def test_future_date_is_refused() -> None:
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    with pytest.raises(touches_module.TouchError):
        _form(contacted_at=tomorrow)


def test_backdating_is_allowed() -> None:
    """Задним числом пишут постоянно — вспомнили через неделю."""
    assert _form(contacted_at="2026-01-05")["contacted_at"] == date(2026, 1, 5)


def test_unknown_channel_falls_back_instead_of_failing() -> None:
    """Канал — украшение, а не смысл: из-за него терять запись нельзя."""
    assert _form(channel="почтовый голубь")["channel"] == "whatsapp"


# ── Область видимости ────────────────────────────────────────────────────────────


def test_employee_sees_every_touch_on_their_own_client() -> None:
    touches_module.create_touch(DANA, _form("ТОО Окстрой"))
    touches_module.create_touch(BOSS, _form("ТОО Окстрой", contact_role="director"))
    touches_module.create_touch(BOSS, _form("ТОО Чужой"))

    mine = touches_module.list_touches({touches_module.client_key("ТОО Окстрой")})

    # Оба касания по своему клиенту — и своё, и директора. Дана обязана знать,
    # что он туда уже писал.
    assert len(mine) == 2
    assert {item["author"] for item in mine} == {"Дана Жумабекова", "Бисултан"}
    assert all(item["client"] == "ТОО Окстрой" for item in mine)


def test_admin_sees_everything() -> None:
    touches_module.create_touch(DANA, _form("ТОО Окстрой"))
    touches_module.create_touch(BOSS, _form("ТОО Чужой"))

    assert len(touches_module.list_touches(None)) == 2


def test_employee_without_visible_clients_sees_an_empty_journal() -> None:
    """Пустое множество — это «ничего», а не «всё». Классическая ошибка."""
    touches_module.create_touch(BOSS, _form("ТОО Чужой"))

    assert touches_module.list_touches(set()) == []


def test_counts_are_scoped_the_same_way() -> None:
    touches_module.create_touch(DANA, _form("ТОО Окстрой"))
    touches_module.create_touch(DANA, _form("ТОО Окстрой", contact_role="assistant"))
    touches_module.create_touch(BOSS, _form("ТОО Чужой"))

    counts = touches_module.count_by_client({touches_module.client_key("ТОО Окстрой")})

    assert counts == {touches_module.client_key("ТОО Окстрой"): 2}


# ── Авторство ────────────────────────────────────────────────────────────────────


def test_author_name_is_a_snapshot_not_a_join() -> None:
    """«Жанара написала главбуху» обязано пережить удаление её учётки."""
    created = touches_module.create_touch(DANA, _form())

    with bbc_session() as session:
        row = session.get(BbcClientTouch, created["id"])
        assert row.author_name == "Дана Жумабекова"


def test_employee_cannot_edit_someone_elses_touch() -> None:
    created = touches_module.create_touch(BOSS, _form())

    # Чужое отвечает тем же «не найдено», что и несуществующее: подтверждать
    # существование записи, к которой нет доступа, незачем.
    with pytest.raises(touches_module.TouchError):
        touches_module.update_touch(DANA, created["id"], _form(summary="подменил"))
    with pytest.raises(touches_module.TouchError):
        touches_module.delete_touch(DANA, created["id"])


def test_admin_can_edit_any_touch() -> None:
    created = touches_module.create_touch(DANA, _form())

    updated = touches_module.update_touch(BOSS, created["id"], _form(summary="уточнил итог"))

    assert updated["summary"] == "уточнил итог"


def test_delete_is_soft() -> None:
    """История касаний по долгу — доказательная база, а не черновик."""
    created = touches_module.create_touch(DANA, _form())
    touches_module.delete_touch(DANA, created["id"])

    assert touches_module.list_touches(None) == []
    with bbc_session() as session:
        assert session.get(BbcClientTouch, created["id"]).deleted_at is not None


# ── Файлы ────────────────────────────────────────────────────────────────────────

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def test_attached_file_is_readable_by_someone_who_sees_the_client() -> None:
    touch = touches_module.create_touch(DANA, _form())
    touches_module.attach_file(DANA, touch["id"], PNG, "скрин.png", "image/png")

    listing = touches_module.list_touches(None)
    file_id = listing[0]["files"][0]["id"]
    blob, content_type, filename = touches_module.read_file(file_id, None)

    assert blob == PNG
    assert content_type == "image/png"
    assert filename == "скрин.png"


def test_file_on_an_invisible_client_is_not_served() -> None:
    touch = touches_module.create_touch(BOSS, _form("ТОО Чужой"))
    attached = touches_module.attach_file(BOSS, touch["id"], PNG, "скрин.png", "image/png")

    # Угадать id файла ничего не даёт: права проверяются по клиенту касания.
    with pytest.raises(touches_module.TouchError):
        touches_module.read_file(attached["id"], {touches_module.client_key("ТОО Окстрой")})


def test_content_type_is_sniffed_not_trusted() -> None:
    """Заявленный тип пишет клиент. Проверяем по содержимому."""
    touch = touches_module.create_touch(DANA, _form())

    from app.bbc.storage import StorageError

    with pytest.raises(StorageError):
        # Исполняемый файл, представленный картинкой.
        touches_module.attach_file(DANA, touch["id"], b"MZ\x90\x00" * 8, "x.png", "image/png")


def test_path_traversal_in_the_filename_is_stripped() -> None:
    touch = touches_module.create_touch(DANA, _form())

    attached = touches_module.attach_file(
        DANA, touch["id"], PNG, "../../../etc/passwd.png", "image/png"
    )

    assert attached["filename"] == "passwd.png"


def test_oversized_file_is_refused() -> None:
    from app.bbc.storage import MAX_FILE_BYTES, StorageError

    touch = touches_module.create_touch(DANA, _form())
    huge = b"\x89PNG\r\n\x1a\n" + b"0" * MAX_FILE_BYTES

    with pytest.raises(StorageError):
        touches_module.attach_file(DANA, touch["id"], huge, "big.png", "image/png")


def test_file_count_per_touch_is_capped() -> None:
    from app.bbc.storage import MAX_FILES_PER_TOUCH

    touch = touches_module.create_touch(DANA, _form())
    for index in range(MAX_FILES_PER_TOUCH):
        touches_module.attach_file(DANA, touch["id"], PNG, f"{index}.png", "image/png")

    with pytest.raises(touches_module.TouchError):
        touches_module.attach_file(DANA, touch["id"], PNG, "лишний.png", "image/png")


# ── Сужение строк по сотруднику ──────────────────────────────────────────────────


def test_own_scope_narrows_rows_to_the_employees_own_clients() -> None:
    """Ось «чьи строки» живёт в filter_rows и должна доужимать, а не расширять."""
    rows = [
        {"departments": ("ОБО",), "employee": "Дана", "client": "ТОО Окстрой"},
        {"departments": ("ОБО",), "employee": "Дана Ж.", "client": "ТОО Второй"},
        {"departments": ("ОБО",), "employee": "Жанара", "client": "ТОО Чужой"},
        {"departments": ("НО",), "employee": "Дана", "client": "ТОО Другой отдел"},
    ]
    scope = Scope.for_employee(
        user_id=1,
        departments=["ОБО"],
        blocks=["receivables"],
        data_scope="own",
        employee_aliases=["дана", "Дана Ж."],
    )

    visible = filter_rows(rows, scope)

    # Своё в своём отделе — да. Чужое — нет. Своё в чужом отделе — тоже нет:
    # сужение по сотруднику не расширяет то, что разрешено отделом.
    assert [row["client"] for row in visible] == ["ТОО Окстрой", "ТОО Второй"]


def test_department_scope_ignores_the_employee_column() -> None:
    rows = [
        {"departments": ("ОБО",), "employee": "Дана", "client": "A"},
        {"departments": ("ОБО",), "employee": "Жанара", "client": "B"},
    ]
    scope = Scope.for_employee(
        user_id=1,
        departments=["ОБО"],
        blocks=["receivables"],
        data_scope="department",
    )

    assert len(filter_rows(rows, scope)) == 2


def test_unknown_data_scope_collapses_to_the_narrowest() -> None:
    """Порча этой колонки должна отнимать доступ, а не раздавать."""
    scope = Scope.for_employee(
        user_id=1,
        departments=["ОБО"],
        blocks=["receivables"],
        data_scope="everything",
        employee_aliases=["Дана"],
    )

    assert scope.data_scope == "own"
