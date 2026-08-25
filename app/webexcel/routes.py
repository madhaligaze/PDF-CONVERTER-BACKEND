"""HTTP-маршруты Web-Excel.

Все обработчики объявлены обычным `def`, а не `async def`, и это не небрежность.
Внутри — синхронный gspread, который на большой вкладке думает восемь секунд.
В `async def` эти восемь секунд встали бы колом в цикле событий и подвесили бы
заодно дашборд и анализатор; обычный `def` FastAPI уводит в пул потоков.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.webexcel.config import webexcel_settings
from app.webexcel.google import (
    WebExcelError,
    fetch_tab_grid,
    invalidate_cache,
    list_spreadsheets,
    spreadsheet_meta,
)
from app.webexcel.univer import build_workbook, convert_tab

log = logging.getLogger(__name__)

router = APIRouter(prefix="/web-excel", tags=["web-excel"])


def _guard() -> None:
    if not webexcel_settings.enabled:
        raise HTTPException(status_code=404, detail="Раздел «Таблицы» выключен")
    if not webexcel_settings.credentials_available:
        raise HTTPException(
            status_code=503,
            detail="Не настроены креды Google — импорт из Sheets недоступен",
        )


# ── Источники в Google ──────────────────────────────────────────────────────


@router.get("/sources")
def get_sources() -> dict[str, Any]:
    """Все книги Google, открытые сервисному аккаунту."""
    _guard()
    try:
        return {"books": list_spreadsheets()}
    except WebExcelError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/sources/{spreadsheet_id}")
def get_source_meta(spreadsheet_id: str) -> dict[str, Any]:
    """Название книги и её вкладки — без грида, один дешёвый запрос."""
    _guard()
    try:
        return spreadsheet_meta(spreadsheet_id)
    except WebExcelError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/sources/{spreadsheet_id}/tab")
def get_source_tab(spreadsheet_id: str, title: str = Query(...)) -> dict[str, Any]:
    """Одна вкладка как лист Univer — со всем оформлением.

    Вкладка отдаётся по одной намеренно. Ответ Google с оформлением для «Журнала»
    весит 46 МБ на вкладку; тянуть восемь вкладок разом означало бы держать
    треть гигабайта в памяти контейнера ради одного открытия книги.
    """
    _guard()
    try:
        raw = fetch_tab_grid(spreadsheet_id, title)
        converted = convert_tab(raw)
    except WebExcelError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "spreadsheet_id": spreadsheet_id,
        "spreadsheet_title": raw["spreadsheet_title"],
        "sheet": converted["sheet"],
        "styles": converted["styles"],
        "stats": converted["stats"],
        "fonts": converted["fonts"],
        "checkboxes": converted["checkboxes"],
    }


@router.post("/sources/refresh")
def refresh_sources() -> dict[str, Any]:
    """Сбросить кэш — по кнопке «Обновить из Google»."""
    _guard()
    invalidate_cache()
    return {"ok": True}


# ── Книги приложения ────────────────────────────────────────────────────────


class SaveBookRequest(BaseModel):
    name: str = Field(default="Без названия", max_length=200)
    kind: str = Field(default="blank", max_length=16)
    origin_spreadsheet_id: str = Field(default="", max_length=64)
    origin_title: str = Field(default="", max_length=200)
    origin_tabs: list[str] = Field(default_factory=list)
    snapshot: dict[str, Any] = Field(default_factory=dict)
    note: str = ""


def _serialize(book: Any, with_snapshot: bool = False) -> dict[str, Any]:
    payload = {
        "id": book.id,
        "name": book.name,
        "kind": book.kind,
        "origin_spreadsheet_id": book.origin_spreadsheet_id,
        "origin_title": book.origin_title,
        "origin_tabs": book.origin_tabs or [],
        "note": book.note,
        "created_at": book.created_at.isoformat() if book.created_at else None,
        "updated_at": book.updated_at.isoformat() if book.updated_at else None,
    }
    if with_snapshot:
        payload["snapshot"] = book.snapshot or {}
    return payload


@router.get("/books")
def list_books() -> dict[str, Any]:
    from sqlalchemy import select

    from app.webexcel.db import webexcel_session
    from app.webexcel.models import WebExcelBook

    with webexcel_session() as session:
        rows = session.scalars(
            select(WebExcelBook).order_by(WebExcelBook.updated_at.desc())
        ).all()
        return {"books": [_serialize(row) for row in rows]}


@router.get("/books/{book_id}")
def get_book(book_id: int) -> dict[str, Any]:
    from app.webexcel.db import webexcel_session
    from app.webexcel.models import WebExcelBook

    with webexcel_session() as session:
        book = session.get(WebExcelBook, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="Книга не найдена")
        return _serialize(book, with_snapshot=True)


@router.post("/books")
def create_book(request: SaveBookRequest) -> dict[str, Any]:
    from app.webexcel.db import webexcel_session
    from app.webexcel.models import WebExcelBook

    with webexcel_session() as session:
        book = WebExcelBook(
            name=request.name.strip() or "Без названия",
            kind=request.kind,
            origin_spreadsheet_id=request.origin_spreadsheet_id,
            origin_title=request.origin_title,
            origin_tabs=request.origin_tabs,
            snapshot=request.snapshot,
            note=request.note,
        )
        session.add(book)
        session.flush()
        return _serialize(book)


@router.put("/books/{book_id}")
def update_book(book_id: int, request: SaveBookRequest) -> dict[str, Any]:
    from app.webexcel.db import webexcel_session
    from app.webexcel.models import WebExcelBook

    with webexcel_session() as session:
        book = session.get(WebExcelBook, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="Книга не найдена")
        book.name = request.name.strip() or book.name
        if request.snapshot:
            book.snapshot = request.snapshot
        if request.note:
            book.note = request.note
        session.flush()
        return _serialize(book)


@router.delete("/books/{book_id}")
def delete_book(book_id: int) -> dict[str, Any]:
    from app.webexcel.db import webexcel_session
    from app.webexcel.models import WebExcelBook

    with webexcel_session() as session:
        book = session.get(WebExcelBook, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="Книга не найдена")
        session.delete(book)
        return {"ok": True}


# ── Импорт целиком ──────────────────────────────────────────────────────────


class ImportRequest(BaseModel):
    spreadsheet_id: str
    tabs: list[str] = Field(default_factory=list)
    name: str = ""


@router.post("/import")
def import_book(request: ImportRequest) -> dict[str, Any]:
    """Импорт выбранных вкладок книги Google одним снимком Univer.

    Вкладки тянутся последовательно, а не параллельно: квота Google — 60 чтений
    в минуту на весь сервисный аккаунт, и этот же аккаунт обслуживает дашборд.
    Четыре параллельных импорта выели бы её за секунды и уронили бы дебиторку
    у всех остальных.

    **Фронт этим маршрутом не пользуется и пользоваться не должен.** Он ходит
    повкладочно в `/sources/{id}/tab` и собирает книгу у себя, потому что здесь
    есть потолок, которого не видно из кода: одна вкладка «Журнала» читается
    восемь секунд, у «Осн.Общей сводки» вкладок 23, а прокси Next рвёт запрос
    на 180 секундах. Маршрут оставлен для скриптов и разовых выгрузок, где
    вкладок немного.
    """
    _guard()
    try:
        meta = spreadsheet_meta(request.spreadsheet_id)
        wanted = request.tabs or [t["title"] for t in meta["tabs"] if not t["hidden"]][:1]
        converted = [convert_tab(fetch_tab_grid(request.spreadsheet_id, title)) for title in wanted]
    except WebExcelError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    workbook = build_workbook(request.spreadsheet_id, request.name or meta["title"], converted)
    stats = workbook.pop("_stats", [])
    fonts = workbook.pop("_fonts", [])
    return {
        "workbook": workbook,
        "stats": stats,
        "fonts": fonts,
        "tabs": wanted,
        "title": meta["title"],
    }


__all__ = ["router"]
