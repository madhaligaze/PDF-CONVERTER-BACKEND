"""Приведение значений книги к каноническому виду — справочники BBC.

Разбор ячейки в типизированное значение (`clean`, `parse_money`, `parse_date`,
`parse_bool`) переехал в `app/core/scalars.py` и реэкспортируется отсюда: грязь
в ячейках — свойство таблиц, которые ведут руками, а не свойство BBC, и тот же
разбор нужен разделу «Книги». Вызывающие этого переезда не заметили — имена и
поведение прежние.

Здесь осталось то, что действительно про BBC: как в книгах пишут названия
юрлиц, видов услуг, статусов договора и отделов. Одно и то же значение пишут
по-разному («Разовый» / «Разовая» / «Разовая услуга»), и свод по такой колонке
без приведения к канону распадается на три строки вместо одной.
"""
from __future__ import annotations

from app.core.scalars import (
    SPACE_RE as _SPACE_RE,
    clean,
    is_error,
    parse_bool,
    parse_date,
    parse_money,
)


# ── Canonical dimensions ─────────────────────────────────────────────────────────

# Legal entity codes as typed in «Наша Фирма» → readable names.
FIRM_LABELS: dict[str, str] = {
    "BBC": "Big Business Consulting",
    "BBCS": "SAKOMPA-M",
    "BBCL": "BBC Legal Support",
    "BBCA": "Big Business Consulting & Audit",
    "BBC HR": "BBC HR",
    "BBC ASTANA": "BBC Astana",
    "EA": "Elite Advisory",
}

_SERVICE_KINDS: dict[str, str] = {
    "АБОН.П.": "Абонентская плата",
    "АБОНЕНТСКОЕ ОБСЛУЖИВАНИЕ": "Абонентская плата",
    "РАЗОВЫЙ": "Разовая услуга",
    "РАЗОВАЯ": "Разовая услуга",
    "РАЗОВАЯ УСЛУГА": "Разовая услуга",
    "АРЕНДА": "Аренда",
    "ПЕРИОДИЧЕСКИ": "Периодическая",
    "ПО КВАРТАЛЬНЫЙ ???": "Периодическая",
    "ИНОЕ": "Иное",
}

_STATUSES: dict[str, str] = {
    "ПРОДЛЕНИЕ": "Продление",
    "ДЕЙСТВ.": "Действующий",
    "НА ИСПОЛНЕНИИ": "На исполнении",
    "НА ИСП.": "На исполнении",
    "РАЗОВЫЙ": "Разовый",
    "НЕ ИЗВЕСТНО": "Неизвестно",
    "НЕ СОСТОЯЛОСЬ": "Не состоялось",
}

DEPARTMENT_LABELS: dict[str, str] = {
    "ОБО": "Бухгалтерский отдел",
    "НО": "Налоговый отдел",
    "ЮО": "Юридический отдел",
    "HR": "Кадровый отдел",
    "ФО": "Финансовый отдел",
}


def canonical_firm(value: object) -> str:
    """Uppercase entity code, unchanged if unknown (never silently dropped)."""
    return _SPACE_RE.sub(" ", clean(value)).upper()


def firm_label(code: str) -> str:
    return FIRM_LABELS.get(canonical_firm(code), code)


def canonical_service_kind(value: object) -> str:
    """Collapse «Разовый»/«Разовая»/«Разовая услуга» into one label."""
    text = _SPACE_RE.sub(" ", clean(value)).upper()
    if not text:
        return ""
    return _SERVICE_KINDS.get(text, clean(value))


def canonical_status(value: object) -> str:
    """Collapse the status spellings; free-text notes are kept verbatim."""
    text = _SPACE_RE.sub(" ", clean(value)).upper()
    if not text:
        return ""
    if text in _STATUSES:
        return _STATUSES[text]
    # «Приостоновление в Июне» / «Приостоновили в Июне» → one bucket.
    if text.startswith("ПРИОСТ"):
        return "Приостановлен"
    if text.startswith("РАСТОРЖ"):
        return "Расторжение"
    return clean(value)


def department_label(code: str) -> str:
    return DEPARTMENT_LABELS.get(clean(code).upper(), code)


__all__ = [
    "DEPARTMENT_LABELS",
    "FIRM_LABELS",
    "canonical_firm",
    "canonical_service_kind",
    "canonical_status",
    "clean",
    "department_label",
    "firm_label",
    "is_error",
    "parse_bool",
    "parse_date",
    "parse_money",
]
