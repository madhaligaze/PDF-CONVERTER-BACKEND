"""HTTP-маршруты раздела «Книги».

Почему они здесь, а не внутри `app/books/`
──────────────────────────────────────────
Модуль «Книги» задуман общим: та же машинерия должна обслуживать другую
компанию. Поэтому он ничего не знает о том, как в этом продукте устроены
учётки, — а маршрутам знать надо, иначе к финансовым книгам получит доступ кто
угодно.

Место, где сходятся модули, называется корнем композиции, и лежит оно снаружи
обоих. Здесь и только здесь встречаются `app.books.service` и `app.bbc.deps`;
тест `test_books_isolated` следит, чтобы внутрь пакета эта связь не протекла.

Обработчики объявлены обычным `def`, а не `async def`, и это не небрежность:
внутри синхронные gspread и SQLAlchemy, а книга читается секундами. В
`async def` это встало бы колом в цикле событий и подвесило заодно дашборд;
обычный `def` FastAPI уводит в пул потоков.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.bbc.deps import require_user
from app.books import roles as role_catalog, service
from app.books.config import books_settings
from app.books.db import books_session
from app.books.models import BookTable
from app.books.sources.base import SourceError
from app.books.sources.gsheets import GoogleSheetsSource, invalidate_cache

log = logging.getLogger(__name__)

router = APIRouter(prefix="/books", tags=["books"])


def _guard() -> None:
    if not books_settings.enabled:
        raise HTTPException(status_code=404, detail="Раздел «Книги» выключен")
    if not books_settings.credentials_available:
        raise HTTPException(
            status_code=503,
            detail="Не настроены креды Google — импорт из Sheets недоступен",
        )


def _actor(user: Any) -> str:
    return getattr(user, "username", "") or ""


def _table(session, table_id: UUID) -> BookTable:
    table = session.get(BookTable, table_id)
    if table is None or table.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Вкладка не найдена")
    return table


# ── Источники в Google ───────────────────────────────────────────────────────


@router.get("/sources")
def list_sources(user=Depends(require_user)) -> dict[str, Any]:
    """Книги Google, доступные сервисному аккаунту."""
    _guard()
    try:
        books = GoogleSheetsSource().list_books()
    except SourceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"books": [{"id": b.id, "title": b.title} for b in books]}


@router.get("/sources/{spreadsheet_id}")
def source_tabs(spreadsheet_id: str, user=Depends(require_user)) -> dict[str, Any]:
    _guard()
    try:
        title, tabs = GoogleSheetsSource().list_tabs(spreadsheet_id)
    except SourceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "id": spreadsheet_id,
        "title": title,
        "tabs": [
            {"id": t.id, "title": t.title, "rows": t.rows, "cols": t.cols}
            for t in tabs
            if not t.hidden
        ],
    }


@router.post("/sources/refresh")
def refresh_sources(user=Depends(require_user)) -> dict[str, bool]:
    _guard()
    invalidate_cache()
    return {"ok": True}


# ── Внутренние книги ─────────────────────────────────────────────────────────


@router.get("")
def list_books(user=Depends(require_user)) -> dict[str, Any]:
    with books_session() as session:
        workspace = service.ensure_workspace(session)
        return {"books": service.list_books(session, workspace.id)}


@router.get("/tables/{table_id}")
def get_table(
    table_id: UUID,
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    # «recent» — порядок для ввода: свежее сверху. Без него запись, добавленная
    # через форму, оказывалась в конце книги и человеку не показывалась.
    order: str = Query(default="position", pattern="^(position|recent)$"),
    user=Depends(require_user),
) -> dict[str, Any]:
    with books_session() as session:
        table = _table(session, table_id)
        fields, _ = service.suggest_for_table(session, table)
        page = service.list_rows(
            session, table_id, limit=limit, offset=offset,
            newest_first=order == "recent",
        )
        return {
            "table": {
                "id": str(table.id),
                "name": table.name,
                "book_id": str(table.book_id),
                "header_row": table.header_row,
            },
            "fields": [
                {"key": f.key, "title": f.title, "type": f.type, "position": f.position}
                for f in fields
            ],
            "bindings": service.bindings_of(session, table_id),
            # Подписи ролей по-русски. Без них в шапку грида уезжали ключи
            # вида `entry_date` — английское слово посреди русских названий.
            # В этом продукте так уже случалось, и заметил это не разработчик.
            "role_titles": {
                item.key: item.title for item in role_catalog.all_roles()
            },
            **page,
        }


# ── Табло привязок ───────────────────────────────────────────────────────────


@router.get("/tables/{table_id}/board")
def get_board(table_id: UUID, user=Depends(require_user)) -> dict[str, Any]:
    """Что нашлось в книге, что нужно приложению и что из этого следует."""
    with books_session() as session:
        table = _table(session, table_id)
        return service.board(session, table)


class BindingRequest(BaseModel):
    field_key: str = Field(min_length=1, max_length=200)
    #: `null` снимает привязку.
    role_key: str | None = None


@router.put("/tables/{table_id}/bindings")
def put_binding(
    table_id: UUID, request: BindingRequest, user=Depends(require_user)
) -> dict[str, Any]:
    with books_session() as session:
        table = _table(session, table_id)
        try:
            service.set_binding(
                session,
                table,
                field_key=request.field_key,
                role_key=request.role_key,
                actor=_actor(user),
            )
        except service.BooksError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        service.rebuild_facts(session, table_id)
        return service.board(session, table)


# ── Импорт ───────────────────────────────────────────────────────────────────


class ImportRequest(BaseModel):
    spreadsheet_id: str = Field(min_length=10, max_length=120)
    tab: str = Field(min_length=1, max_length=200)


def _preview_payload(preview: service.ImportPreview) -> dict[str, Any]:
    plan = preview.plan
    return {
        "run_id": preview.run_id,
        "table_id": preview.table_id,
        "book_id": preview.book_id,
        "summary": plan.summary(),
        "alignment": plan.alignment,
        "describe": plan.describe(),
        "blocked": plan.blocked,
        "blocked_reason": plan.blocked_reason,
        "issues": [
            {"kind": i.kind, "key": i.key, "detail": i.detail} for i in plan.issues[:200]
        ],
    }


@router.post("/import/preview")
def preview(request: ImportRequest, user=Depends(require_user)) -> dict[str, Any]:
    """Прочитать книгу и показать, что изменится. Ничего не применяет."""
    _guard()
    with books_session() as session:
        service.ensure_workspace(session)
        service.seed_catalog(session)
        workspace = service.ensure_workspace(session)
        try:
            result = service.preview_import(
                session,
                GoogleSheetsSource(),
                workspace.id,
                spreadsheet_id=request.spreadsheet_id,
                tab_title=request.tab,
                actor=_actor(user),
            )
        except service.BooksError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return _preview_payload(result)


class ApplyRequest(ImportRequest):
    run_id: UUID


@router.post("/import/apply")
def apply(request: ApplyRequest, user=Depends(require_user)) -> dict[str, Any]:
    """Применить то, что человек увидел в предпросмотре.

    План не хранится, а пересчитывается: книга живая, и между предпросмотром и
    подтверждением её могли поправить. Пересчитанный план сверяется с тем, что
    показывали; разошлись — применять нельзя, потому что человек соглашался не
    на это. Просим посмотреть заново.
    """
    _guard()
    with books_session() as session:
        workspace = service.ensure_workspace(session)
        try:
            fresh = service.preview_import(
                session,
                GoogleSheetsSource(),
                workspace.id,
                spreadsheet_id=request.spreadsheet_id,
                tab_title=request.tab,
                actor=_actor(user),
            )
        except service.BooksError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        from app.books.models import ImportRun

        approved = session.get(ImportRun, request.run_id)
        if approved is None:
            raise HTTPException(status_code=404, detail="Предпросмотр не найден")

        was = {k: v for k, v in (approved.summary or {}).items() if k != "alignment"}
        now = {**fresh.plan.summary(), "table": fresh.table_id,
               "tab": request.tab, "truncated": was.get("truncated")}
        if was.get("create") != now.get("create") or was.get("update") != now.get("update"):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Книгу изменили, пока вы смотрели предпросмотр. "
                    "Посмотрите заново — применять то, чего вы не видели, нельзя."
                ),
            )

        try:
            counts = service.apply_import(
                session,
                workspace.id,
                run_id=UUID(fresh.run_id),
                plan=fresh.plan,
                table_id=UUID(fresh.table_id),
                actor=_actor(user),
            )
        except service.BooksError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"applied": counts, "table_id": fresh.table_id}


# ── Строки ───────────────────────────────────────────────────────────────────


class RowRequest(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    version: int | None = None


@router.post("/tables/{table_id}/rows")
def create_row(
    table_id: UUID, request: RowRequest, user=Depends(require_user)
) -> dict[str, Any]:
    with books_session() as session:
        table = _table(session, table_id)
        row = service.save_row(
            session, table, row_id=None, values=request.values, actor=_actor(user)
        )
        service.rebuild_facts(session, table_id)
        return {"id": str(row.id), "version": row.version}


@router.patch("/tables/{table_id}/rows/{row_id}")
def update_row(
    table_id: UUID, row_id: UUID, request: RowRequest, user=Depends(require_user)
) -> dict[str, Any]:
    with books_session() as session:
        table = _table(session, table_id)
        try:
            row = service.save_row(
                session,
                table,
                row_id=row_id,
                values=request.values,
                version=request.version,
                actor=_actor(user),
            )
        except service.BooksError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        service.rebuild_facts(session, table_id)
        return {"id": str(row.id), "version": row.version}


__all__ = ["router"]
