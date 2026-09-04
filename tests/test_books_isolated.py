"""«Книги» не знают про BBC — и это проверяется, а не подразумевается.

Зачем
─────
Модуль задуман общим: та же машинерия должна обслуживать другую компанию с
другими книгами. Ровно поэтому он оперирует полями, ролями и привязками, а всё
специфичное для компании хранит данными.

Такие договорённости не держатся сами. Через месяц кому-то понадобится
`canonical_firm` из `app.bbc.normalize` — импорт короче, чем вынести функцию в
общее место, — и модуль перестанет быть переносимым в тот же день. Заметить это
по код-ревью почти невозможно: одна строка импорта среди тридцати.

Тест разбирает исходники синтаксически, а не импортирует их: импорт потащил бы
за собой настройки и подключение к базе, и проверка границы зависела бы от
наличия Postgres.

Что делать, если тест упал
──────────────────────────
Не добавлять исключение. Нужное из `bbc` — вынести в общее место, как уже
сделано с разбором ячеек (`app/core/scalars.py`) и как будет сделано с
привязкой колонок по названиям.
"""
from __future__ import annotations

import ast
from pathlib import Path

BOOKS = Path(__file__).resolve().parents[1] / "app" / "books"

#: Модули, знающие про конкретную компанию. Импорт любого из них внутри
#: «Книг» означает, что общий модуль перестал быть общим.
FORBIDDEN_ROOTS = ("app.bbc", "app.webexcel", "app.telegram")


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_books_does_not_import_company_specific_modules() -> None:
    offenders: list[str] = []
    for path in sorted(BOOKS.rglob("*.py")):
        for name in sorted(_imports_of(path)):
            if any(name == root or name.startswith(root + ".") for root in FORBIDDEN_ROOTS):
                offenders.append(f"{path.relative_to(BOOKS.parent.parent)} → {name}")

    assert not offenders, (
        "модуль «Книги» перестал быть независимым:\n"
        + "\n".join(f"  · {line}" for line in offenders)
        + "\n\nНужное из этих модулей выносится в общее место, а не импортируется сюда."
    )


def test_books_module_actually_scanned() -> None:
    """Сам тест обязан что-то проверять.

    Без этого переименование каталога превратило бы проверку выше в вечно
    зелёную: `rglob` по несуществующему пути не находит ничего и радостно
    сообщает, что нарушений нет.
    """
    files = list(BOOKS.rglob("*.py"))
    assert len(files) >= 4, f"в {BOOKS} нашлось всего {len(files)} файлов"
