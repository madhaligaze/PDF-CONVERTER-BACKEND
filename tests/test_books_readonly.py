"""Приложение не пишет в Google. Это проверяется, а не обещается.

Работа переезжает во внутренние книги, Google остаётся каналом импорта. Значит
свойство «мы не можем испортить чужую книгу» — часть продукта, и держаться на
внимательности оно не должно: правку, добавляющую запись, ревьюер не заметит
среди тридцати строк, а заметит финансист, когда книга поедет.

Три уровня, каждый страхует предыдущий:

1. **Скоупы токена.** `spreadsheets.readonly` — Google откажет в записи, даже
   если такой вызов появится в коде. Это единственная защита, не зависящая от
   нашей дисциплины, и потому главная.
2. **Протокол источника.** В `books/sources/base.py` нет метода записи —
   писать не через что.
3. **Разбор исходников** — здесь. Ловит вызов записи в тот день, когда его
   написали.

Про `clear()`
─────────────
В списке запрещённых его нет намеренно. У листа Google `clear()` стирает
данные, а у обычного словаря — сбрасывает кэш, и статически они неразличимы:
`invalidate_cache` в `gsheets.py` честно зовёт `_meta_cache.clear()`. Ловить
его значило бы либо получать ложную тревогу на каждом кэше, либо вести список
исключений, который однажды и спрячет настоящий вызов. Уровни 1 и 2 этот
случай закрывают.
"""
from __future__ import annotations

import ast
from pathlib import Path

SOURCES = Path(__file__).resolve().parents[1] / "app" / "books" / "sources"

#: Методы gspread, меняющие книгу. Однозначные — те, что не встречаются у
#: словарей, списков и прочих обычных объектов.
WRITE_METHODS = frozenset({
    "update",
    "update_cell",
    "update_cells",
    "update_acell",
    "update_title",
    "append_row",
    "append_rows",
    "insert_row",
    "insert_rows",
    "insert_note",
    "delete_row",
    "delete_rows",
    "delete_columns",
    "batch_update",
    "batch_clear",
    "add_worksheet",
    "del_worksheet",
    "duplicate_sheet",
    "copy_to",
    "share",
    "add_protected_range",
    "set_basic_filter",
    "resize",
    "format",
})


def _write_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in WRITE_METHODS:
            found.append(f"{path.name}:{node.lineno} → .{func.attr}()")
    return found


def test_no_write_calls_in_source_adapters() -> None:
    offenders: list[str] = []
    for path in sorted(SOURCES.rglob("*.py")):
        offenders.extend(_write_calls(path))

    assert not offenders, (
        "в источниках импорта появилась запись в Google:\n"
        + "\n".join(f"  · {line}" for line in offenders)
        + "\n\nПриложение не пишет в чужие книги. Если нужна правка — она "
        "делается во внутренней книге."
    )


def test_scopes_are_read_only() -> None:
    """Главная защита: токен физически не может писать."""
    from app.books.config import GOOGLE_SCOPES

    assert GOOGLE_SCOPES, "скоупы не заданы вовсе"
    for scope in GOOGLE_SCOPES:
        assert scope.endswith(".readonly"), f"скоуп «{scope}» разрешает запись"


def test_source_protocol_has_no_write_method() -> None:
    """В протоколе источника нет метода записи — писать не через что."""
    from app.books.sources.base import Source

    methods = {name for name in dir(Source) if not name.startswith("_")}
    assert methods == {"list_books", "list_tabs", "read_tab"}, methods


def test_adapters_actually_scanned() -> None:
    """Сам тест обязан что-то проверять.

    Без этого переименование каталога превратило бы проверку выше в вечно
    зелёную: `rglob` по несуществующему пути не находит ничего и радостно
    сообщает, что нарушений нет.
    """
    files = list(SOURCES.rglob("*.py"))
    assert len(files) >= 3, f"в {SOURCES} нашлось всего {len(files)} файлов"
