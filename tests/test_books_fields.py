"""Колонки книги: их можно заводить, переименовывать и убирать.

Зачем это вообще есть
─────────────────────
Книга должна жить дальше без Google. Завели новый вид расхода — колонку под
него надо добавить здесь, а не идти в чужую таблицу и ждать повторного импорта.
Значит состав колонок — правда, которую меняют, а не слепок чтения листа.

Самая опасная точка — стык с импортом. `sync_fields` помечает удалённым всё,
чего не оказалось в прочитанном листе; колонки, заведённой у нас, в листе
Google не будет никогда. Проверка на это стоит первой.
"""
from __future__ import annotations

import pytest

from app.books import service
from app.books.db import books_session
from app.books.models import Binding, Book, BookField, BookRow, BookTable, RowFact


@pytest.fixture
def books_db(tmp_path, monkeypatch):
    """Своя база на прогон: на SQLite таблица `books` общая с web-excel.

    Подробнее — в `test_books_rows.py`, там та же фикстура и то же объяснение.
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
    with books_session() as session:
        workspace = service.ensure_workspace(session)
        book = Book(workspace_id=workspace.id, title="Книга", source_ref="")
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
                    workspace_id=workspace.id, table_id=table.id, key=key,
                    title=title, type=kind, position=position, origin="source",
                )
            )
        for position, values in enumerate(
            [
                {"data": "2026-01-10", "kto": "ТОО Ромашка", "summa": "1000.00"},
                {"data": "2026-02-11", "kto": "ИП Астра", "summa": "2000.00"},
            ]
        ):
            session.add(
                BookRow(
                    workspace_id=workspace.id, table_id=table.id,
                    position=(position + 1) * 1000, values=values, base={}, origin="source",
                )
            )
        session.flush()
        session.expunge(table)
        return table


def _keys(session, table: BookTable) -> list[str]:
    fields, _ = service.suggest_for_table(session, table)
    return [f.key for f in fields]


# ── Стык с импортом ──────────────────────────────────────────────────────────


def test_reimport_does_not_hide_a_column_added_here(table: BookTable) -> None:
    """Главная проверка файла.

    `sync_fields` прячет колонки, которых нет в листе. Колонки, заведённой в
    приложении, в листе Google нет и не будет — и без признака происхождения
    первое же повторное чтение книги спрятало бы её молча.
    """
    with books_session() as session:
        service.add_field(session, table, title="Наша колонка", actor="admin")
        grid = [["Дата", "Контрагент", "Сумма"], ["2026-01-10", "ТОО Ромашка", "1000"]]

        service.sync_fields(session, table, grid)

        assert "nasha_kolonka" in _keys(session, table)


def test_reimport_still_hides_a_column_gone_from_the_book(table: BookTable) -> None:
    """Обратное свойство не должно пострадать: колонку, исчезнувшую из книги,
    импорт по-прежнему убирает с глаз."""
    with books_session() as session:
        service.sync_fields(session, table, [["Дата", "Контрагент"], ["2026-01-10", "ТОО"]])
        assert "summa" not in _keys(session, table)


# ── Заведение ────────────────────────────────────────────────────────────────


def test_a_new_column_lands_where_asked(table: BookTable) -> None:
    with books_session() as session:
        service.add_field(session, table, title="Проект", after="kto", actor="admin")
        assert _keys(session, table) == ["data", "kto", "proekt", "summa"]


def test_without_a_neighbour_the_column_goes_last(table: BookTable) -> None:
    with books_session() as session:
        service.add_field(session, table, title="Проект", actor="admin")
        assert _keys(session, table)[-1] == "proekt"


def test_a_nameless_column_is_refused(table: BookTable) -> None:
    with books_session() as session:
        with pytest.raises(service.BooksError):
            service.add_field(session, table, title="   ", actor="admin")


def test_an_unknown_type_is_refused(table: BookTable) -> None:
    with books_session() as session:
        with pytest.raises(service.BooksError):
            service.add_field(session, table, title="Цена", type="деньги", actor="admin")


def test_adding_a_removed_column_brings_its_values_back(table: BookTable) -> None:
    """Значения удалённой колонки лежат в строках под её ключом.

    Завести рядом вторую с ключом `summa_2` значило бы оставить прежние числа
    висеть в невидимой колонке — и человек, вернувший «Сумму», увидел бы
    пустоту вместо своих данных.
    """
    with books_session() as session:
        service.remove_field(session, table, "summa", actor="admin")
        service.add_field(session, table, title="Сумма", type="money", actor="admin")

        assert "summa" in _keys(session, table)
        row = session.query(BookRow).filter_by(table_id=table.id).order_by(BookRow.position).first()
        assert row.values["summa"] == "1000.00"


# ── Переименование ───────────────────────────────────────────────────────────


def test_renaming_keeps_the_key(table: BookTable) -> None:
    """Ключом адресованы значения всех строк, привязка и настройки.

    Менять его вслед за заголовком значило бы переписать `rows.values` всей
    книги ради исправленной опечатки.
    """
    with books_session() as session:
        field = service.update_field(session, table, "summa", title="Сумма, тг", actor="admin")
        assert field.key == "summa"
        row = session.query(BookRow).filter_by(table_id=table.id).order_by(BookRow.position).first()
        assert row.values["summa"] == "1000.00"


def test_the_old_name_is_remembered(table: BookTable) -> None:
    """Чтобы повторный импорт узнал колонку под прежним заголовком."""
    with books_session() as session:
        service.update_field(session, table, "summa", title="Сумма, тг", actor="admin")
        field = session.query(BookField).filter_by(table_id=table.id, key="summa").one()
        assert "Сумма" in field.names


def test_a_column_can_be_moved(table: BookTable) -> None:
    with books_session() as session:
        service.update_field(session, table, "summa", after=None, move=True, actor="admin")
        assert _keys(session, table)[0] == "summa"


def test_an_unknown_column_is_not_found(table: BookTable) -> None:
    with books_session() as session:
        with pytest.raises(service.BooksError):
            service.update_field(session, table, "nikakaya", title="Что-то", actor="admin")


# ── Удаление ─────────────────────────────────────────────────────────────────


def test_usage_counts_only_filled_cells(table: BookTable) -> None:
    """Число нужно ДО удаления: «убрать колонку» и «убрать колонку и 3128
    заполненных значений» — разные решения."""
    with books_session() as session:
        assert service.field_usage(session, table.id, "summa") == 2
        service.save_row(
            session,
            table,
            row_id=session.query(BookRow).filter_by(table_id=table.id).first().id,
            values={"summa": ""},
            actor="admin",
        )
        assert service.field_usage(session, table.id, "summa") == 1


def test_removing_a_column_hides_it_but_keeps_the_values(table: BookTable) -> None:
    with books_session() as session:
        hidden = service.remove_field(session, table, "summa", actor="admin")

        assert hidden == 2
        assert "summa" not in _keys(session, table)
        row = session.query(BookRow).filter_by(table_id=table.id).order_by(BookRow.position).first()
        assert row.values["summa"] == "1000.00"


def test_removing_a_column_takes_its_binding_and_facts(table: BookTable) -> None:
    """Производное для невидимой колонки держать нельзя: дашборд продолжил бы
    считать по колонке, которой на экране нет."""
    with books_session() as session:
        field = session.query(BookField).filter_by(table_id=table.id, key="summa").one()
        session.add(
            Binding(
                workspace_id=table.workspace_id, table_id=table.id,
                field_id=field.id, role_key="inflow",
            )
        )
        session.flush()
        service.rebuild_facts(session, table.id)
        assert session.query(RowFact).filter_by(table_id=table.id, role_key="inflow").count() == 2

        service.remove_field(session, table, "summa", actor="admin")

        assert session.query(RowFact).filter_by(table_id=table.id, role_key="inflow").count() == 0
        assert session.query(Binding).filter_by(table_id=table.id, field_id=field.id).count() == 0


def test_removing_an_unknown_column_is_refused(table: BookTable) -> None:
    with books_session() as session:
        with pytest.raises(service.BooksError):
            service.remove_field(session, table, "nikakaya", actor="admin")


def test_removing_twice_is_refused(table: BookTable) -> None:
    with books_session() as session:
        service.remove_field(session, table, "summa", actor="admin")
        with pytest.raises(service.BooksError):
            service.remove_field(session, table, "summa", actor="admin")
