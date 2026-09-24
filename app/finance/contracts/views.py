"""Листы-отборы: правило блока и принадлежность договора к листам.

Лист ничего не хранит — он показывает договоры, подходящие под правило одного
из его блоков. Правило — группы условий «и», соединённые «или»:

    {"any": [{"all": [{"field": "type", "op": "in", "value": ["<id>", "<id>"]},
                      {"field": "executor_is_own", "op": "is", "value": true}]},
             {"all": [{"field": "subject", "op": "in", "value": ["<id>"]}]}]}

Пустое `any` — «все договоры»: так устроен главный лист.

Почему одной колонки мало: в реестре BBC «Агентский» и «Финансовая помощь»
лежат в **предмете** при виде «Иное», и отбор «по виду» не поймал бы ни одной
строки этих листов. Поэтому поле в условии — любое: вид, предмет, стороны,
статус, свои поля и виртуальные признаки ниже.

Принадлежность считает только сервер. Клиент предсказывает её на секунду для
только что сделанной правки, но отдельной реализации правил на клиенте нет —
две реализации однажды разошлись бы, и лист показывал бы не то, что в правиле.
"""
from __future__ import annotations

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
OPS = ("in", "not_in", "eq", "neq", "empty", "not_empty", "contains", "is")


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


def _check(condition: dict[str, Any], facts: dict[str, Any]) -> bool:
    field, op, value = condition["field"], condition["op"], condition.get("value")
    have = _fact_values(facts, field)
    if op == "empty":
        return not have
    if op == "not_empty":
        return bool(have)
    if op == "is":
        return bool(facts.get(field)) is bool(value)
    if op == "contains":
        needle = norm(value)
        return bool(needle) and any(needle in norm(item) for item in have)
    wanted = _as_list(value)
    if op in ("eq", "in"):
        return any(item in wanted for item in have)
    if op in ("neq", "not_in"):
        return not any(item in wanted for item in have)
    return False


def matches(rule: dict[str, Any] | None, facts: dict[str, Any]) -> bool:
    groups = (rule or {}).get("any") or []
    if not groups:
        return True
    return any(all(_check(condition, facts) for condition in group.get("all", [])) for group in groups)


def membership(facts: dict[str, Any], views: Iterable[Any]) -> list[dict[str, Any]]:
    """В каких листах и блоках стоит договор: `[{"view": key, "block": i}]`.

    Договор встаёт в первый подходящий блок листа — в двух блоках одного листа
    одна запись не показывается: в Excel она и стояла бы в одном месте.
    """
    out: list[dict[str, Any]] = []
    for view in views:
        for index, block in enumerate(view.blocks or []):
            if matches((block or {}).get("filter"), facts):
                out.append({"view": view.key, "block": index})
                break
    return out


def fields_used(rule: dict[str, Any] | None) -> set[str]:
    return {
        condition["field"]
        for group in (rule or {}).get("any") or []
        for condition in group.get("all", [])
    }


__all__ = ["FilterError", "OPS", "VIRTUAL_FIELDS", "fields_used", "matches", "membership", "validate"]
