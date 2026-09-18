"""История действий и отмена.

Учёт ведут несколько человек, и вопрос «кто убрал эту операцию на два
миллиона» возникает раньше вопроса «сколько денег». Поэтому запись хранит не
описание («изменил операцию»), а **состояние до и после**.

Почему отмена — это возврат состояния, а не обратное действие
─────────────────────────────────────────────────────────────
Обратное действие («было списание — сделаем поступление») кажется проще и
врёт: в журнале появляется вторая операция, которой в жизни не было, отчёт за
период меняется дважды, а сверка с выпиской перестаёт сходиться. Возврат
состояния возвращает ровно то, что было, и в истории остаётся отметка, что
это была отмена.

`undone_at` не даёт отменить дважды: вторая отмена вернула бы уже отменённое и
выглядела бы как новая правка.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.finance.models import ActionLog, Operation, Workspace
from app.finance.service import FinanceError

#: Поля операции, которые история хранит и умеет вернуть. Список закрытый: то,
#: что в него не входит, отменой не восстановится, и лучше знать это заранее,
#: чем обнаружить при отмене.
OPERATION_FIELDS = (
    "kind",
    "status",
    "paid_at",
    "accrued_at",
    "amount",
    "currency",
    "rate",
    "account_from_id",
    "account_to_id",
    "category_id",
    "counterparty_id",
    "comment",
    "deleted_at",
)

TITLES = {
    "operation.create": "операция заведена",
    "operation.update": "операция изменена",
    "operation.delete": "операция убрана",
    "operation.settle": "ожидание закрыто оплатой",
    "invoice.create": "счёт выставлен",
    "invoice.void": "счёт отменён",
    "import.apply": "загрузка заведена",
    "rules.apply": "правила применены",
    "recurrence.create": "повторение создано",
    "recurrence.remove": "повторение удалено",
    "integration.create": "подключение создано",
    "integration.receive": "операции пришли из подключения",
}


def _plain(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def snapshot(operation: Operation) -> dict[str, Any]:
    """Состояние операции для истории."""
    return {field: _plain(getattr(operation, field, None)) for field in OPERATION_FIELDS}


def write(
    session: Session,
    workspace: Workspace,
    *,
    kind: str,
    entity: str,
    entity_id: uuid.UUID | None = None,
    title: str = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    actor: str = "",
) -> ActionLog:
    entry = ActionLog(
        workspace_id=workspace.id,
        actor=actor,
        kind=kind,
        entity=entity,
        entity_id=entity_id,
        title=title or TITLES.get(kind, kind),
        before=before or {},
        after=after or {},
    )
    session.add(entry)
    session.flush()
    return entry


def listing(
    session: Session, workspace_id: uuid.UUID, *, limit: int = 100
) -> list[dict[str, Any]]:
    rows = session.scalars(
        sa.select(ActionLog)
        .where(ActionLog.workspace_id == workspace_id)
        .order_by(ActionLog.at.desc())
        .limit(limit)
    )
    out: list[dict[str, Any]] = []
    for entry in rows:
        out.append(
            {
                "id": str(entry.id),
                "at": entry.at.isoformat() if entry.at else None,
                "actor": entry.actor,
                "kind": entry.kind,
                "entity": entry.entity,
                "entity_id": str(entry.entity_id) if entry.entity_id else None,
                "title": entry.title,
                "before": entry.before or {},
                "after": entry.after or {},
                "undone_at": entry.undone_at.isoformat() if entry.undone_at else None,
                # Отменить можно только то, что умеем вернуть: операции.
                "can_undo": entry.undone_at is None and entry.entity == "operation" and bool(entry.entity_id),
            }
        )
    return out


def _restore(operation: Operation, state: dict[str, Any]) -> None:
    for field in OPERATION_FIELDS:
        if field not in state:
            continue
        value = state[field]
        if field in ("paid_at", "accrued_at") and isinstance(value, str) and value:
            value = date.fromisoformat(value[:10])
        elif field == "deleted_at" and isinstance(value, str) and value:
            value = datetime.fromisoformat(value)
        elif field in ("amount", "rate") and value is not None:
            value = Decimal(str(value))
        elif field.endswith("_id") and isinstance(value, str) and value:
            value = uuid.UUID(value)
        setattr(operation, field, value)


def undo(
    session: Session, workspace: Workspace, entry_id: uuid.UUID, *, actor: str = ""
) -> dict[str, Any]:
    """Отменить действие, вернув состояние «до».

    Создание отменяется удалением (мягким — `deleted_at`), изменение и удаление
    — возвратом прежних полей. Пересчёт `amount_base` обязателен: без него
    отменённая правка суммы вернула бы сумму, но не её оценку в валюте
    компании, и отчёты показали бы третью цифру, которой не было никогда.
    """
    entry = session.get(ActionLog, entry_id)
    if entry is None or entry.workspace_id != workspace.id:
        raise FinanceError("Запись истории не найдена")
    if entry.undone_at is not None:
        raise FinanceError("Это действие уже отменено")
    if entry.entity != "operation" or not entry.entity_id:
        raise FinanceError("Отмена пока умеет только операции")

    operation = session.get(Operation, entry.entity_id)
    if operation is None:
        raise FinanceError("Операции больше нет — отменять нечего")

    if entry.kind == "operation.create":
        operation.deleted_at = datetime.now(timezone.utc)
    else:
        if not entry.before:
            raise FinanceError("В записи нет состояния «до»")
        _restore(operation, entry.before)

    operation.amount_base = (
        Decimal(str(operation.amount)) * Decimal(str(operation.rate))
    ).quantize(Decimal("0.01"))
    operation.version += 1
    entry.undone_at = datetime.now(timezone.utc)
    session.flush()

    write(
        session,
        workspace,
        kind="operation.undo",
        entity="operation",
        entity_id=operation.id,
        title=f"отменено: {entry.title}",
        before=entry.after or {},
        after=snapshot(operation),
        actor=actor,
    )
    return {"ok": True, "operation_id": str(operation.id)}


__all__ = ["OPERATION_FIELDS", "TITLES", "listing", "snapshot", "undo", "write"]
