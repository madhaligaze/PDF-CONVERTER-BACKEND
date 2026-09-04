"""Дашборд поверх внутренней книги — второй источник тех же цифр.

Зачем нужен переключатель, а не замена
──────────────────────────────────────
Работа переезжает во внутренние книги, но выключать Google одним днём нельзя, и
дело не в осторожности. Переключатель даёт то, чего замена не даёт вовсе:
возможность **сверить**. Открыл дебиторку по листу, открыл по книге, сравнил
итоги — и увидел своими глазами, что переезд ничего не потерял. Если цифры
разойдутся, это будет видно сразу, а не через квартал в отчёте.

И обратный ход остаётся: пока переключатель есть, неверная привязка колонки —
это неудобство на минуту, а не остановка работы отдела.

Почему разбор общий
───────────────────
Строка договора собирается одной и той же функцией `dataset.build_contract_row`
для обоих источников. Отличается только то, откуда берутся значения: из листа —
через `Layout` по заголовкам, отсюда — через привязки полей к ролям.

Иначе дашборд считал бы по двум источникам разными правилами и показывал два
разных ответа на один вопрос — оба одинаково уверенно.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bbc.dataset import ContractRow, assign_carry_in, build_contract_row
from app.books.models import Binding, BookField, BookRow, BookTable

log = logging.getLogger(__name__)

#: Колонка парсера BBC → роль каталога.
#:
#: Имена расходятся в четырёх местах, и это не небрежность: в каталоге роли
#: названы так, как их назвала бы любая компания («предмет договора», «долг»), а
#: в парсере — так, как называется колонка в книге BBC. Ровно эта таблица и есть
#: всё, что связывает общий модуль с конкретной компанией.
ROLE_BY_COLUMN: dict[str, str] = {
    "month": "month",
    "period_label": "period_label",
    "firm": "firm",
    "service_kind": "service_kind",
    "employee": "employee",
    "client": "client",
    "invoice_day": "invoice_day",
    "contract_amount": "contract_amount",
    "department": "department",
    "subject": "contract_subject",
    "signed_at": "signed_at",
    "contract_no": "contract_no",
    "addendum": "addendum",
    "period_start": "period_start",
    "period_end": "period_end",
    "saldo_start": "saldo_start",
    "invoiced": "invoiced",
    "invoice_no": "invoice_no",
    "invoice_date": "invoice_date",
    "paid_flag": "paid_flag",
    "paid_amount": "paid_amount",
    "pay_date_1": "payment_date_1",
    "pay_amount_1": "payment_amount_1",
    "pay_date_2": "payment_date_2",
    "pay_amount_2": "payment_amount_2",
    "pay_date_3": "payment_date_3",
    "pay_amount_3": "payment_amount_3",
    "avr_flag": "avr_flag",
    "avr_amount": "avr_amount",
    "avr_no": "avr_no",
    "avr_date": "avr_date",
    "avr_accepted": "avr_accepted",
    "esf_sent": "esf_sent",
    "saldo_end": "saldo_end",
    "status": "status",
    "diff_avr_paid": "diff_avr_paid",
    "debit_credit": "debt",
}

#: Роли, без которых считать дебиторку нечем. Вкладка, где их нет, мастером
#: быть не может — сколько бы прочих колонок в ней ни нашлось.
REQUIRED_ROLES = ("client", "contract_amount", "saldo_end")


class NoBookSource(RuntimeError):
    """Подходящей внутренней книги нет. Текст предназначен человеку."""


def _bindings(session: Session, table_id: UUID) -> dict[str, str]:
    """{ключ роли: ключ поля} для вкладки."""
    fields = {
        row.id: row.key
        for row in session.scalars(
            select(BookField).where(BookField.table_id == table_id)
        )
    }
    return {
        row.role_key: fields[row.field_id]
        for row in session.scalars(select(Binding).where(Binding.table_id == table_id))
        if row.field_id in fields
    }


def find_master_table(session: Session, workspace_id: UUID) -> tuple[BookTable, dict[str, str]]:
    """Вкладка, по которой можно считать дебиторку, и её привязки.

    Выбирается та, что покрывает больше всего ролей мастер-книги. «Больше
    всего» — не «хоть сколько-то»: вкладка без заказчика, суммы договора и
    сальдо не мастер, и подставлять её значило бы показать дашборд, посчитанный
    по журналу платежей.
    """
    tables = session.scalars(
        select(BookTable).where(
            BookTable.workspace_id == workspace_id, BookTable.deleted_at.is_(None)
        )
    ).all()

    best: tuple[int, BookTable, dict[str, str]] | None = None
    for table in tables:
        bound = _bindings(session, table.id)
        if not all(role in bound for role in REQUIRED_ROLES):
            continue
        covered = sum(1 for role in ROLE_BY_COLUMN.values() if role in bound)
        if best is None or covered > best[0]:
            best = (covered, table, bound)

    if best is None:
        raise NoBookSource(
            "Ни одна внутренняя книга не размечена под дебиторку: нужны роли "
            "«Заказчик», «Сумма договора» и «Сальдо конец». Разложите колонки "
            "на табло привязок в разделе «Книги»."
        )
    return best[1], best[2]


def rows_from_books(session: Session, workspace_id: UUID) -> tuple[list[ContractRow], dict[str, Any]]:
    """Строки дашборда из внутренней книги. Возвращает строки и описание источника."""
    table, bound = find_master_table(session, workspace_id)
    field_by_column = {
        column: bound[role] for column, role in ROLE_BY_COLUMN.items() if role in bound
    }

    rows = session.scalars(
        select(BookRow)
        .where(
            BookRow.table_id == table.id,
            BookRow.deleted_at.is_(None),
            BookRow.state == "live",
        )
        .order_by(BookRow.position)
    ).all()

    result: list[ContractRow] = []
    for index, row in enumerate(rows, start=2):
        values = row.values or {}

        def cell(column: str, _values: dict[str, Any] = values) -> str:
            key = field_by_column.get(column)
            if key is None:
                return ""
            value = _values.get(key)
            return "" if value is None else str(value)

        def has(column: str) -> bool:
            return column in field_by_column

        result.append(build_contract_row(index, cell=cell, has=has))

    assign_carry_in(result)

    missing = [
        column for column in ROLE_BY_COLUMN if column not in field_by_column
    ]
    return result, {
        "table_id": str(table.id),
        "table": table.name,
        "rows": len(result),
        "covered": len(field_by_column),
        # Не привязанные колонки — не ошибка, но и не мелочь: без «Дебет /
        # Кредит» долг посчитается по сальдо, и цифра будет другой. Пусть это
        # видно на экране, а не выясняется сравнением итогов.
        "missing": missing,
    }


__all__ = [
    "REQUIRED_ROLES",
    "ROLE_BY_COLUMN",
    "NoBookSource",
    "find_master_table",
    "rows_from_books",
]
