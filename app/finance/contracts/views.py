"""Листы-отборы: правило блока и принадлежность договора к листам.

Лист ничего не хранит — он показывает договоры, подходящие под правило одного
из его блоков. Правило — группы условий «и», соединённые «или»:

    {"any": [{"all": [{"field": "type", "op": "in", "value": ["<id>", "<id>"]},
                      {"field": "executor_is_own", "op": "is", "value": true}]},
             {"all": [{"field": "subject", "op": "in", "value": ["<id>"]}]}]}

Пустое правило
──────────────
Отбор без условий не отбирает **ничего**. Главному листу отбор не нужен
вовсе: в нём стоит каждый договор (`place`), а условия его блоков только
раскладывают договоры по блокам. Раньше пустое `any` значило «все договоры»
для любого листа, и блок, которому разбор Excel не подобрал правила, молча
показывал весь реестр: лист «Заказчик ГК» на 192 строки выгружался пятью
тысячами договоров. Новый лист без правила теперь пуст, пока правило не задано.

Почему одной колонки мало: в реестре BBC «Агентский» и «Финансовая помощь»
лежат в **предмете** при виде «Иное», и отбор «по виду» не поймал бы ни одной
строки этих листов. Поэтому поле в условии — любое: вид, предмет, стороны,
статус, свои поля и виртуальные признаки ниже.

Принадлежность считает только сервер. Клиент предсказывает её на секунду для
только что сделанной правки, но отдельной реализации правил на клиенте нет —
две реализации однажды разошлись бы, и лист показывал бы не то, что в правиле.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from app.books.layout import norm

#: Виртуальные поля: их нет колонкой, они выводятся из сторон и смыслов.
VIRTUAL_FIELDS = {
    "executor_is_own": "Исполнитель — наше юрлицо",
    "customer_is_own": "Заказчик — наше юрлицо",
    "intra_group": "Обе стороны — наши юрлица",
    "phase": "Фаза статуса",
    "economic": "Системный смысл",
}
#: `lt`/`lte`/`gt`/`gte` — числа: «Срок, мес < 2», «Остаток (сводка) > 0».
NUMBER_OPS = ("lt", "lte", "gt", "gte")
OPS = ("in", "not_in", "eq", "neq", "empty", "not_empty", "contains", "is", *NUMBER_OPS)


class FilterError(ValueError):
    """Правило записано так, что его нельзя исполнить."""


def validate(rule: Any) -> dict[str, Any]:
    """Привести правило к виду `{"any": [{"all": [...]}, ...]}` или отказать."""
    if rule in (None, {}, []):
        return {"any": []}
    if not isinstance(rule, dict) or not isinstance(rule.get("any", []), list):
        raise FilterError("Правило листа: ждём {\"any\": [{\"all\": [...]}]}")
    groups: list[dict[str, Any]] = []
    for group in rule.get("any", []):
        conditions = group.get("all") if isinstance(group, dict) else None
        if not isinstance(conditions, list) or not conditions:
            raise FilterError("Правило листа: у группы должно быть хотя бы одно условие")
        clean: list[dict[str, Any]] = []
        for condition in conditions:
            if not isinstance(condition, dict) or not condition.get("field"):
                raise FilterError("Правило листа: у условия нет поля")
            op = condition.get("op", "in")
            if op not in OPS:
                raise FilterError(f"Правило листа: неизвестное условие «{op}»")
            if op in NUMBER_OPS and _number(condition.get("value")) is None:
                raise FilterError("Правило листа: в сравнении нужно число")
            clean.append({"field": str(condition["field"]), "op": op, "value": condition.get("value")})
        groups.append({"all": clean})
    return {"any": groups}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]


def _fact_values(facts: dict[str, Any], field: str) -> list[str]:
    value = facts.get(field)
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item not in (None, "")]
    if isinstance(value, bool):
        return ["true" if value else "false"]
    return [str(value)]


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if len(value) == 1 else None
        if value is None:
            return None
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _check(condition: dict[str, Any], facts: dict[str, Any]) -> bool:
    field, op, value = condition["field"], condition["op"], condition.get("value")
    if op in NUMBER_OPS:
        # Пустое не больше и не меньше ничего: договор без даты не стоит ни в
        # «до 2 мес», ни в «6+ мес», а без суммы из сводки — не в «Остатках».
        have_number, want = _number(facts.get(field)), _number(value)
        if have_number is None or want is None:
            return False
        return {
            "lt": have_number < want,
            "lte": have_number <= want,
            "gt": have_number > want,
            "gte": have_number >= want,
        }[op]
    have = _fact_values(facts, field)
    if op == "empty":
        return not have
    if op == "not_empty":
        return bool(have)
    if op == "is":
        return bool(facts.get(field)) is bool(value)
    if op == "contains":
        # У полей-ссылок (вид, предмет, стороны, свои списки) в фактах лежит
        # идентификатор; текст — рядом, в «<поле>__text». Сравнивать с id
        # значит не отобрать ничего: «предмет содержит „аренд“» молчал.
        needle = norm(value)
        text = facts.get(f"{field}__text")
        pool = _fact_values({"text": text}, "text") if text is not None else have
        return bool(needle) and any(needle in norm(item) for item in pool)
    wanted = _as_list(value)
    if op in ("eq", "in"):
        return any(item in wanted for item in have)
    if op in ("neq", "not_in"):
        return not any(item in wanted for item in have)
    return False


def is_empty(rule: dict[str, Any] | None) -> bool:
    """Правило без единого условия."""
    return not ((rule or {}).get("any") or [])


def matches(rule: dict[str, Any] | None, facts: dict[str, Any]) -> bool:
    """Подходит ли договор под правило. Пустое правило не отбирает ничего."""
    groups = (rule or {}).get("any") or []
    if not groups:
        return False
    return any(all(_check(condition, facts) for condition in group.get("all", [])) for group in groups)


def place(view: Any, facts: dict[str, Any]) -> int | None:
    """Блок листа, в котором стоит договор, или `None` — договора в листе нет.

    Договор встаёт в первый подходящий блок: в двух блоках одного листа одна
    запись не показывается — в Excel она и стояла бы в одном месте. Главный
    лист держит все договоры: первый блок с подходящим правилом, иначе первый
    блок без правила, иначе первый блок.
    """
    blocks = [block or {} for block in (view.blocks or [])]
    for index, block in enumerate(blocks):
        if matches(block.get("filter"), facts):
            return index
    if not getattr(view, "main", False):
        return None
    for index, block in enumerate(blocks):
        if is_empty(block.get("filter")):
            return index
    return 0


def membership(facts: dict[str, Any], views: Iterable[Any]) -> list[dict[str, Any]]:
    """В каких листах и блоках стоит договор: `[{"view": key, "block": i}]`."""
    out: list[dict[str, Any]] = []
    for view in views:
        index = place(view, facts)
        if index is not None:
            out.append({"view": view.key, "block": index})
    return out


def fields_used(rule: dict[str, Any] | None) -> set[str]:
    return {
        condition["field"]
        for group in (rule or {}).get("any") or []
        for condition in group.get("all", [])
    }


__all__ = [
    "FilterError", "NUMBER_OPS", "OPS", "VIRTUAL_FIELDS", "fields_used", "is_empty", "matches", "membership", "place", "validate",
]
