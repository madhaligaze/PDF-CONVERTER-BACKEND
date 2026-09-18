"""Работа с данными раздела «Финансы»: справочники, операции, импорт.

Здесь нет ни одного расчёта отчётов — они в `reports.py`. Разделение не
формальное: справочники и операции меняют правду, отчёты только читают, и
смешивать эти две ответственности в одном файле означает однажды написать
отчёт, который что-то поправил «по дороге».

Про версии правок
─────────────────
У операции есть `version`. Правка присылает ту версию, которую видела, и если
в базе уже другая — правка отклоняется с 409, а не перетирает чужую работу.
Это не теория: в разделе будут работать несколько финансистов одновременно, а
табличный вид отправляет правку по каждой ячейке, то есть шансов разъехаться
больше, чем у формы.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.books.layout import norm
from app.finance.config import finance_settings
from app.finance.importing import ParsedRow, Preview
from app.finance.models import (
    POSITION_STEP,
    Account,
    Category,
    Counterparty,
    ImportBatch,
    ImportRow,
    Operation,
    OperationCategory,
    OperationProject,
    OperationTag,
    Plan,
    Project,
    Tag,
    Workspace,
)

log = logging.getLogger(__name__)

DEFAULT_SLUG = "default"


class FinanceError(RuntimeError):
    """Действие невозможно по смыслу: нет счёта, сумма не та, версия устарела."""


class VersionConflict(FinanceError):
    """Операцию уже поправил кто-то другой."""


# ── Пространство и первичное наполнение ──────────────────────────────────────


#: Счета, с которых начинают почти все. Заводятся при создании пространства,
#: потому что пустой раздел без счетов не даёт сделать ни одного действия: ни
#: записать операцию, ни загрузить выписку.
SEED_ACCOUNTS = (
    ("Банковский счёт", "bank"),
    ("Касса", "cash"),
)

#: Статьи, которые в управленческом учёте называются одинаково почти везде.
#: Это не «правильный» список, а стартовый: его переименуют под себя.
SEED_CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("income", "Выручка", ""),
    ("income", "Прочий доход", ""),
    ("income", "Получение кредита", "loan_in"),
    ("income", "Возврат займа", "loan_return"),
    ("expense", "Аренда", ""),
    ("expense", "Зарплата", ""),
    ("expense", "Закуп товара", ""),
    ("expense", "Услуги подрядчиков", ""),
    ("expense", "Налоги и сборы", "tax"),
    ("expense", "Погашение кредита", "loan_out"),
    ("expense", "Дивиденды", "dividend"),
)

#: Природа начальных статей. Отдельным словарём, а не четвёртым полем
#: кортежа: так её видно одним взглядом, и ошибка «кредит посчитан выручкой»
#: ловится чтением, а не отчётом.
SEED_NATURES: dict[str, str] = {
    "Выручка": "revenue",
    "Прочий доход": "other",
    "Получение кредита": "capital",
    "Возврат займа": "capital",
    "Закуп товара": "cogs",
    "Налоги и сборы": "tax",
    "Погашение кредита": "capital",
    "Дивиденды": "capital",
}


def create_workspace(session: Session, *, title: str) -> Workspace:
    """Новая компания с начальными справочниками.

    Зовётся из регистрации и из «добавить ещё одну компанию». Пустая компания
    без счетов не даёт сделать ни одного действия — ни записать операцию, ни
    загрузить выписку, — поэтому счета и статьи заводятся сразу.

    `slug` собирается из названия и номера: он нужен только как человекочитаемый
    ключ в адресах и логах, уникальность держится счётчиком, а не транслитом.
    """
    clean = (title or "").strip() or "Компания"
    base = re.sub(r"[^a-z0-9]+", "-", clean.lower()).strip("-") or "company"
    slug = base
    suffix = 2
    while session.scalar(sa.select(Workspace.id).where(Workspace.slug == slug)) is not None:
        slug = f"{base}-{suffix}"
        suffix += 1

    workspace = Workspace(
        slug=slug, title=clean, base_currency=finance_settings.base_currency
    )
    session.add(workspace)
    session.flush()
    _seed(session, workspace)
    log.info("finance: создана компания «%s» (%s)", clean, slug)
    return workspace


def get_workspace(session: Session, workspace_id: uuid.UUID) -> Workspace:
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise FinanceError("Компания не найдена")
    return workspace


def rename_workspace(session: Session, workspace: Workspace, *, title: str) -> Workspace:
    clean = (title or "").strip()
    if len(clean) < 2:
        raise FinanceError("У компании должно быть название")
    workspace.title = clean
    session.flush()
    return workspace


def ensure_workspace(session: Session, *, slug: str = DEFAULT_SLUG) -> Workspace:
    """Компания по умолчанию — для тестов и для данных, заведённых до учёток.

    Осталась намеренно, хотя компаний теперь много. Во-первых, на ней стоят
    полсотни тестов, и переписывать их на регистрацию значило бы проверять
    авторизацию там, где проверяют отчёты. Во-вторых, данные, заведённые до
    появления учёток, лежат именно в компании `default`, и путь к ним должен
    остаться.

    В маршрутах этой функции быть не должно: там компания берётся из сессии.
    Это проверяется тестом `test_finance_routes_take_company_from_session`.

    Про гонку на первом открытии
    ────────────────────────────
    Раздел открывается двумя запросами сразу — сводка и справочники, — и до
    исправления оба видели пустую базу и оба заводили пространство. Второй
    падал на уникальном ключе `slug`, отвечал 500, и экран оставался пустым:
    «Требуется вход» в панели, пустые справочники, ни одной подсказки, что
    делать. Поймано сквозным прогоном 18 сентября 2026; в проде это выглядело
    бы как «раздел не работает у всех, кто зашёл первым».

    Поэтому вставка идёт во вложенной транзакции: проиграли гонку — откатываем
    только её и перечитываем то, что создал сосед. К этому моменту его
    транзакция уже завершена (на конфликте уникального ключа Postgres держит
    нас на блокировке до его фиксации), поэтому перечитывание видит и
    пространство, и его начальные справочники.
    """
    workspace = session.scalar(sa.select(Workspace).where(Workspace.slug == slug))
    if workspace is not None:
        return workspace

    workspace = Workspace(
        slug=slug, title="Финансы компании", base_currency=finance_settings.base_currency
    )
    try:
        with session.begin_nested():
            session.add(workspace)
            session.flush()
    except IntegrityError:
        existing = session.scalar(sa.select(Workspace).where(Workspace.slug == slug))
        if existing is None:
            # Конфликт был, а пространства нет — это уже не гонка, а что-то
            # другое, и молчать об этом нельзя.
            raise
        log.info("finance: пространство «%s» создал параллельный запрос", slug)
        return existing

    _seed(session, workspace)
    log.info("finance: создано пространство «%s» с начальными справочниками", slug)
    return workspace


def _seed(session: Session, workspace: Workspace) -> None:
    """Начальные счета и статьи новой компании."""
    for position, (name, kind) in enumerate(SEED_ACCOUNTS):
        session.add(
            Account(
                workspace_id=workspace.id,
                name=name,
                normalized_name=norm(name),
                kind=kind,
                currency=workspace.base_currency,
                starting_balance=Decimal("0"),
                position=position * POSITION_STEP,
            )
        )
    for position, (side, name, system_key) in enumerate(SEED_CATEGORIES):
        session.add(
            Category(
                workspace_id=workspace.id,
                side=side,
                name=name,
                normalized_name=norm(name),
                system_key=system_key,
                nature=SEED_NATURES.get(name, "operating"),
                position=position * POSITION_STEP,
            )
        )
    session.flush()


# ── Справочники ──────────────────────────────────────────────────────────────


def _next_position(session: Session, model, workspace_id: uuid.UUID) -> int:
    top = session.scalar(
        sa.select(sa.func.max(model.position)).where(model.workspace_id == workspace_id)
    )
    return int(top or 0) + POSITION_STEP


def list_accounts(session: Session, workspace_id: uuid.UUID, *, with_archived: bool = False) -> list[Account]:
    query = sa.select(Account).where(Account.workspace_id == workspace_id)
    if not with_archived:
        query = query.where(Account.archived_at.is_(None))
    return list(session.scalars(query.order_by(Account.position, Account.name)))


def create_account(
    session: Session,
    workspace: Workspace,
    *,
    name: str,
    kind: str = "bank",
    currency: str | None = None,
    starting_balance: Decimal | float | str = 0,
    excluded_from_reports: bool = False,
) -> Account:
    clean = (name or "").strip()
    if not clean:
        raise FinanceError("У счёта должно быть название")
    exists = session.scalar(
        sa.select(Account).where(
            Account.workspace_id == workspace.id, Account.normalized_name == norm(clean)
        )
    )
    if exists is not None:
        raise FinanceError(f"Счёт «{clean}» уже есть")
    account = Account(
        workspace_id=workspace.id,
        name=clean,
        normalized_name=norm(clean),
        kind=kind,
        currency=(currency or workspace.base_currency).upper(),
        starting_balance=Decimal(str(starting_balance or 0)),
        excluded_from_reports=excluded_from_reports,
        position=_next_position(session, Account, workspace.id),
    )
    session.add(account)
    session.flush()
    return account


def _find_or_create(
    session: Session,
    model,
    workspace_id: uuid.UUID,
    name: str,
    *,
    extra: dict[str, Any] | None = None,
    create: bool = True,
):
    """Найти запись справочника по названию или создать её.

    Сравнение по `normalized_name`: «PW Клиент», «pw клиент» и «PW  Клиент» —
    одна и та же запись. Иначе справочник за месяц зарастает двойниками, и в
    отчёте один контрагент оказывается тремя.
    """
    clean = (name or "").strip()
    if not clean:
        return None
    conditions = [model.workspace_id == workspace_id, model.normalized_name == norm(clean)]
    for key, value in (extra or {}).items():
        conditions.append(getattr(model, key) == value)
    found = session.scalar(sa.select(model).where(*conditions))
    if found is not None or not create:
        return found
    created = model(
        workspace_id=workspace_id,
        name=clean,
        normalized_name=norm(clean),
        **(extra or {}),
    )
    if hasattr(created, "position"):
        created.position = _next_position(session, model, workspace_id)
    session.add(created)
    session.flush()
    return created


def find_entry(session: Session, model, workspace_id: uuid.UUID, name: str, **extra):
    """Найти запись справочника по названию, ничего не создавая.

    Отдельным именем, а не `create=False` у `_find_or_create`: табличный вид
    зовёт это на каждую правку ячейки, и вызов обязан читаться как «найди», а
    не как «создай, но не сейчас».
    """
    return _find_or_create(session, model, workspace_id, name, extra=extra or None, create=False)


def ensure_category(session: Session, workspace_id: uuid.UUID, side: str, name: str, *, create: bool = True):
    return _find_or_create(session, Category, workspace_id, name, extra={"side": side}, create=create)


def ensure_counterparty(
    session: Session, workspace_id: uuid.UUID, name: str, *, role: str = "client", create: bool = True
):
    return _find_or_create(session, Counterparty, workspace_id, name, extra={"role": role}, create=create)


def ensure_project(session: Session, workspace_id: uuid.UUID, name: str, *, create: bool = True):
    return _find_or_create(session, Project, workspace_id, name, create=create)


def ensure_tag(session: Session, workspace_id: uuid.UUID, name: str, *, create: bool = True):
    return _find_or_create(session, Tag, workspace_id, name, create=create)


def list_categories(session: Session, workspace_id: uuid.UUID, side: str | None = None) -> list[Category]:
    query = sa.select(Category).where(
        Category.workspace_id == workspace_id, Category.archived_at.is_(None)
    )
    if side:
        query = query.where(Category.side == side)
    return list(session.scalars(query.order_by(Category.side, Category.position, Category.name)))


def list_counterparties(session: Session, workspace_id: uuid.UUID, role: str | None = None) -> list[Counterparty]:
    query = sa.select(Counterparty).where(
        Counterparty.workspace_id == workspace_id, Counterparty.archived_at.is_(None)
    )
    if role:
        query = query.where(Counterparty.role == role)
    return list(session.scalars(query.order_by(Counterparty.position, Counterparty.name)))


def list_projects(session: Session, workspace_id: uuid.UUID) -> list[Project]:
    return list(
        session.scalars(
            sa.select(Project)
            .where(Project.workspace_id == workspace_id, Project.archived_at.is_(None))
            .order_by(Project.position, Project.name)
        )
    )


def list_tags(session: Session, workspace_id: uuid.UUID) -> list[Tag]:
    return list(
        session.scalars(
            sa.select(Tag)
            .where(Tag.workspace_id == workspace_id, Tag.archived_at.is_(None))
            .order_by(Tag.name)
        )
    )


def archive(session: Session, model, workspace_id: uuid.UUID, item_id: uuid.UUID) -> None:
    """Убрать запись справочника из списков, не удаляя.

    Удалять нельзя: на записи ссылаются операции, и удаление либо оборвёт
    ссылку, либо потребует переписать историю. «Архивная» запись исчезает из
    выпадающих списков, но прошлые отчёты остаются целыми.
    """
    item = session.get(model, item_id)
    if item is None or item.workspace_id != workspace_id:
        raise FinanceError("Запись не найдена")
    item.archived_at = datetime.now(timezone.utc)
    session.flush()


# ── Операции ─────────────────────────────────────────────────────────────────


@dataclass
class OperationInput:
    """Поля операции, приходящие из формы или из таблицы."""

    kind: str
    paid_at: date
    amount: Decimal
    status: str = "fact"
    accrued_at: date | None = None
    period_start: date | None = None
    period_end: date | None = None
    currency: str | None = None
    rate: Decimal | None = None
    account_from_id: uuid.UUID | None = None
    account_to_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    comment: str = ""
    projects: Sequence[tuple[uuid.UUID, Decimal]] = ()
    #: Дробление по статьям: один платёж на несколько статей.
    categories: Sequence[tuple[uuid.UUID, Decimal]] = ()
    tags: Sequence[uuid.UUID] = ()
    #: Откуда операция родилась, если не руками.
    recurrence_id: uuid.UUID | None = None
    integration_id: uuid.UUID | None = None
    source: str = "app"
    external_key: str | None = None
    raw: dict[str, Any] | None = None
    import_batch_id: uuid.UUID | None = None


def _validate(session: Session, workspace: Workspace, data: OperationInput) -> None:
    if data.amount is None or Decimal(data.amount) < 0:
        raise FinanceError("Сумма не может быть отрицательной: направление задаёт вид операции")
    if data.kind == "transfer":
        if not (data.account_from_id and data.account_to_id):
            raise FinanceError("Для перевода нужны оба счёта")
        if data.account_from_id == data.account_to_id:
            raise FinanceError("Перевод на тот же счёт ничего не меняет")
    # У ожидания счёта может не быть, и это не пробел в данных.
    #
    # Счёт клиенту выставляют, не зная, на какой из своих счетов придут деньги:
    # это выяснится в момент оплаты. Требовать счёт заранее значит заставить
    # выбрать наугад — а потом сверять выписку с угаданным. Для факта счёт
    # обязателен по-прежнему: деньги всегда откуда-то и куда-то двигаются.
    if data.status == "plan":
        return
    if data.kind == "income" and not data.account_to_id:
        raise FinanceError("Не указано, на какой счёт пришли деньги")
    if data.kind == "expense" and not data.account_from_id:
        raise FinanceError("Не указано, с какого счёта ушли деньги")
    for account_id in (data.account_from_id, data.account_to_id):
        if account_id is None:
            continue
        account = session.get(Account, account_id)
        if account is None or account.workspace_id != workspace.id:
            raise FinanceError("Счёт не найден")
    if data.category_id is not None:
        category = session.get(Category, data.category_id)
        if category is None or category.workspace_id != workspace.id:
            raise FinanceError("Категория не найдена")
        if data.kind != "transfer":
            expected = "income" if data.kind == "income" else "expense"
            if category.side != expected:
                raise FinanceError(
                    f"Категория «{category.name}» относится к другой стороне учёта: "
                    f"это {'доход' if category.side == 'income' else 'расход'}"
                )


def _split_state(amount: Decimal, splits: Sequence[tuple[uuid.UUID, Decimal]]) -> str:
    if not splits:
        return "none"
    total = sum((Decimal(str(value)) for _pid, value in splits), Decimal("0"))
    if total == amount:
        return "exact"
    if total > amount:
        return "mismatch"
    return "partial"


def _write_category_splits(
    session: Session, operation: Operation, splits: Sequence[tuple[uuid.UUID, Decimal]]
) -> None:
    """Переписать дробление операции по статьям.

    Части всегда положительные — знак задаёт вид операции. Сумма частей больше
    суммы операции запрещена: это не «частичное разнесение», а ошибка, и
    показать её надо в момент ввода, а не в отчёте через месяц. Меньше —
    можно: остаток остаётся на статье самой операции.

    Когда части заданы, `category_id` операции не отменяется: на нём остаётся
    неразнесённый остаток, и отчёт складывает и то и то.
    """
    session.execute(
        sa.delete(OperationCategory).where(OperationCategory.operation_id == operation.id)
    )
    if not splits:
        return
    total = Decimal("0")
    for category_id, value in splits:
        amount = Decimal(str(value))
        if amount <= 0:
            raise FinanceError("Часть платежа не может быть нулевой или отрицательной")
        total += amount
        session.add(
            OperationCategory(
                operation_id=operation.id, category_id=category_id, amount=amount
            )
        )
    if total > Decimal(str(operation.amount)):
        raise FinanceError(
            f"Части по статьям ({total}) больше суммы операции ({operation.amount})"
        )
    session.flush()


def create_operation(
    session: Session, workspace: Workspace, data: OperationInput, *, actor: str = ""
) -> Operation:
    _validate(session, workspace, data)
    amount = Decimal(str(data.amount))
    rate = Decimal(str(data.rate)) if data.rate is not None else Decimal("1")
    currency = (data.currency or workspace.base_currency).upper()
    operation = Operation(
        workspace_id=workspace.id,
        kind=data.kind,
        status=data.status,
        paid_at=data.paid_at,
        accrued_at=data.accrued_at,
        period_start=data.period_start,
        period_end=data.period_end,
        amount=amount,
        currency=currency,
        # Сумма в валюте компании считается сейчас и хранится: отчёт за июнь,
        # открытый в сентябре, обязан показывать те же цифры.
        amount_base=(amount * rate).quantize(Decimal("0.01")),
        rate=rate,
        account_from_id=data.account_from_id,
        account_to_id=data.account_to_id,
        category_id=data.category_id if data.kind != "transfer" else None,
        counterparty_id=data.counterparty_id,
        comment=data.comment or "",
        split_state=_split_state(amount, data.projects),
        source=data.source,
        recurrence_id=data.recurrence_id,
        integration_id=data.integration_id,
        external_key=data.external_key,
        import_batch_id=data.import_batch_id,
        raw=data.raw or {},
        created_by=actor,
    )
    session.add(operation)
    session.flush()
    for project_id, value in data.projects:
        session.add(
            OperationProject(operation_id=operation.id, project_id=project_id, amount=Decimal(str(value)))
        )
    _write_category_splits(session, operation, data.categories)
    for tag_id in data.tags:
        session.add(OperationTag(operation_id=operation.id, tag_id=tag_id))
    session.flush()
    return operation


def update_operation(
    session: Session,
    workspace: Workspace,
    operation_id: uuid.UUID,
    changes: dict[str, Any],
    *,
    version: int | None = None,
    actor: str = "",
) -> Operation:
    """Правка полей операции. `version` — та, что видел правящий."""
    operation = session.get(Operation, operation_id)
    if operation is None or operation.workspace_id != workspace.id or operation.deleted_at is not None:
        raise FinanceError("Операция не найдена")
    if version is not None and operation.version != version:
        raise VersionConflict(
            f"Операцию уже изменили: у вас версия {version}, в базе {operation.version}. "
            "Обновите страницу, чтобы не затереть чужую правку."
        )

    projects = changes.pop("projects", None)
    categories = changes.pop("categories", None)
    tags = changes.pop("tags", None)
    for key, value in changes.items():
        if not hasattr(operation, key):
            raise FinanceError(f"Неизвестное поле «{key}»")
        setattr(operation, key, value)

    if projects is not None:
        session.execute(
            sa.delete(OperationProject).where(OperationProject.operation_id == operation.id)
        )
        for project_id, value in projects:
            session.add(
                OperationProject(
                    operation_id=operation.id, project_id=project_id, amount=Decimal(str(value))
                )
            )
        operation.split_state = _split_state(Decimal(str(operation.amount)), projects)
    if categories is not None:
        _write_category_splits(session, operation, categories)
    if tags is not None:
        session.execute(sa.delete(OperationTag).where(OperationTag.operation_id == operation.id))
        for tag_id in tags:
            session.add(OperationTag(operation_id=operation.id, tag_id=tag_id))

    data = OperationInput(
        kind=operation.kind,
        paid_at=operation.paid_at,
        amount=Decimal(str(operation.amount)),
        account_from_id=operation.account_from_id,
        account_to_id=operation.account_to_id,
        category_id=operation.category_id,
    )
    _validate(session, workspace, data)
    operation.amount_base = (Decimal(str(operation.amount)) * Decimal(str(operation.rate))).quantize(
        Decimal("0.01")
    )
    operation.version += 1
    session.flush()
    return operation


def delete_operation(
    session: Session, workspace: Workspace, operation_id: uuid.UUID, *, actor: str = ""
) -> None:
    """Пометить операцию удалённой.

    Мягко, а не `DELETE`: удалённая операция обязана остаться видимой в истории
    действий, иначе исчезновение денег из отчёта нельзя объяснить.
    """
    operation = session.get(Operation, operation_id)
    if operation is None or operation.workspace_id != workspace.id:
        raise FinanceError("Операция не найдена")
    operation.deleted_at = datetime.now(timezone.utc)
    operation.version += 1
    session.flush()


@dataclass
class OperationFilter:
    """Фильтр журнала. Пустые поля ничего не сужают."""

    date_from: date | None = None
    date_to: date | None = None
    kinds: Sequence[str] = ()
    statuses: Sequence[str] = ()
    account_ids: Sequence[uuid.UUID] = ()
    category_ids: Sequence[uuid.UUID] = ()
    counterparty_ids: Sequence[uuid.UUID] = ()
    project_ids: Sequence[uuid.UUID] = ()
    search: str = ""
    amount_from: Decimal | None = None
    amount_to: Decimal | None = None
    #: По какой дате фильтровать: платежа или сделки.
    by: str = "paid"


def _apply_filter(query, workspace_id: uuid.UUID, flt: OperationFilter):
    column = Operation.paid_at if flt.by == "paid" else sa.func.coalesce(
        Operation.accrued_at, Operation.paid_at
    )
    query = query.where(Operation.workspace_id == workspace_id, Operation.deleted_at.is_(None))
    if flt.date_from:
        query = query.where(column >= flt.date_from)
    if flt.date_to:
        query = query.where(column <= flt.date_to)
    if flt.kinds:
        query = query.where(Operation.kind.in_(list(flt.kinds)))
    if flt.statuses:
        query = query.where(Operation.status.in_(list(flt.statuses)))
    if flt.account_ids:
        ids = list(flt.account_ids)
        query = query.where(
            sa.or_(Operation.account_from_id.in_(ids), Operation.account_to_id.in_(ids))
        )
    if flt.category_ids:
        query = query.where(Operation.category_id.in_(list(flt.category_ids)))
    if flt.counterparty_ids:
        query = query.where(Operation.counterparty_id.in_(list(flt.counterparty_ids)))
    if flt.project_ids:
        query = query.where(
            Operation.id.in_(
                sa.select(OperationProject.operation_id).where(
                    OperationProject.project_id.in_(list(flt.project_ids))
                )
            )
        )
    if flt.amount_from is not None:
        query = query.where(Operation.amount >= flt.amount_from)
    if flt.amount_to is not None:
        query = query.where(Operation.amount <= flt.amount_to)
    if flt.search:
        like = f"%{flt.search.strip()}%"
        query = query.where(Operation.comment.ilike(like))
    return query


def list_operations(
    session: Session,
    workspace_id: uuid.UUID,
    flt: OperationFilter | None = None,
    *,
    limit: int = 250,
    offset: int = 0,
) -> tuple[list[Operation], int]:
    flt = flt or OperationFilter()
    base = _apply_filter(sa.select(Operation), workspace_id, flt)
    total = session.scalar(
        _apply_filter(sa.select(sa.func.count(Operation.id)), workspace_id, flt)
    )
    rows = list(
        session.scalars(
            base.order_by(Operation.paid_at.desc(), Operation.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return rows, int(total or 0)


def operation_projects(session: Session, operation_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[OperationProject]]:
    if not operation_ids:
        return {}
    rows = session.scalars(
        sa.select(OperationProject).where(OperationProject.operation_id.in_(list(operation_ids)))
    )
    grouped: dict[uuid.UUID, list[OperationProject]] = {}
    for row in rows:
        grouped.setdefault(row.operation_id, []).append(row)
    return grouped


def operation_tags(session: Session, operation_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    if not operation_ids:
        return {}
    rows = session.execute(
        sa.select(OperationTag.operation_id, OperationTag.tag_id).where(
            OperationTag.operation_id.in_(list(operation_ids))
        )
    )
    grouped: dict[uuid.UUID, list[uuid.UUID]] = {}
    for operation_id, tag_id in rows:
        grouped.setdefault(operation_id, []).append(tag_id)
    return grouped


# ── Импорт ───────────────────────────────────────────────────────────────────


def save_preview(
    session: Session, workspace: Workspace, preview: Preview, *, actor: str = ""
) -> ImportBatch:
    """Сохранить разбор файла как партию импорта, ничего ещё не заводя.

    Партия сохраняется до применения намеренно: человек уходит разбираться с
    десятью строками, возвращается через час и не должен загружать файл заново.
    """
    # Прошлый незавершённый разбор ТОГО ЖЕ источника снимается.
    #
    # Ответ на вопрос раздела («на какой счёт?», «как записаны даты?») — это
    # повторный разбор, то есть новая партия. Без уборки в истории оставались
    # две-три записи «разобрано, не заведено» на один файл, и человек не знал,
    # какую из них продолжать. Найдено живым прогоном 18 сентября.
    stale = session.scalars(
        sa.select(ImportBatch).where(
            ImportBatch.workspace_id == workspace.id,
            ImportBatch.file_name == preview.file_name,
            ImportBatch.status == "preview",
        )
    ).all()
    for old_batch in stale:
        session.execute(sa.delete(ImportRow).where(ImportRow.batch_id == old_batch.id))
        session.delete(old_batch)
    if stale:
        session.flush()

    counts = preview.counts
    batch = ImportBatch(
        workspace_id=workspace.id,
        file_name=preview.file_name,
        status="preview",
        mapping=preview.mapping,
        decisions={
            "date_order": preview.date_reading.order,
            "date_evidence": preview.date_reading.evidence,
            "header_line": preview.header_line,
            "unused_columns": preview.unused_columns,
            "accounts_missing": preview.accounts_missing,
        },
        rows_total=counts["total"],
        created_by=actor,
    )
    session.add(batch)
    session.flush()

    known = _known_keys(session, workspace.id)
    duplicates = 0
    for row in preview.rows:
        state = row.state
        if state == "imported" and row.values.get("external_key") in known:
            state = "duplicate"
            duplicates += 1
            row.problems.append(
                {"field": "", "text": "такая операция уже есть — повторная загрузка того же файла"}
            )
        session.add(
            ImportRow(
                batch_id=batch.id,
                line=row.line,
                state=state,
                raw=row.raw,
                parsed=row.values,
                problems=row.problems,
            )
        )
    batch.rows_duplicate = duplicates
    batch.rows_failed = counts["failed"]
    batch.rows_skipped = counts["skipped"]
    session.flush()
    return batch


def _known_keys(session: Session, workspace_id: uuid.UUID) -> set[str]:
    rows = session.scalars(
        sa.select(Operation.external_key).where(
            Operation.workspace_id == workspace_id,
            Operation.external_key.is_not(None),
            Operation.deleted_at.is_(None),
        )
    )
    return {value for value in rows if value}


def apply_batch(
    session: Session,
    workspace: Workspace,
    batch_id: uuid.UUID,
    *,
    actor: str = "",
    only_lines: Sequence[int] | None = None,
    create_dictionaries: bool = True,
) -> dict[str, Any]:
    """Завести операции по готовым строкам партии.

    Главное отличие от импортёров, которые мы разбирали: **строки с
    замечаниями не мешают остальным.** Плохая строка остаётся в партии со своим
    объяснением, хорошие становятся операциями. Файл из двухсот строк с одной
    испорченной заводит сто девяносто девять, а не ноль.
    """
    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.workspace_id != workspace.id:
        raise FinanceError("Партия импорта не найдена")
    # Повторный вызов — законная работа, а не ошибка: человек поправил
    # отложенную строку и заводит только её. Уже заведённые строки пропускаются
    # по `operation_id`, поэтому дублей это не создаёт.

    accounts = {account.normalized_name: account for account in list_accounts(session, workspace.id)}
    rows = list(
        session.scalars(
            sa.select(ImportRow).where(ImportRow.batch_id == batch.id).order_by(ImportRow.line)
        )
    )
    known = _known_keys(session, workspace.id)

    imported = 0
    for row in rows:
        if only_lines is not None and row.line not in set(only_lines):
            continue
        if row.state != "imported" or row.operation_id is not None:
            continue
        values = dict(row.parsed or {})
        key = values.get("external_key")
        if key and key in known:
            row.state = "duplicate"
            continue

        kind = values.get("kind")
        try:
            paid_at = date.fromisoformat(values["paid_at"])
            amount = Decimal(str(values["amount"]))
        except (KeyError, TypeError, ValueError):
            row.state = "failed"
            row.problems = list(row.problems or []) + [
                {"field": "", "text": "строка не разобрана до конца — нет даты или суммы"}
            ]
            continue

        account_from = accounts.get(norm(values.get("account_from") or "")) if values.get("account_from") else None
        account_to = accounts.get(norm(values.get("account_to") or "")) if values.get("account_to") else None

        side = "income" if kind == "income" else "expense"
        category = None
        if values.get("category") and kind != "transfer":
            category = ensure_category(
                session, workspace.id, side, values["category"], create=create_dictionaries
            )
        counterparty = None
        if values.get("counterparty"):
            role = "client" if kind == "income" else "supplier"
            counterparty = ensure_counterparty(
                session, workspace.id, values["counterparty"], role=role, create=create_dictionaries
            )
        projects: list[tuple[uuid.UUID, Decimal]] = []
        if values.get("project"):
            project = ensure_project(session, workspace.id, values["project"], create=create_dictionaries)
            if project is not None:
                projects.append((project.id, amount))
        tags: list[uuid.UUID] = []
        for tag_name in values.get("tags") or []:
            tag = ensure_tag(session, workspace.id, tag_name, create=create_dictionaries)
            if tag is not None:
                tags.append(tag.id)

        accrued = values.get("accrued_at")
        data = OperationInput(
            kind=kind,
            status="fact",
            paid_at=paid_at,
            accrued_at=date.fromisoformat(accrued) if accrued else None,
            period_start=date.fromisoformat(values["period_start"]) if values.get("period_start") else None,
            period_end=date.fromisoformat(values["period_end"]) if values.get("period_end") else None,
            amount=amount,
            currency=values.get("currency") or workspace.base_currency,
            account_from_id=account_from.id if account_from else None,
            account_to_id=account_to.id if account_to else None,
            category_id=category.id if category else None,
            counterparty_id=counterparty.id if counterparty else None,
            comment=values.get("comment") or "",
            projects=projects,
            tags=tags,
            source="import",
            external_key=key,
            raw=row.raw,
            import_batch_id=batch.id,
        )
        try:
            operation = create_operation(session, workspace, data, actor=actor)
        except FinanceError as exc:
            row.state = "failed"
            row.problems = list(row.problems or []) + [{"field": "", "text": str(exc)}]
            continue
        row.operation_id = operation.id
        imported += 1
        if key:
            known.add(key)

    batch.rows_imported = int(
        session.scalar(
            sa.select(sa.func.count(ImportRow.id)).where(
                ImportRow.batch_id == batch.id, ImportRow.operation_id.is_not(None)
            )
        )
        or 0
    )
    batch.rows_failed = int(
        session.scalar(
            sa.select(sa.func.count(ImportRow.id)).where(
                ImportRow.batch_id == batch.id, ImportRow.state == "failed"
            )
        )
        or 0
    )
    batch.status = "applied"
    batch.applied_at = datetime.now(timezone.utc)
    session.flush()
    return {
        "imported": imported,
        "failed": batch.rows_failed,
        "skipped": batch.rows_skipped,
        "duplicate": batch.rows_duplicate,
        "total": batch.rows_total,
    }


def fix_import_row(
    session: Session,
    workspace: Workspace,
    batch_id: uuid.UUID,
    line: int,
    patch: dict[str, Any],
) -> ImportRow:
    """Поправить отложенную строку партии, не перезагружая файл.

    Разобранные значения строки правятся точечно; после правки строка
    пересчитывается на «чего не хватает» и, если хватает всего, помечается
    готовой к заводке. Это и есть ответ на «файл отвергнут целиком»: работа
    идёт со строкой, а не с файлом.
    """
    row = session.scalar(
        sa.select(ImportRow).where(ImportRow.batch_id == batch_id, ImportRow.line == line)
    )
    if row is None:
        raise FinanceError("Строка не найдена")
    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.workspace_id != workspace.id:
        raise FinanceError("Партия импорта не найдена")

    values = dict(row.parsed or {})
    values.update({key: value for key, value in patch.items() if key in _FIXABLE})
    problems: list[dict[str, str]] = []

    if not values.get("paid_at"):
        problems.append({"field": "paid_at", "text": "нет даты платежа"})
    if not values.get("amount"):
        problems.append({"field": "amount", "text": "нет суммы"})
    kind = values.get("kind")
    if kind not in ("income", "expense", "transfer"):
        problems.append({"field": "kind", "text": "не указан вид операции"})
    accounts = {account.normalized_name for account in list_accounts(session, workspace.id)}
    for key, needed_for in (("account_from", ("expense", "transfer")), ("account_to", ("income", "transfer"))):
        name = values.get(key)
        if name and norm(name) not in accounts:
            problems.append({"field": key, "text": f"счёт «{name}» не найден"})
        elif not name and kind in needed_for:
            problems.append({"field": key, "text": "счёт не указан"})

    row.parsed = values
    row.problems = problems
    row.state = "failed" if problems else "imported"
    session.flush()
    return row


#: Поля строки импорта, которые можно поправить руками. Список закрытый: через
#: правку строки нельзя подменить отпечаток и завести дубль в обход проверки.
_FIXABLE = frozenset(
    {
        "paid_at", "accrued_at", "period_start", "period_end", "amount", "kind", "currency",
        "account_from", "account_to", "category", "subcategory", "counterparty", "project",
        "subproject", "comment", "tags",
    }
)


__all__ = [
    "DEFAULT_SLUG",
    "create_workspace",
    "get_workspace",
    "rename_workspace",
    "FinanceError",
    "OperationFilter",
    "OperationInput",
    "SEED_ACCOUNTS",
    "SEED_CATEGORIES",
    "VersionConflict",
    "apply_batch",
    "archive",
    "create_account",
    "create_operation",
    "delete_operation",
    "ensure_category",
    "ensure_counterparty",
    "ensure_project",
    "ensure_tag",
    "ensure_workspace",
    "fix_import_row",
    "list_accounts",
    "list_categories",
    "list_counterparties",
    "list_operations",
    "list_projects",
    "list_tags",
    "operation_projects",
    "operation_tags",
    "save_preview",
    "update_operation",
]
