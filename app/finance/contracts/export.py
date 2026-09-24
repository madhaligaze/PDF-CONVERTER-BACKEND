"""Выгрузка реестра в .xlsx — теми же листами, блоками и шапками, что в файле.

Страховка на переходный период и для тех, кому нужен файл. Лист выгрузки —
это отбор реестра: договор стоит в первом подходящем блоке листа, как стоял бы
в Excel. Шапка блока — подписи колонок из файла (у «Заказчик ГК / Заказчик ГК»
заказчик снова в F), текст соглашений — как был, даты — датами Excel, суммы —
числами. Цвета шапки исходного файла возвращаются в выгрузке: на экране их нет,
а в файле человек их ждёт.
"""
from __future__ import annotations

import io
import uuid
from datetime import date
from decimal import Decimal
from typing import Any, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance import history
from app.finance.contracts import views as views_module
from app.finance.contracts.models import Contract, EntityView
from app.finance.contracts.service import (
    Access,
    Actor,
    Registry,
    facts_of,
    people_of,
    visible_to,
)
from app.finance.models import Workspace

ROW_NUMBER = "row_number"
#: Колонки главного листа, если у листа своих колонок нет (реестр заведён
#: не из файла).
DEFAULT_KEYS = (
    ROW_NUMBER, "status", "folder_url", "planned_end_at", "people", "executor", "customer",
    "number", "signed_at", "department", "type", "subject", "amount", "paid_snapshot",
    "remaining_snapshot", "amendments_text", "amendments_summary_text", "end_date", "note",
)


def _text(registry: Registry, contract: Contract, key: str, people: Sequence[uuid.UUID]) -> Any:
    """Значение ячейки выгрузки: так, как его пишут в реестре."""
    if key in ("executor", "customer"):
        party_id = contract.executor_id if key == "executor" else contract.customer_id
        party = registry.parties.get(party_id) if party_id else None
        return party.name if party else None
    if key in ("type", "subject", "status", "economic_role"):
        value_id = getattr(contract, f"{key}_id")
        value = registry.values.get(value_id) if value_id else None
        return value.value if value else None
    if key == "department":
        department = registry.departments.get(contract.department_id) if contract.department_id else None
        return department.code if department else None
    if key == "people":
        names = [registry.employees[item].full_name for item in people if item in registry.employees]
        return ", ".join(names) or None
    if key == "amount":
        if contract.amount is not None:
            return float(contract.amount)
        return contract.amount_terms or None
    if key in ("paid_snapshot", "remaining_snapshot"):
        raw = (contract.file_snapshot or {}).get(key.replace("_snapshot", ""))
        try:
            return float(Decimal(str(raw))) if raw not in (None, "") else None
        except ArithmeticError:
            return raw
    if key in ("signed_at", "planned_end_at", "end_date"):
        return getattr(contract, key)
    if key in ("billing", "end_kind"):
        return getattr(contract, key) or None
    if key in ("number", "folder_url", "amendments_text", "amendments_summary_text", "note", "amount_terms", "currency"):
        return getattr(contract, key) or None
    raw = (contract.attrs or {}).get(key)
    field = registry.field_by_key.get(key)
    if raw in (None, "", []) or field is None:
        return None
    try:
        if field.type in ("list",):
            value = registry.values.get(uuid.UUID(str(raw)))
            return value.value if value else raw
        if field.type == "multi_list":
            return ", ".join(
                registry.values[uuid.UUID(item)].value for item in raw if uuid.UUID(item) in registry.values
            )
        if field.type == "person":
            return ", ".join(
                registry.employees[uuid.UUID(item)].full_name for item in raw if uuid.UUID(item) in registry.employees
            )
        if field.type in ("number", "money"):
            return float(Decimal(str(raw)))
        if field.type == "date":
            return date.fromisoformat(str(raw))
    except (ValueError, KeyError, ArithmeticError):
        return str(raw)
    return raw


def build(session: Session, workspace: Workspace, access: Access, actor: Actor, view_keys: Sequence[str] | None = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    registry = Registry(session, workspace)
    contracts = list(
        session.scalars(
            sa.select(Contract)
            .where(Contract.workspace_id == workspace.id, Contract.deleted_at.is_(None))
            .order_by(Contract.position, Contract.created_at)
        )
    )
    people = people_of(session, [item.id for item in contracts])
    visible = [item for item in contracts if visible_to(item, registry, access, people.get(item.id, []))]
    facts = {item.id: facts_of(item, registry, people.get(item.id, [])) for item in visible}
    hidden = set(access.hidden)

    views: list[EntityView] = [
        view for view in registry.views if not view_keys or view.key in view_keys
    ]
    book = Workbook()
    book.remove(book.active)
    bold = Font(bold=True)
    wrap = Alignment(wrap_text=True, vertical="top")
    for view in views:
        sheet = book.create_sheet(title=_sheet_title(view.title, book.sheetnames))
        fill_hex = (view.style or {}).get("header_fill") or ""
        fill = PatternFill("solid", start_color=fill_hex) if fill_hex else None
        row_index = 1
        taken: set[uuid.UUID] = set()
        widths: dict[int, float] = {}
        for number, block in enumerate(view.blocks or []):
            columns = [
                column for column in (block.get("columns") or []) if column.get("key") not in hidden
            ] or _default_columns(registry, hidden)
            members = [
                item
                for item in visible
                if item.id not in taken and views_module.matches(block.get("filter"), facts[item.id])
            ]
            taken.update(item.id for item in members)
            if block.get("title"):
                sheet.cell(row=row_index, column=2, value=block["title"]).font = bold
                row_index += 1
            for c_index, column in enumerate(columns, start=1):
                cell = sheet.cell(row=row_index, column=c_index, value=column.get("label") or _title(registry, column["key"]))
                cell.font = bold
                cell.alignment = wrap
                if fill is not None:
                    cell.fill = fill
                if column.get("width"):
                    widths[c_index] = max(widths.get(c_index, 0), float(column["width"]))
            header_row = row_index
            row_index += 1
            for position, item in enumerate(members, start=1):
                mine = people.get(item.id, [])
                for c_index, column in enumerate(columns, start=1):
                    key = column["key"]
                    value = position if key == ROW_NUMBER else _text(registry, item, key, mine)
                    cell = sheet.cell(row=row_index, column=c_index, value=value)
                    if isinstance(value, date):
                        cell.number_format = "DD.MM.YYYY"
                    elif isinstance(value, float) and key != ROW_NUMBER:
                        cell.number_format = "# ##0"
                    if isinstance(value, str) and "\n" in value:
                        cell.alignment = wrap
                row_index += 1
            if number == 0 and len(view.blocks or []) == 1:
                sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)
            row_index += 2
        for c_index, width in widths.items():
            sheet.column_dimensions[get_column_letter(c_index)].width = width
    if not book.sheetnames:
        book.create_sheet("Реестр")
    buffer = io.BytesIO()
    book.save(buffer)
    history.write(
        session,
        workspace,
        kind="contract.export",
        entity="contract",
        title=f"реестр выгружен в .xlsx: {len(visible)} договоров, листов {len(views)}",
        after={"views": [view.key for view in views], "contracts": len(visible)},
        actor=actor.email,
    )
    return buffer.getvalue()


def _default_columns(registry: Registry, hidden: set[str]) -> list[dict[str, Any]]:
    return [
        {"key": key, "label": "№" if key == ROW_NUMBER else _title(registry, key)}
        for key in DEFAULT_KEYS
        if key == ROW_NUMBER or (key in registry.field_by_key and key not in hidden)
    ]


def _title(registry: Registry, key: str) -> str:
    if key == ROW_NUMBER:
        return "№"
    field = registry.field_by_key.get(key)
    return field.title if field else key


def _sheet_title(title: str, taken: list[str]) -> str:
    clean = "".join(ch for ch in (title or "Лист") if ch not in "[]:*?/\\")[:31] or "Лист"
    candidate, index = clean, 2
    while candidate in taken:
        suffix = f" {index}"
        candidate = clean[: 31 - len(suffix)] + suffix
        index += 1
    return candidate


__all__ = ["build"]
