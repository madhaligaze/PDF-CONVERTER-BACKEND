"""Модель одной книги Web-Excel.

Снимок хранится целиком в `snapshot` (JSON `IWorkbookData`), а не разложенным
по ячейкам. Это осознанно: книга читается и пишется целиком одним экраном, а
запросов вида «сумма по колонке за июль» к этой таблице не бывает — за ними
ходят в дашборд, который считает по своим источникам.

`origin_spreadsheet_id` — id книги в Google, из которой сделан импорт. Он нужен,
чтобы позже показать «в исходнике появились новые строки», и чтобы не
импортировать одну и ту же книгу дважды под разными именами.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.webexcel.db import WebExcelBase


def _now() -> datetime:
    return datetime.now(UTC)


class WebExcelBook(WebExcelBase):
    __tablename__ = "books"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # «Новая таблица» или «импортирована из Google» — различаются в списке.
    kind: Mapped[str] = mapped_column(String(16), default="blank", nullable=False)
    origin_spreadsheet_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    origin_title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    # Список импортированных вкладок — показывается в карточке книги.
    origin_tabs: Mapped[list | None] = mapped_column(JSON, default=list, nullable=True)
    snapshot: Mapped[dict | None] = mapped_column(JSON, default=dict, nullable=True)
    # Кто последним сохранял. Пусто, пока раздел не за логином.
    updated_by: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


__all__ = ["WebExcelBook"]
