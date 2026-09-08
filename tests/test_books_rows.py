"""Строки книги: поиск по всей вкладке, правка по ячейке, удаление.

Почему эти три вместе
─────────────────────
Все три появились из-за одного и того же требования: работать в книге должно
быть можно двумя способами — таблицей и карточками, — и оба правят одни и те
же строки. Поверхностей две, поведение обязано быть одно, и проверяется оно
здесь, ниже уровня интерфейса.
"""
from __future__ import annotations

from uuid import UUID

import pytest

from app.books import service
from app.books.db import books_session
from app.books.models import Binding, Book, BookField, BookRow, BookTable, RowFact


@pytest.fixture
def books_db(tmp_path, monkeypatch):
    """Своя база на прогон — и не только ради чистоты.

    Общий файл SQLite для этого набора не годится, и причина не в остатках от
    прошлых прогонов. Таблица `books` объявлена дважды: в этом модуле
    (`books.books` — книги компании) и в web-excel (`webexcel.books` —
    сохранённые образы листов). На Postgres их разводят схемы, а на SQLite
    схем нет, имена совпадают, и первый, кто создаст таблицу, определит её
    вид для второго. Общий файл теста уже был создан web-excel, и вставка
    книги падала на «нет колонки workspace_id».

    Пока SQLite — только тестовая подпорка, отдельная база это закрывает.
    """
    from sqlalchemy import create_engine

    from app.books import db as books_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'books.db'}", future=True)
    monkeypatch.setattr(books_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(books_db_module, "_session_factory", None)
    monkeypatch.setattr(books_db_module, "_initialized", False)
    yield engine


@pytest.fixture
def table(books_db) -> BookTable:
    """Крошечная книга: дата, контрагент, сумма — и три строки в ней."""
    with books_session() as session:
        workspace = service.ensure_workspace(session)
        book = Book(workspace_id=workspace.id, title="Книга проверки", source_ref="")
        session.add(book)
        session.flush()

        table = BookTable(workspace_id=workspace.id, book_id=book.id, name="Лист")
        session.add(table)
        session.flush()

        for position, (key, title, kind) in enumerate(
            [("data", "Дата", "date"), ("kto", "Контрагент", "text"), ("summa", "Сумма", "money")]
        ):
            session.add(
                BookField(
                    workspace_id=workspace.id,
                    table_id=table.id,
                    key=key,
                    title=title,
                    type=kind,
                    position=position,
                )
            )
        session.flush()

        for position, values in enumerate(
            [
                {"data": "2026-01-10", "kto": 'ТОО "Kaspi Bank"', "summa": "1000.00"},
                {"data": "2026-02-11", "kto": "ИП Астра", "summa": "2000.00"},
                {"data": "2026-03-12", "kto": 'ТОО "KASPI Bank"', "summa": "3000.00"},
            ]
        ):
            session.add(
                BookRow(
                    workspace_id=workspace.id,
                    table_id=table.id,
                    position=(position + 1) * 1000,
                    values=values,
                    base={},
                    origin="source",
                )
            )
        session.flush()
        session.expunge(table)
        return table


# ── Поиск ────────────────────────────────────────────────────────────────────


def test_search_looks_at_the_whole_tab(table: BookTable) -> None:
    """Поиск отвечает по книге, а не по загруженной странице.

    Пока он жил на фронте, «нашлось 2» считалось по двумстам приехавшим
    строкам из 3634 — число, похожее на правду, и потому неотличимое от неё.
    """
    with books_session() as session:
        page = service.list_rows(session, table.id, limit=1, query="Kaspi")
        # Всего совпадений два, хотя на страницу поместилась одна.
        assert page["total"] == 2
        assert len(page["rows"]) == 1


def test_search_is_case_insensitive(table: BookTable) -> None:
    """Регистр проверяется на латинице, и это не лень.

    Набор идёт на SQLite, а там две вещи не такие, как на боевом Postgres:
    `values` хранится текстом с экранированием (`\u0420...` вместо буквы), а
    `lower()` знает только латиницу. Кириллица в таком окружении не найдётся
    никогда — и проверка на ней говорила бы о SQLite, а не о поиске.

    Что кириллица и её регистр работают, проверено на живом Postgres пилотной
    книги: «СЕКСЕНБАЕВ», «сексенбаев» и «Сексенбаев» дают одни и те же 6 строк.
    Латиницы в этих книгах хватает — половина названий счетов начинается с
    «Kaspi», — так что проверка не выдуманная.
    """
    with books_session() as session:
        assert service.list_rows(session, table.id, query="kaspi")["total"] == 2


def test_search_finds_by_any_column(table: BookTable) -> None:
    """Искать будут и по сумме, и по дате — по тому, что видно в таблице."""
    with books_session() as session:
        assert service.list_rows(session, table.id, query="2026-02")["total"] == 1
        assert service.list_rows(session, table.id, query="3000")["total"] == 1


def test_search_does_not_treat_percent_as_a_wildcard(table: BookTable) -> None:
    """«100%» — это текст, а не «что угодно».

    Без экранирования такой запрос совпал бы с каждой строкой книги и выдал бы
    «нашлось 3634» на запрос, которому не отвечает ничего.
    """
    with books_session() as session:
        assert service.list_rows(session, table.id, query="%")["total"] == 0
        assert service.list_rows(session, table.id, query="_")["total"] == 0


def test_empty_search_returns_everything(table: BookTable) -> None:
    with books_session() as session:
        assert service.list_rows(session, table.id, query="  ")["total"] == 3


# ── Правка по одной ячейке ───────────────────────────────────────────────────


def test_patch_touches_only_the_named_column(table: BookTable) -> None:
    """Соседние колонки в запрос не попадают — значит и не затираются.

    Это и позволяет двоим править одну строку одновременно: таблица шлёт ровно
    ту ячейку, которую тронули.
    """
    with books_session() as session:
        row = session.query(BookRow).filter_by(table_id=table.id).order_by(BookRow.position).first()
        service.save_row(session, table, row_id=row.id, values={"kto": "ТОО Пион"}, actor="ы")
        assert row.values == {"data": "2026-01-10", "kto": "ТОО Пион", "summa": "1000.00"}


def test_value_can_be_erased(table: BookTable) -> None:
    """Пустая строка — законное значение и означает «стереть».

    Пока форма отбрасывала пустое, стереть ошибочную сумму было нечем: старая
    оставалась в книге при любом сохранении.
    """
    with books_session() as session:
        row = session.query(BookRow).filter_by(table_id=table.id).order_by(BookRow.position).first()
        service.save_row(session, table, row_id=row.id, values={"summa": ""}, actor="ы")
        assert row.values["summa"] == ""


def test_stale_version_is_refused(table: BookTable) -> None:
    with books_session() as session:
        row = session.query(BookRow).filter_by(table_id=table.id).order_by(BookRow.position).first()
        service.save_row(session, table, row_id=row.id, values={"kto": "Первый"}, version=1, actor="a")
        with pytest.raises(service.BooksError):
            service.save_row(session, table, row_id=row.id, values={"kto": "Второй"}, version=1, actor="b")


# ── Пересборка проекции по одной строке ──────────────────────────────────────


def _bind(session, table: BookTable, key: str, role: str) -> None:
    field = session.query(BookField).filter_by(table_id=table.id, key=key).one()
    session.add(
        Binding(
            workspace_id=table.workspace_id,
            table_id=table.id,
            field_id=field.id,
            role_key=role,
        )
    )
    session.flush()


def test_row_rebuild_matches_full_rebuild(table: BookTable) -> None:
    """Пересборка одной строки даёт то же, что пересборка всей вкладки.

    Это и есть условие, при котором дешёвую операцию можно ставить вместо
    дорогой. Дорогая на пилотном журнале — 30 780 фактов и шесть секунд, и
    висела она на каждом сохранении строки.
    """
    with books_session() as session:
        _bind(session, table, "summa", "inflow")
        _bind(session, table, "kto", "counterparty")
        service.rebuild_facts(session, table.id)
        whole = {
            (str(f.row_id), f.role_key, str(f.text_value), str(f.num_value))
            for f in session.query(RowFact).filter_by(table_id=table.id)
        }

        for row in session.query(BookRow).filter_by(table_id=table.id):
            service.rebuild_row_facts(session, row)
        by_row = {
            (str(f.row_id), f.role_key, str(f.text_value), str(f.num_value))
            for f in session.query(RowFact).filter_by(table_id=table.id)
        }

        assert by_row == whole


def test_row_rebuild_leaves_other_rows_alone(table: BookTable) -> None:
    with books_session() as session:
        _bind(session, table, "summa", "inflow")
        service.rebuild_facts(session, table.id)
        rows = session.query(BookRow).filter_by(table_id=table.id).order_by(BookRow.position).all()

        service.save_row(session, table, row_id=rows[0].id, values={"summa": "77.00"}, actor="ы")
        service.rebuild_row_facts(session, rows[0])

        facts = {
            str(f.row_id): f.num_value
            for f in session.query(RowFact).filter_by(table_id=table.id, role_key="inflow")
        }
        assert len(facts) == 3
        assert str(facts[str(rows[0].id)]) == "77.00"
        assert str(facts[str(rows[1].id)]) == "2000.00"


# ── Удаление ─────────────────────────────────────────────────────────────────


def test_delete_hides_the_row_and_its_facts(table: BookTable) -> None:
    """Убранная строка исчезает и из списка, и из расчётов."""
    with books_session() as session:
        _bind(session, table, "summa", "inflow")
        service.rebuild_facts(session, table.id)
        row = session.query(BookRow).filter_by(table_id=table.id).order_by(BookRow.position).first()

        service.delete_row(session, table, row_id=row.id, version=row.version, actor="ы")

        assert service.list_rows(session, table.id)["total"] == 2
        assert session.query(RowFact).filter_by(row_id=row.id).count() == 0


def test_delete_is_soft(table: BookTable) -> None:
    """Строка книги — чья-то операция с деньгами; «удалил не ту» обязано быть
    исправимо, поэтому в базе она остаётся с отметкой времени."""
    with books_session() as session:
        row = session.query(BookRow).filter_by(table_id=table.id).order_by(BookRow.position).first()
        service.delete_row(session, table, row_id=row.id, version=row.version, actor="ы")
        assert session.get(BookRow, row.id).deleted_at is not None


def test_delete_refuses_a_stale_version(table: BookTable) -> None:
    with books_session() as session:
        row = session.query(BookRow).filter_by(table_id=table.id).order_by(BookRow.position).first()
        with pytest.raises(service.BooksError):
            service.delete_row(session, table, row_id=row.id, version=row.version + 5, actor="ы")


def test_delete_twice_is_refused(table: BookTable) -> None:
    with books_session() as session:
        row = session.query(BookRow).filter_by(table_id=table.id).order_by(BookRow.position).first()
        service.delete_row(session, table, row_id=row.id, version=row.version, actor="ы")
        with pytest.raises(service.BooksError):
            service.delete_row(session, table, row_id=row.id, actor="ы")


def test_row_id_of_another_table_is_not_found(table: BookTable) -> None:
    with books_session() as session:
        with pytest.raises(service.BooksError):
            service.delete_row(
                session, table, row_id=UUID(int=0), actor="ы"
            )
