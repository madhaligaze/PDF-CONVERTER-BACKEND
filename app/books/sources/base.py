"""Что модуль требует от источника импорта.

Протокол намеренно узкий: перечислить книги, показать вкладки, отдать грид.
Записи здесь нет и не будет — приложение не пишет в источник ни при каких
обстоятельствах, работа переезжает во внутренние книги.

Свойство «мы не можем испортить чужую книгу» должно быть доказуемым. Три
уровня, и каждый следующий страхует предыдущий:

1. В протоколе нет метода записи — писать не через что.
2. Токен выдаётся со `spreadsheets.readonly` — Google откажет, даже если
   кто-то обойдёт протокол.
3. Тест разбирает исходники каталога и падает, если в них появится вызов
   записи, — раньше, чем это доедет до боевой книги.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Protocol


class SourceError(RuntimeError):
    """Источник не отдал данные. Текст предназначен человеку, а не логам."""


@dataclass(frozen=True)
class SourceBook:
    """Книга у источника — то, из чего человек выбирает при импорте."""

    id: str
    title: str
    modified_at: str = ""


@dataclass(frozen=True)
class SourceTab:
    """Вкладка книги."""

    id: str
    title: str
    rows: int = 0
    cols: int = 0
    hidden: bool = False


@dataclass(frozen=True)
class SourceGrid:
    """Прочитанная вкладка: сетка строк как есть, без разбора."""

    book_id: str
    book_title: str
    tab: SourceTab
    values: list[list[str]] = dc_field(default_factory=list)
    #: Уперлись в потолок чтения — значит книга прочитана не вся, и об этом
    #: надо сказать вслух, а не молча импортировать первые N строк.
    truncated: bool = False


class Source(Protocol):
    """Источник импорта. Только чтение — методов записи в протоколе нет."""

    def list_books(self) -> list[SourceBook]: ...

    def list_tabs(self, book_id: str) -> tuple[str, list[SourceTab]]: ...

    def read_tab(self, book_id: str, tab_title: str) -> SourceGrid: ...


__all__ = [
    "Source",
    "SourceBook",
    "SourceError",
    "SourceGrid",
    "SourceTab",
]
