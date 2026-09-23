"""Полка «Таблиц»: одна запись — одна таблица.

Снимок книги Univer (`IWorkbookData`) хранится **строкой**, а не JSON-колонкой, и
сервер его не разбирает никогда. Разбор снимка в объекты Python стоит примерно
в десять раз больше его размера: таблица на 20 МБ — это 200 МБ памяти на одно
сохранение. Строка же проходит насквозь: пришла телом запроса, легла в базу,
ушла обратно. Postgres сжимает её сам (TOAST).

`sheets` — оглавление для полки: названия листов и их размер. Отдельно от снимка,
чтобы список полки не поднимал из базы ни одного снимка.

Имя таблицы `shelf`, а не прежнее `books`: `books` есть и у модуля «Книги», и на
SQLite, где схем нет, два одноимённых класса метили в одну таблицу.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.webexcel.db import WebExcelBase


def _now() -> datetime:
    return datetime.now(UTC)


class ShelfTable(WebExcelBase):
    __tablename__ = "shelf"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # blank — своя; google — по ссылке из Google Sheets; file — из файла .xlsx.
    source: Mapped[str] = mapped_column(String(16), default="blank", nullable=False)
    # Ссылка на книгу Google или имя файла — откуда таблица пришла.
    source_ref: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    sheets: Mapped[list | None] = mapped_column(JSON, default=list, nullable=True)
    snapshot: Mapped[str] = mapped_column(Text, default="", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


__all__ = ["ShelfTable"]
