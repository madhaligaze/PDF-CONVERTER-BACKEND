"""Типы значений полей: разбор, проекция и показ.

Модуль намеренно чистый — ни базы, ни сети, ни настроек. Из-за этого таблицу
поведения можно проверить тестами за миллисекунды, а не поднятым Postgres, и
она проверяется целиком, а не по одному случаю на удачу.

Три операции над значением, и путать их нельзя:

* `parse` — сырая ячейка → типизированное значение или `None`. `None` значит
  «не разобралось», а не «ноль».
* `project` — типизированное значение → строка `row_facts`, из которой считает
  дашборд. Только здесь значение раскладывается по типизированным колонкам.
* `render` — значение → текст для показа и выгрузки.

Почему `None`, а не подстановка нуля
────────────────────────────────────
Пустая ячейка и неразобранная ячейка — разные вещи. Ноль складывается в сумму
и выглядит как факт: строка «оплата 0» неотличима от «оплату не смогли
прочитать». В книгах, которые ведут руками, второе встречается регулярно —
текст в денежной колонке, сломанная формула, «уточнить» вместо суммы. Поэтому
неразобранное остаётся видимым как неразобранное, и расчёт такую строку
исключает и говорит об этом.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.core.scalars import clean, is_error, parse_bool, parse_date, parse_decimal

#: Типы полей. Порядок значим: он же порядок проверок при определении типа —
#: от самого узкого к самому широкому, и `text` забирает всё, что не опознано.
FIELD_TYPES: tuple[str, ...] = (
    "bool",
    "date",
    "money",
    "number",
    "enum",
    "formula",
    "text",
    "unknown",
)

#: Типы, значения которых участвуют в арифметике.
NUMERIC_TYPES = frozenset({"money", "number"})

#: Узкий неразрывный пробел — разделитель разрядов при показе.
#:
#: Записан escape-последовательностью намеренно. Раньше он стоял в исходнике
#: самим символом и был неотличим от обычного пробела: тест сравнивал вывод с
#: «1 234 567,89», падал, и по глазам понять было нечего. Невидимый символ в
#: коде — это загадка, оставленная следующему читателю.
THIN_NBSP = " "


@dataclass(frozen=True)
class Fact:
    """Значение, разложенное по типизированным колонкам `row_facts`.

    Ровно одна из колонок заполнена — кроме `text_value`, который заполняется
    всегда: по нему работают поиск и группировка, и для денег он тоже нужен
    (сгруппировать по сумме — законный запрос).
    """

    num_value: Decimal | None = None
    date_value: date | None = None
    text_value: str | None = None
    bool_value: bool | None = None

    @property
    def empty(self) -> bool:
        return (
            self.num_value is None
            and self.date_value is None
            and self.bool_value is None
            and not self.text_value
        )


def parse(field_type: str, raw: Any) -> Any | None:
    """Сырая ячейка → значение объявленного типа. `None` — не разобралось.

    Ошибки формул (`#REF!`, `#N/A`) считаются пустотой для любого типа, кроме
    `text`: в текстовой колонке они хотя бы видны человеку как есть, а в
    денежной превратились бы в ноль.
    """
    if field_type == "text" or field_type == "unknown":
        return clean(raw) or None
    if is_error(raw):
        return None
    if field_type in NUMERIC_TYPES:
        return parse_decimal(raw)
    if field_type == "date":
        return parse_date(raw)
    if field_type == "bool":
        return parse_bool(raw)
    if field_type == "enum":
        return clean(raw) or None
    if field_type == "formula":
        # Значение формулы уже вычислено книгой — храним результат, а не текст.
        return clean(raw) or None
    return clean(raw) or None


def project(field_type: str, raw: Any) -> Fact:
    """Ячейка → строка проекции. Именно отсюда дашборд берёт числа."""
    value = parse(field_type, raw)
    if value is None:
        return Fact()

    if field_type in NUMERIC_TYPES:
        return Fact(num_value=value, text_value=render(field_type, value))
    if field_type == "date":
        return Fact(date_value=value, text_value=value.isoformat())
    if field_type == "bool":
        return Fact(bool_value=value, text_value="да" if value else "нет")
    return Fact(text_value=str(value))


def render(field_type: str, value: Any) -> str:
    """Значение → текст для показа. Пусто, если значения нет."""
    if value is None:
        return ""
    if field_type in NUMERIC_TYPES:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
        quantized = amount.quantize(Decimal("0.01")) if field_type == "money" else amount
        return f"{quantized:,}".replace(",", THIN_NBSP).replace(".", ",")
    if field_type == "date" and isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    if field_type == "bool":
        return "да" if value else "нет"
    return str(value)


def coerce_for_storage(field_type: str, raw: Any) -> Any:
    """Значение в том виде, в каком оно ложится в `rows.values` (jsonb).

    jsonb не умеет ни `Decimal`, ни `date`, а хранить их строкой в чужом
    формате — значит потерять их при следующем чтении. Поэтому: даты в ISO,
    суммы строкой с точкой (`Decimal` в float переводить нельзя — это ровно та
    потеря копеек, ради которой заведён `numeric`), остальное как есть.

    Нерпазобранное сохраняется **как есть, сырым текстом**, а не выбрасывается:
    человек должен увидеть в гриде то, что написано в книге, даже если это
    «уточнить у Айгуль» в колонке с суммой.
    """
    value = parse(field_type, raw)
    if value is None:
        text = clean(raw)
        return text or None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


__all__ = [
    "FIELD_TYPES",
    "NUMERIC_TYPES",
    "Fact",
    "coerce_for_storage",
    "parse",
    "project",
    "render",
]
