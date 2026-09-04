"""Публичный слой модуля «Книги» — единственная дверь наружу.

Маршруты не трогают модели напрямую. Так уже сделано неправильно у соседа:
`webexcel/routes.py` импортирует модели внутри каждого обработчика, и любое
изменение схемы приходится искать по всем маршрутам.

Здесь же лежит вся работа с базой. Разбор, привязка, выравнивание и слияние
остаются чистыми функциями в своих модулях — сюда они приходят готовыми, и
поэтому таблицу слияния можно проверять тестами без базы и без Google.

Авторизации здесь нет намеренно. «Книги» ничего не знают о том, как в этом
продукте устроены учётки: кто вошёл — решают маршруты в `app/api/routes/books.py`,
там же, где сходятся модули. Иначе общий модуль знал бы про конкретную компанию.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.books import roles as role_catalog
from app.books.db import books_session
from app.books.discover import discover_fields
from app.books.ingest import Plan, SourceRow, meaningful_rows, merge, propose_identity
from app.books.models import (
    POSITION_STEP,
    Binding,
    Book,
    BookField,
    BookRow,
    BookTable,
    ImportIssue,
    ImportRun,
    RoleCatalog,
    RoleSynonym,
    RowEdit,
    RowFact,
    Workspace,
)
from app.books.sources.base import Source, SourceError
from app.books.suggest import FieldView, propose
from app.books.types import project
from app.core.scalars import clean

log = logging.getLogger(__name__)

DEFAULT_WORKSPACE_SLUG = "default"


class BooksError(RuntimeError):
    """Ошибка, текст которой предназначен человеку, а не логам."""


# ── Рабочее пространство и каталог ───────────────────────────────────────────


def ensure_workspace(session: Session, slug: str = DEFAULT_WORKSPACE_SLUG) -> Workspace:
    workspace = session.scalar(select(Workspace).where(Workspace.slug == slug))
    if workspace is None:
        workspace = Workspace(slug=slug, title="Основное пространство")
        session.add(workspace)
        session.flush()
    return workspace


def seed_catalog(session: Session) -> int:
    """Записать каталог ролей и поставляемые синонимы в базу.

    Идемпотентно: роли обновляются, синонимы добавляются недостающие. Выученные
    синонимы (`source="learned"`) не трогаются — они принадлежат компании, а не
    поставке.
    """
    existing = {row.key: row for row in session.scalars(select(RoleCatalog))}
    for position, item in enumerate(role_catalog.all_roles()):
        row = existing.get(item.key)
        if row is None:
            session.add(
                RoleCatalog(
                    key=item.key,
                    title=item.title,
                    value_type=item.value_type,
                    description=item.description,
                    position=position,
                )
            )
        else:
            row.title = item.title
            row.value_type = item.value_type
            row.description = item.description
            row.position = position
    session.flush()

    known = {
        (row.role_key, row.normalized)
        for row in session.scalars(
            select(RoleSynonym).where(RoleSynonym.workspace_id.is_(None))
        )
    }
    added = 0
    from app.books.layout import norm

    for item in role_catalog.all_roles():
        for spelling in item.synonyms:
            key = (item.key, norm(spelling))
            if key in known:
                continue
            session.add(
                RoleSynonym(
                    role_key=item.key,
                    workspace_id=None,
                    text=spelling,
                    normalized=norm(spelling),
                    source="seed",
                )
            )
            known.add(key)
            added += 1
    session.flush()
    return added


def learned_synonyms(session: Session, workspace_id: UUID) -> dict[str, list[str]]:
    """Написания, подтверждённые человеком в этой компании.

    Именно из них каталог со временем умнеет: следующая книга, где колонку
    назвали так же, привяжется без вопросов.
    """
    rows = session.scalars(
        select(RoleSynonym).where(
            RoleSynonym.workspace_id == workspace_id,
            RoleSynonym.source == "learned",
        )
    )
    result: dict[str, list[str]] = {}
    for row in rows:
        result.setdefault(row.role_key, []).append(row.text)
    return result


# ── Книги и вкладки ──────────────────────────────────────────────────────────


def list_books(session: Session, workspace_id: UUID) -> list[dict[str, Any]]:
    books = session.scalars(
        select(Book)
        .where(Book.workspace_id == workspace_id, Book.deleted_at.is_(None))
        .order_by(Book.title)
    ).all()
    # Вкладки приходят вместе с книгами, а не отдельным запросом на каждую:
    # книг у компании десятки, вкладок в книге единицы, и N+1 обращений к базе
    # ради списка из трёх строк — плохой обмен.
    tables = session.scalars(
        select(BookTable)
        .where(BookTable.workspace_id == workspace_id, BookTable.deleted_at.is_(None))
        .order_by(BookTable.position, BookTable.name)
    ).all()
    by_book: dict[Any, list[dict[str, Any]]] = {}
    for table in tables:
        by_book.setdefault(table.book_id, []).append(
            {"id": str(table.id), "name": table.name}
        )

    return [
        {
            "id": str(book.id),
            "title": book.title,
            "source_kind": book.source_kind,
            "source_ref": book.source_ref,
            "imported_at": book.imported_at.isoformat() if book.imported_at else None,
            "tables": by_book.get(book.id, []),
        }
        for book in books
    ]


def get_or_create_book(
    session: Session, workspace_id: UUID, *, title: str, source_ref: str
) -> Book:
    book = session.scalar(
        select(Book).where(
            Book.workspace_id == workspace_id,
            Book.source_ref == source_ref,
            Book.deleted_at.is_(None),
        )
    )
    if book is None:
        book = Book(
            workspace_id=workspace_id,
            title=title,
            source_kind="google_sheets",
            source_ref=source_ref,
        )
        session.add(book)
        session.flush()
    else:
        book.title = title
    return book


def get_or_create_table(
    session: Session, book: Book, *, name: str, source_ref: str
) -> BookTable:
    table = session.scalar(
        select(BookTable).where(
            BookTable.book_id == book.id,
            BookTable.name == name,
            BookTable.deleted_at.is_(None),
        )
    )
    if table is None:
        table = BookTable(
            workspace_id=book.workspace_id,
            book_id=book.id,
            name=name,
            source_ref=source_ref,
        )
        session.add(table)
        session.flush()
    return table


# ── Поля ─────────────────────────────────────────────────────────────────────


def sync_fields(
    session: Session, table: BookTable, grid: Sequence[Sequence[str]]
) -> tuple[list[BookField], int]:
    """Обновить описания колонок по прочитанному гриду.

    Поле, пропавшее из книги, не удаляется — помечается `deleted_at`. В нём
    лежат значения строк, и удалять их вместе с колонкой значило бы терять
    данные из-за того, что кто-то временно спрятал столбец.
    """
    discovered, header_row = discover_fields(grid)
    table.header_row = header_row

    existing = {
        row.key: row
        for row in session.scalars(
            select(BookField).where(BookField.table_id == table.id)
        )
    }
    seen: set[str] = set()
    result: list[BookField] = []

    for item in discovered:
        seen.add(item.key)
        row = existing.get(item.key)
        if row is None:
            row = BookField(
                workspace_id=table.workspace_id,
                table_id=table.id,
                key=item.key,
                title=item.title,
                type=item.type,
                position=item.position,
                names=list(item.names),
                stats=item.stats,
            )
            session.add(row)
        else:
            row.title = item.title or row.title
            row.type = item.type
            row.position = item.position
            row.stats = item.stats
            row.deleted_at = None
            # Прежние написания заголовка не теряем: книгу переименовывают, и
            # повторный импорт обязан узнать колонку по любому из них.
            names = list(dict.fromkeys([*row.names, *item.names]))
            row.names = names
        result.append(row)

    for key, row in existing.items():
        if key not in seen and row.deleted_at is None:
            row.deleted_at = datetime.now(UTC)

    session.flush()
    return result, header_row


def field_views(fields: Sequence[BookField]) -> list[FieldView]:
    return [
        FieldView(key=f.key, title=f.title, type=f.type, names=tuple(f.names or ()))
        for f in fields
        if f.deleted_at is None
    ]


# ── Привязки ─────────────────────────────────────────────────────────────────


def bindings_of(session: Session, table_id: UUID) -> dict[str, str]:
    """{ключ поля: ключ роли} — все привязки, и подтверждённые, и предложенные."""
    return {key: item[0] for key, item in _bindings_detailed(session, table_id).items()}


def _bindings_detailed(
    session: Session, table_id: UUID
) -> dict[str, tuple[str, bool]]:
    """{ключ поля: (роль, подтверждена ли человеком)}.

    Различие существенно и видно на табло. Предложение сохраняется сразу, чтобы
    дашборд считал с первого импорта, — но выдавать догадку за решение человека
    нельзя: он должен понимать, что именно ещё не смотрел.
    """
    rows = session.scalars(select(Binding).where(Binding.table_id == table_id)).all()
    fields = {
        row.id: row.key
        for row in session.scalars(
            select(BookField).where(BookField.table_id == table_id)
        )
    }
    return {
        fields[row.field_id]: (row.role_key, row.confirmed_at is not None)
        for row in rows
        if row.field_id in fields
    }


def suggest_for_table(
    session: Session, table: BookTable
) -> tuple[list[BookField], Any]:
    fields = session.scalars(
        select(BookField)
        .where(BookField.table_id == table.id, BookField.deleted_at.is_(None))
        .order_by(BookField.position)
    ).all()
    proposal = propose(
        field_views(fields),
        learned=learned_synonyms(session, table.workspace_id),
    )
    return list(fields), proposal


def set_binding(
    session: Session,
    table: BookTable,
    *,
    field_key: str,
    role_key: str | None,
    actor: str = "",
) -> None:
    """Привязать поле к роли или снять привязку.

    Подтверждённая человеком привязка запоминается как выученный синоним —
    отсюда каталог и умнеет. Записывается заголовок поля, а не его ключ:
    следующая книга придёт со своими ключами, а заголовок у неё будет тот же.
    """
    field = session.scalar(
        select(BookField).where(
            BookField.table_id == table.id, BookField.key == field_key
        )
    )
    if field is None:
        raise BooksError(f"Поля «{field_key}» в этой вкладке нет")

    session.execute(
        delete(Binding).where(
            Binding.table_id == table.id, Binding.field_id == field.id
        )
    )
    if role_key is None:
        session.flush()
        return

    if role_catalog.role(role_key) is None:
        raise BooksError(f"Роли «{role_key}» нет в каталоге")

    # Роль занимает ровно одно поле: иначе «сумма договора» приезжала бы сразу
    # из двух колонок, и какая победит — зависело бы от порядка строк.
    session.execute(
        delete(Binding).where(
            Binding.table_id == table.id, Binding.role_key == role_key
        )
    )
    session.add(
        Binding(
            workspace_id=table.workspace_id,
            table_id=table.id,
            field_id=field.id,
            role_key=role_key,
            confidence="manual",
            confirmed_at=datetime.now(UTC),
            confirmed_by=actor,
        )
    )
    _remember_synonym(session, table.workspace_id, role_key, field.title)
    session.flush()


def _remember_synonym(
    session: Session, workspace_id: UUID, role_key: str, spelling: str
) -> None:
    from app.books.layout import norm

    text = clean(spelling)
    if not text:
        return
    normalized = norm(text)
    exists = session.scalar(
        select(RoleSynonym).where(
            RoleSynonym.role_key == role_key,
            RoleSynonym.normalized == normalized,
            RoleSynonym.workspace_id.in_([workspace_id, None]),
        )
    )
    if exists is not None:
        return
    session.add(
        RoleSynonym(
            role_key=role_key,
            workspace_id=workspace_id,
            text=text,
            normalized=normalized,
            source="learned",
        )
    )


def persist_suggestions(session: Session, table: BookTable) -> int:
    """Записать предложенные привязки как неподтверждённые.

    Зачем вообще их писать
    ──────────────────────
    Проекция `row_facts` строится по привязкам из базы. Если предложения там не
    сохранять, то после первого импорта фактов ноль: строки есть, а дашборду
    считать не по чему — пока человек не подтвердит все сорок колонок руками.
    Ровно это и вышло на первом живом прогоне.

    Поэтому предложение сохраняется сразу и работает сразу. От подтверждённого
    оно отличается пустым `confirmed_at`: на табло видно, что это догадка, и
    человек её меняет одним движением. Но пустой дашборд ему для этого больше
    не показывают.

    Уже существующие привязки не трогаются: решение человека сильнее догадки.
    """
    saved = bindings_of(session, table.id)
    if saved:
        return 0

    fields, proposal = suggest_for_table(session, table)
    by_key = {field.key: field for field in fields}
    written = 0
    for item in proposal.suggestions:
        field = by_key.get(item.field_key)
        if field is None:
            continue
        session.add(
            Binding(
                workspace_id=table.workspace_id,
                table_id=table.id,
                field_id=field.id,
                role_key=item.role_key,
                confidence=item.confidence,
            )
        )
        written += 1
    session.flush()
    return written


def board(session: Session, table: BookTable) -> dict[str, Any]:
    """Табло привязок: что нашлось, что нужно, и что из этого следует."""
    fields, proposal = suggest_for_table(session, table)
    detailed = _bindings_detailed(session, table.id)
    saved = {key: role for key, (role, _) in detailed.items()}
    confirmed = {key for key, (_, ok) in detailed.items() if ok}
    suggested = {item.field_key: item for item in proposal.suggestions}

    # Сохранённая привязка сильнее предложенной: человек уже решил.
    effective = dict(saved)
    for field_key, item in suggested.items():
        if field_key not in effective and item.role_key not in effective.values():
            effective[field_key] = item.role_key

    catalog = {item.key: item for item in role_catalog.all_roles()}
    return {
        "table": {
            "id": str(table.id),
            "name": table.name,
            "header_row": table.header_row,
        },
        "fields": [
            {
                "key": f.key,
                "title": f.title,
                "type": f.type,
                "position": f.position,
                "stats": f.stats or {},
                "role": effective.get(f.key),
                "confirmed": f.key in confirmed,
                "suggestion": (
                    {
                        "role": suggested[f.key].role_key,
                        "confidence": suggested[f.key].confidence,
                        "reason": suggested[f.key].reason,
                    }
                    if f.key in suggested
                    else None
                ),
            }
            for f in fields
        ],
        "roles": [
            {
                "key": item.key,
                "title": item.title,
                "value_type": item.value_type,
                "description": item.description,
                "bound_to": next(
                    (fk for fk, rk in effective.items() if rk == item.key), None
                ),
            }
            for item in role_catalog.all_roles()
        ],
        "sections": [
            status.to_dict()
            for status in role_catalog.section_status(set(effective.values()))
        ],
        "refusals": [
            {
                "kind": item.kind,
                "role": item.role_key,
                "fields": list(item.field_keys),
                "reason": item.reason,
            }
            for item in proposal.refusals
        ],
        "unbound": [f.key for f in fields if f.key not in effective],
        "catalog_size": len(catalog),
    }


# ── Импорт ───────────────────────────────────────────────────────────────────


@dataclass
class ImportPreview:
    run_id: str
    plan: Plan
    table_id: str
    book_id: str


def _source_rows(
    grid: Sequence[Sequence[str]],
    fields: Sequence[BookField],
    header_row: int,
) -> list[SourceRow]:
    from app.books.types import coerce_for_storage

    rows: list[SourceRow] = []
    for offset, raw in enumerate(grid[header_row + 1:], start=header_row + 2):
        values: dict[str, Any] = {}
        for field in fields:
            if field.deleted_at is not None:
                continue
            cell = raw[field.position] if field.position < len(raw) else ""
            stored = coerce_for_storage(field.type, cell)
            if stored is not None:
                values[field.key] = stored
        rows.append(SourceRow(position=offset, values=values))
    return rows


def _mirror_rows(session: Session, table_id: UUID) -> tuple[list[Any], list[BookRow]]:
    from app.books.ingest import MirrorRow

    rows = session.scalars(
        select(BookRow)
        .where(BookRow.table_id == table_id, BookRow.deleted_at.is_(None))
        .order_by(BookRow.position)
    ).all()
    return [
        MirrorRow(
            id=str(row.id),
            values=dict(row.values or {}),
            base=dict(row.base or {}),
            source_key=row.source_key,
            origin=row.origin,
            state=row.state,
        )
        for row in rows
    ], rows


def preview_import(
    session: Session,
    source: Source,
    workspace_id: UUID,
    *,
    spreadsheet_id: str,
    tab_title: str,
    actor: str = "",
) -> ImportPreview:
    """Прочитать книгу и посчитать, что изменится. Ничего не применяет."""
    try:
        grid = source.read_tab(spreadsheet_id, tab_title)
    except SourceError as exc:
        raise BooksError(str(exc)) from exc

    book = get_or_create_book(
        session, workspace_id, title=grid.book_title, source_ref=spreadsheet_id
    )
    table = get_or_create_table(
        session, book, name=grid.tab.title, source_ref=grid.tab.id
    )
    fields, header_row = sync_fields(session, table, grid.values)

    # Первый импорт: привязок ещё нет — сохраняем предложенные как
    # неподтверждённые. Без этого проекция осталась бы пустой до тех пор, пока
    # человек не разметит все колонки руками.
    persist_suggestions(session, table)
    bound = bindings_of(session, table.id)

    substantive = set(role_catalog.substantive_roles())
    substantive_fields = [fk for fk, rk in bound.items() if rk in substantive]

    all_rows = _source_rows(grid.values, fields, header_row)
    rows = meaningful_rows(all_rows, substantive_fields, list(bound))

    mirror, _ = _mirror_rows(session, table.id)
    field_keys = tuple(f.key for f in fields if f.deleted_at is None)
    identity = tuple(table.identity_fields or ())
    if not identity:
        identity = propose_identity([row.values for row in rows], substantive_fields)

    plan = merge(rows, mirror, identity_fields=identity, fields=field_keys)

    run = ImportRun(
        workspace_id=workspace_id,
        book_id=book.id,
        status="blocked" if plan.blocked else "preview",
        started_by=actor,
        summary={
            **plan.summary(),
            "alignment": plan.alignment,
            "table": str(table.id),
            "tab": tab_title,
            "truncated": grid.truncated,
        },
        blocked_reason=plan.blocked_reason,
    )
    session.add(run)
    session.flush()

    for issue in plan.issues[:500]:
        session.add(
            ImportIssue(
                import_run_id=run.id,
                kind=issue.kind,
                field_key=str(issue.detail.get("field", "")),
                detail=issue.detail,
            )
        )
    session.flush()

    return ImportPreview(
        run_id=str(run.id), plan=plan, table_id=str(table.id), book_id=str(book.id)
    )


def apply_import(
    session: Session,
    workspace_id: UUID,
    *,
    run_id: UUID,
    plan: Plan,
    table_id: UUID,
    actor: str = "",
) -> dict[str, int]:
    """Применить посчитанный план. Вызывается только после предпросмотра."""
    if plan.blocked:
        raise BooksError(plan.blocked_reason)

    existing = {
        str(row.id): row
        for row in session.scalars(
            select(BookRow).where(BookRow.table_id == table_id)
        )
    }
    table = session.get(BookTable, table_id)
    if table is None:
        raise BooksError("Вкладка не найдена")

    last_position = session.scalar(
        select(func.max(BookRow.position)).where(BookRow.table_id == table_id)
    ) or 0

    counts = {"created": 0, "updated": 0, "missing": 0}
    for row_plan in plan.rows:
        if row_plan.op == "create":
            last_position += POSITION_STEP
            session.add(
                BookRow(
                    workspace_id=workspace_id,
                    table_id=table_id,
                    position=last_position,
                    values=dict(row_plan.values),
                    base=dict(row_plan.base_updates),
                    source_key=row_plan.key,
                    origin="source",
                    updated_by=actor,
                )
            )
            counts["created"] += 1
            continue

        row = existing.get(str(row_plan.row_id or ""))
        if row is None:
            continue

        if row_plan.op == "missing":
            row.state = "missing_in_source"
            counts["missing"] += 1
            continue

        if row_plan.accepted or row_plan.base_updates:
            changes = {
                key: [row.values.get(key), value]
                for key, value in row_plan.accepted.items()
            }
            row.values = {**(row.values or {}), **row_plan.accepted}
            row.base = {**(row.base or {}), **row_plan.base_updates}
            row.version += 1
            row.updated_by = actor
            row.updated_at = datetime.now(UTC)
            if changes:
                session.add(
                    RowEdit(
                        workspace_id=workspace_id,
                        table_id=table_id,
                        row_id=row.id,
                        actor=actor,
                        source="import",
                        changes=changes,
                    )
                )
                counts["updated"] += 1

    run = session.get(ImportRun, run_id)
    if run is not None:
        run.status = "applied"
        run.finished_at = datetime.now(UTC)
        run.summary = {**(run.summary or {}), "applied": counts}

    book = session.get(Book, table.book_id)
    if book is not None:
        book.imported_at = datetime.now(UTC)

    session.flush()
    rebuild_facts(session, table_id)
    return counts


# ── Производная проекция ─────────────────────────────────────────────────────


def rebuild_facts(session: Session, table_id: UUID) -> int:
    """Пересобрать `row_facts` из строк и привязок.

    Производная таблица: её можно уронить и собрать заново. Если пересборка
    даёт другой результат — значит в ней появилось что-то, чего в правде нет,
    и это уже не производное.
    """
    table = session.get(BookTable, table_id)
    if table is None:
        return 0

    fields = {
        row.id: row
        for row in session.scalars(
            select(BookField).where(BookField.table_id == table_id)
        )
    }
    bindings = session.scalars(
        select(Binding).where(Binding.table_id == table_id)
    ).all()
    by_field_key = {
        fields[b.field_id].key: b.role_key for b in bindings if b.field_id in fields
    }
    types = {f.key: f.type for f in fields.values()}

    session.execute(delete(RowFact).where(RowFact.table_id == table_id))

    rows = session.scalars(
        select(BookRow).where(
            BookRow.table_id == table_id, BookRow.deleted_at.is_(None)
        )
    ).all()

    written = 0
    for row in rows:
        values = row.values or {}
        for field_key, role_key in by_field_key.items():
            fact = project(types.get(field_key, "text"), values.get(field_key))
            if fact.empty:
                continue
            session.add(
                RowFact(
                    row_id=row.id,
                    role_key=role_key,
                    workspace_id=row.workspace_id,
                    table_id=table_id,
                    num_value=fact.num_value,
                    date_value=fact.date_value,
                    text_value=fact.text_value,
                    bool_value=fact.bool_value,
                )
            )
            written += 1
    session.flush()
    return written


# ── Строки ───────────────────────────────────────────────────────────────────


def list_rows(
    session: Session,
    table_id: UUID,
    *,
    limit: int = 100,
    offset: int = 0,
    newest_first: bool = False,
) -> dict[str, Any]:
    """Страница строк вкладки.

    `newest_first` — порядок для ввода, а не для чтения. Новая строка встаёт в
    конец книги: в журнале это позиция 3633 из 3632, и человек, добавивший
    запись через форму, её просто не видел — показывались первые двести.
    Читать книгу удобнее в её собственном порядке, вводить — начиная с того,
    что ввели последним.
    """
    total = session.scalar(
        select(func.count(BookRow.id)).where(
            BookRow.table_id == table_id, BookRow.deleted_at.is_(None)
        )
    )
    order = (
        (BookRow.updated_at.desc(), BookRow.position.desc())
        if newest_first
        else (BookRow.position,)
    )
    rows = session.scalars(
        select(BookRow)
        .where(BookRow.table_id == table_id, BookRow.deleted_at.is_(None))
        .order_by(*order)
        .limit(limit)
        .offset(offset)
    ).all()
    return {
        "total": total or 0,
        "rows": [
            {
                "id": str(row.id),
                "values": row.values or {},
                "origin": row.origin,
                "state": row.state,
                "version": row.version,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ],
    }


def save_row(
    session: Session,
    table: BookTable,
    *,
    row_id: UUID | None,
    values: dict[str, Any],
    version: int | None = None,
    actor: str = "",
) -> BookRow:
    """Создать или поправить строку.

    `version` — оптимистичная блокировка: сохранение с устаревшей версией
    отклоняется, а не затирает чужую правку. Грид и форма правят одни и те же
    строки, и без неё тот, кто нажал «сохранить» вторым, молча выигрывал бы.
    """
    if row_id is None:
        last = session.scalar(
            select(func.max(BookRow.position)).where(BookRow.table_id == table.id)
        ) or 0
        row = BookRow(
            workspace_id=table.workspace_id,
            table_id=table.id,
            position=last + POSITION_STEP,
            values=dict(values),
            base={},
            origin="app",
            updated_by=actor,
        )
        session.add(row)
        session.flush()
        session.add(
            RowEdit(
                workspace_id=table.workspace_id,
                table_id=table.id,
                row_id=row.id,
                actor=actor,
                source="app",
                changes={key: [None, value] for key, value in values.items()},
            )
        )
        session.flush()
        return row

    row = session.get(BookRow, row_id)
    if row is None or row.table_id != table.id:
        raise BooksError("Строка не найдена")
    if version is not None and row.version != version:
        raise BooksError(
            "Строку уже поправили — обновите страницу и посмотрите, что изменилось"
        )

    before = dict(row.values or {})
    changes = {
        key: [before.get(key), value]
        for key, value in values.items()
        if before.get(key) != value
    }
    if not changes:
        return row

    row.values = {**before, **values}
    row.version += 1
    row.updated_by = actor
    row.updated_at = datetime.now(UTC)
    session.add(
        RowEdit(
            workspace_id=table.workspace_id,
            table_id=table.id,
            row_id=row.id,
            actor=actor,
            source="app",
            changes=changes,
        )
    )
    session.flush()
    return row


__all__ = [
    "BooksError",
    "ImportPreview",
    "apply_import",
    "board",
    "bindings_of",
    "books_session",
    "ensure_workspace",
    "learned_synonyms",
    "list_books",
    "list_rows",
    "persist_suggestions",
    "preview_import",
    "rebuild_facts",
    "save_row",
    "seed_catalog",
    "set_binding",
    "suggest_for_table",
    "sync_fields",
]
