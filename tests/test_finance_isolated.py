"""Пакет `app.finance` не знает про учётки этого продукта.

Зачем этот тест
───────────────
Тот же, что у «Книг» (`test_books_isolated`). Модуль задуман переносимым: та же
машинерия должна обслуживать другую компанию с другой моделью доступа. Связь с
`app.bbc` живёт ровно в одном файле — `app/api/routes/finance.py`, в корне
композиции.

Связь протекает незаметно: кто-то допишет `from app.bbc.deps import ...` внутрь
`service.py`, чтобы «быстро проверить права», и через месяц окажется, что
раздел, задуманный общим, знает про конкретную компанию. Тест ловит это в
момент появления.
"""
from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "app" / "finance"

#: Пакеты, на которые модуль имеет право ссылаться.
#:
#: `app.books.layout` в списке намеренно: привязка колонок по названиям — общая
#: машинерия, вынесенная туда первой. Дублировать её было бы хуже: две функции с
#: одним именем, по-разному решающие, какая колонка наша, разъезжаются на первой
#: правке и молча.
ALLOWED_PREFIXES = (
    "app.core",
    "app.finance",
    "app.books.layout",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return {name for name in found if name.startswith("app.")}


def test_finance_ne_zavisit_ot_uchetok_produkta() -> None:
    leaks: dict[str, set[str]] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        outside = {
            name
            for name in _imports(path)
            if not any(name == prefix or name.startswith(prefix + ".") for prefix in ALLOWED_PREFIXES)
        }
        if outside:
            leaks[path.name] = outside
    assert not leaks, (
        "Пакет «Финансы» сослался на чужие модули: "
        + "; ".join(f"{name} → {', '.join(sorted(items))}" for name, items in leaks.items())
        + ". Связь с учётками живёт только в app/api/routes/finance.py."
    )


def test_marshruty_znayut_pro_prava() -> None:
    """Обратная проверка: корень композиции обязан права спрашивать.

    Без неё тест выше можно «пройти», просто не поставив охрану никуда.
    """
    source = (PACKAGE.parents[0] / "api" / "routes" / "finance.py").read_text(encoding="utf-8")
    assert "require_block_user(\"finance\")" in source
    assert "require_admin" in source
