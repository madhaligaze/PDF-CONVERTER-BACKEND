"""Раздел «Финансы» не знает про BBC — ни пакет, ни маршруты.

Зачем этот тест
───────────────
Сначала раздел пускал внутрь учётки BBC Dashboard, и это было неверно по
существу: в «Финансах» каждая компания регистрируется сама и ведёт свой учёт.
Более того, BBC может однажды сам переехать сюда — и зависимость смотрела бы от
общего к частному.

Связь протекает незаметно: кто-то допишет `from app.bbc.deps import ...`, чтобы
«быстро проверить права», и через месяц окажется, что раздел, задуманный
самостоятельным, знает про конкретную компанию. Тест ловит это в момент
появления — и в пакете, и в маршрутах.
"""
from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "finance"
ROUTES = BACKEND / "app" / "api" / "routes" / "finance.py"

#: Пакеты, на которые модуль имеет право ссылаться.
#:
#: `app.books.layout` в списке намеренно: привязка колонок по названиям — общая
#: машинерия, вынесенная туда первой. Дублировать её было бы хуже: две функции с
#: одним именем, по-разному решающие, какая колонка наша, разъезжаются на первой
#: правке и молча.
#:
#: `app.services` — разбор банковских выписок: тот же код читает PDF и в разделе
#: «Анализ выписок», и здесь. Две копии разбора Kaspi разъехались бы так же.
#:
#: Клиента Google здесь нет: он был общим с «Таблицами» (`app.webexcel.google`),
#: а теперь живёт в самом разделе — `app.finance.google`.
ALLOWED_PREFIXES = (
    "app.core",
    "app.finance",
    "app.books.layout",
    "app.services",
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


def _outside(names: set[str]) -> set[str]:
    return {
        name
        for name in names
        if not any(name == prefix or name.startswith(prefix + ".") for prefix in ALLOWED_PREFIXES)
    }


def test_paket_ne_zavisit_ot_chuzhih_moduley() -> None:
    leaks: dict[str, set[str]] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        outside = _outside(_imports(path))
        if outside:
            leaks[path.name] = outside
    assert not leaks, (
        "Пакет «Финансы» сослался на чужие модули: "
        + "; ".join(f"{name} → {', '.join(sorted(items))}" for name, items in leaks.items())
    )


def test_marshruty_ne_zavisyat_ot_uchetok_bbc() -> None:
    """Главное отличие от первой версии: в маршрутах тоже нет `app.bbc`."""
    outside = _outside(_imports(ROUTES))
    assert not outside, f"маршруты «Финансов» импортируют чужое: {sorted(outside)}"


def test_marshruty_zakryty_svoey_avtorizatsiey() -> None:
    """Обратная проверка: убрать чужую охрану мало, надо поставить свою.

    Без неё тест выше можно «пройти», просто сняв все проверки прав.
    """
    source = ROUTES.read_text(encoding="utf-8")
    assert "def current_member(" in source
    assert 'require_ability("write")' in source
    assert 'require_ability("accounts")' in source
    # Проверяем отсутствие ИМПОРТА, а не слова: слово есть в объяснении, почему
    # чужой охраны здесь больше нет, и это объяснение полезно.
    assert "from app.bbc" not in source
    assert "Depends(require_block_user" not in source


def test_marshruty_berut_kompaniyu_iz_sessii() -> None:
    """Компания приходит из сессии, а не «по умолчанию».

    `ensure_workspace` возвращает компанию `default` — она осталась для тестов и
    для данных, заведённых до появления учёток. Если она попадёт в маршрут,
    человек, вошедший в свою компанию, увидит чужие деньги.
    """
    source = ROUTES.read_text(encoding="utf-8")
    assert "ensure_workspace" not in source, (
        "в маршруте оказалась компания по умолчанию — берите её из сессии через _workspace()"
    )
    assert "_workspace(session, member)" in source
