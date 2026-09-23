"""HTTP-маршруты полки «Таблиц».

Входа нет: раздел открыт всем, у кого есть адрес. Поэтому здесь нет ничего, кроме
своих таблиц — ни чужих книг, ни сервисного аккаунта, — и у одной таблицы есть
потолок размера (`WEBEXCEL_MAX_TABLE_MB`).

Снимок книги ходит строкой и сервером не разбирается — почему, см. `models.py`.
Список полки поднимает из базы только оглавление: снимки там не нужны, а у
десятка больших таблиц они весят сотни мегабайт.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import load_only

from app.webexcel.config import webexcel_settings
from app.webexcel.db import webexcel_session
from app.webexcel.models import ShelfTable

router = APIRouter(prefix="/web-excel", tags=["web-excel"])

SOURCES = ("blank", "google", "file")


class SheetInfo(BaseModel):
    name: str = Field(default="", max_length=200)
    rows: int = Field(default=0, ge=0)
    cols: int = Field(default=0, ge=0)


class SaveTableRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    source: str | None = Field(default=None, max_length=16)
    source_ref: str | None = Field(default=None, max_length=500)
    sheets: list[SheetInfo] | None = Field(default=None, max_length=500)
    # `IWorkbookData`, сериализованный клиентом. Строкой — см. `models.py`.
    snapshot: str | None = None


def _guard() -> None:
    if not webexcel_settings.enabled:
        raise HTTPException(status_code=404, detail="Раздел «Таблицы» выключен")


def _checked_snapshot(raw: str) -> tuple[str, int]:
    size = len(raw.encode("utf-8"))
    if size > webexcel_settings.max_table_bytes:
        mb = size / (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=(
                f"Таблица весит {mb:.1f} МБ — на полку помещается до "
                f"{webexcel_settings.max_table_mb} МБ. Разделите её на две"
            ),
        )
    # Разбирать снимок ради проверки — ровно та цена, от которой строка и
    # спасает. Первого знака достаточно, чтобы не положить на полку пустоту или
    # текст ошибки вместо книги.
    if not raw.lstrip().startswith("{"):
        raise HTTPException(status_code=422, detail="Снимок таблицы повреждён")
    return raw, size


def _clean_name(name: str | None, fallback: str) -> str:
    cleaned = (name or "").strip()
    return cleaned[:200] or fallback


def _serialize(table: ShelfTable, *, with_snapshot: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": table.id,
        "name": table.name,
        "source": table.source,
        "source_ref": table.source_ref,
        "sheets": table.sheets or [],
        "size_bytes": table.size_bytes,
        "created_at": table.created_at.isoformat() if table.created_at else None,
        "updated_at": table.updated_at.isoformat() if table.updated_at else None,
    }
    if with_snapshot:
        payload["snapshot"] = table.snapshot
    return payload


_SHELF_COLUMNS = (
    ShelfTable.id,
    ShelfTable.name,
    ShelfTable.source,
    ShelfTable.source_ref,
    ShelfTable.sheets,
    ShelfTable.size_bytes,
    ShelfTable.created_at,
    ShelfTable.updated_at,
)


@router.get("/shelf")
def list_shelf() -> dict[str, Any]:
    _guard()
    with webexcel_session() as session:
        rows = session.scalars(
            select(ShelfTable)
            .options(load_only(*_SHELF_COLUMNS))
            .order_by(ShelfTable.updated_at.desc(), ShelfTable.id.desc())
        ).all()
        return {"tables": [_serialize(row) for row in rows]}


@router.get("/shelf/{table_id}")
def get_table(table_id: int) -> dict[str, Any]:
    _guard()
    with webexcel_session() as session:
        table = session.get(ShelfTable, table_id)
        if table is None:
            raise HTTPException(status_code=404, detail="Таблицы на полке больше нет")
        return _serialize(table, with_snapshot=True)


@router.post("/shelf")
def create_table(request: SaveTableRequest) -> dict[str, Any]:
    _guard()
    snapshot, size = _checked_snapshot(request.snapshot or "")
    source = request.source if request.source in SOURCES else "blank"
    with webexcel_session() as session:
        table = ShelfTable(
            name=_clean_name(request.name, "Новая таблица"),
            source=source,
            source_ref=(request.source_ref or "")[:500],
            sheets=[sheet.model_dump() for sheet in request.sheets or []],
            snapshot=snapshot,
            size_bytes=size,
        )
        session.add(table)
        session.flush()
        return _serialize(table)


@router.put("/shelf/{table_id}")
def update_table(table_id: int, request: SaveTableRequest) -> dict[str, Any]:
    """Сохранить таблицу или переименовать её: поля, которых нет в запросе, не трогаются."""
    _guard()
    checked = _checked_snapshot(request.snapshot) if request.snapshot is not None else None
    with webexcel_session() as session:
        table = session.get(ShelfTable, table_id)
        if table is None:
            raise HTTPException(status_code=404, detail="Таблицы на полке больше нет")
        if request.name is not None:
            table.name = _clean_name(request.name, table.name)
        if request.sheets is not None:
            table.sheets = [sheet.model_dump() for sheet in request.sheets]
        if checked is not None:
            table.snapshot, table.size_bytes = checked
        session.flush()
        return _serialize(table)


@router.post("/shelf/{table_id}/copy")
def copy_table(table_id: int) -> dict[str, Any]:
    _guard()
    with webexcel_session() as session:
        original = session.get(ShelfTable, table_id)
        if original is None:
            raise HTTPException(status_code=404, detail="Таблицы на полке больше нет")
        copy = ShelfTable(
            name=_clean_name(f"{original.name} (копия)", original.name),
            source=original.source,
            source_ref=original.source_ref,
            sheets=list(original.sheets or []),
            snapshot=original.snapshot,
            size_bytes=original.size_bytes,
        )
        session.add(copy)
        session.flush()
        return _serialize(copy)


@router.delete("/shelf/{table_id}")
def delete_table(table_id: int) -> dict[str, Any]:
    _guard()
    with webexcel_session() as session:
        table = session.get(ShelfTable, table_id)
        if table is None:
            raise HTTPException(status_code=404, detail="Таблицы на полке больше нет")
        session.delete(table)
        return {"ok": True}


__all__ = ["router"]
