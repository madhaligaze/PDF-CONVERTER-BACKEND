"""Чтение книг из Google Sheets. Только чтение.

Здесь нет ни одного вызова записи, и это проверяется тестом
`test_books_readonly.py`, который разбирает исходники каталога. Токен строится
со `spreadsheets.readonly` — Google откажет в записи, даже если кто-то напишет
такой вызов мимо протокола.

Про квоту
─────────
Квота Google — 60 чтений в минуту на весь сервисный аккаунт, и этот же аккаунт
обслуживает дашборд BBC. Один импорт книги на восемь вкладок съедает её
восьмую часть. Поэтому: вкладки читаются по одной и только те, что попросили;
список книг и вкладок живёт в коротком кэше — состав книги меняется раз в
месяц, а спрашивают о нём на каждое открытие экрана.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from app.books.config import GOOGLE_SCOPES, books_settings
from app.books.sources.base import SourceBook, SourceError, SourceGrid, SourceTab

log = logging.getLogger(__name__)

#: Сколько живёт список книг и вкладок. Гриды не кэшируются: их читают, чтобы
#: узнать, что изменилось, и кэш здесь означал бы «не заметить правку».
META_TTL_SECONDS = 300.0


def humanize(exc: Exception) -> str:
    """Ошибка Google человеческим языком.

    Текст уезжает прямо на экран импорта, поэтому «APIError: [429]» не годится:
    человеку надо понять, что делать, а не что случилось внутри библиотеки.
    """
    text = str(exc)
    if "429" in text or "Quota exceeded" in text:
        return (
            "Google не успевает отвечать: превышена квота на чтение. "
            "Подождите минуту и повторите."
        )
    if "403" in text:
        return (
            "Google не даёт доступ к книге. Расшарьте её сервисному аккаунту "
            "хотя бы на чтение."
        )
    if "404" in text:
        return "Книга не найдена: проверьте ссылку — возможно, её удалили."
    return f"Google вернул ошибку: {text}"


def _credentials():
    raw = (books_settings.service_account_json or "").strip()
    if not raw:
        raise SourceError("Не настроены креды Google — импорт недоступен")

    from google.oauth2.service_account import Credentials

    try:
        if raw.startswith("{"):
            return Credentials.from_service_account_info(
                json.loads(raw), scopes=GOOGLE_SCOPES
            )
        path = books_settings.credentials_path
        if path is None or not path.is_file():
            raise SourceError(f"Файл ключа не найден: {raw}")
        return Credentials.from_service_account_file(str(path), scopes=GOOGLE_SCOPES)
    except SourceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SourceError(f"Ключ Google не читается: {exc}") from exc


def _client():
    import gspread

    return gspread.authorize(_credentials())


_meta_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _cached(key: str, produce):
    now = time.monotonic()
    with _cache_lock:
        hit = _meta_cache.get(key)
        if hit is not None and now - hit[0] < META_TTL_SECONDS:
            return hit[1]
    value = produce()
    with _cache_lock:
        _meta_cache[key] = (now, value)
    return value


def invalidate_cache() -> None:
    """Сбросить кэш — по кнопке «Обновить список»."""
    with _cache_lock:
        _meta_cache.clear()


class GoogleSheetsSource:
    """Книги Google, доступные сервисному аккаунту."""

    def list_books(self) -> list[SourceBook]:
        def produce() -> list[SourceBook]:
            try:
                files = _client().list_spreadsheet_files()
            except Exception as exc:  # noqa: BLE001
                raise SourceError(humanize(exc)) from exc
            return [
                SourceBook(
                    id=item.get("id", ""),
                    title=item.get("name", "") or "Без названия",
                    modified_at=item.get("modifiedTime", "") or "",
                )
                for item in files
                if item.get("id")
            ]

        return _cached("books", produce)

    def list_tabs(self, book_id: str) -> tuple[str, list[SourceTab]]:
        def produce() -> tuple[str, list[SourceTab]]:
            try:
                book = _client().open_by_key(book_id)
                worksheets = book.worksheets()
                title = book.title
            except Exception as exc:  # noqa: BLE001
                raise SourceError(humanize(exc)) from exc
            tabs = [
                SourceTab(
                    id=str(ws.id),
                    title=ws.title,
                    rows=ws.row_count,
                    cols=ws.col_count,
                    hidden=bool(getattr(ws, "isSheetHidden", False)),
                )
                for ws in worksheets
            ]
            return title, tabs

        return _cached(f"tabs:{book_id}", produce)

    def read_tab(self, book_id: str, tab_title: str) -> SourceGrid:
        """Одна вкладка целиком, в пределах потолков.

        Потолки — не экономия, а защита от книги, заведённой с запасом:
        «Тех.Журнал» пилотной книги объявляет 11409 строк на 37 колонок. Если
        уперлись, об этом сообщается флагом, а не молчанием: импортировать
        половину книги, не сказав об этом, — худший исход.
        """
        try:
            book = _client().open_by_key(book_id)
            worksheet = book.worksheet(tab_title)
            values = worksheet.get_all_values()
            book_title = book.title
            tab = SourceTab(
                id=str(worksheet.id),
                title=worksheet.title,
                rows=worksheet.row_count,
                cols=worksheet.col_count,
            )
        except Exception as exc:  # noqa: BLE001
            raise SourceError(humanize(exc)) from exc

        max_rows = max(1, books_settings.max_rows)
        max_cols = max(1, books_settings.max_cols)
        truncated = len(values) > max_rows or any(len(row) > max_cols for row in values)
        if truncated:
            log.warning(
                "Книги: вкладка «%s» прочитана не целиком (%s строк, потолок %s)",
                tab_title, len(values), max_rows,
            )
            values = [row[:max_cols] for row in values[:max_rows]]

        return SourceGrid(
            book_id=book_id,
            book_title=book_title,
            tab=tab,
            values=values,
            truncated=truncated,
        )


__all__ = [
    "META_TTL_SECONDS",
    "GoogleSheetsSource",
    "humanize",
    "invalidate_cache",
]
