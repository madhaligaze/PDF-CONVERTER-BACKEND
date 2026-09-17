"""Модель управленческого учёта раздела «Финансы».

Что здесь правда, а что производное
───────────────────────────────────
**Правда** — `workspaces`, `accounts`, `categories`, `counterparties`,
`projects`, `tags`, `operations`, `operation_projects`, `operation_tags`,
`plans`, `import_batches`, `import_rows`. Потеряли — потеряли навсегда.

**Производное** — отчёты (движение денег, прибыль, дебиторка, календарь). Они
не хранятся вообще: считаются из операций по запросу. Это сознательный выбор
против кэша с пересчётом: предрассчитанная таблица отчёта переживает правку
операции и показывает старые деньги, а заметить это невозможно — цифра
выглядит как цифра. Пока операций десятки тысяч, счёт на запрос дешевле
доверия к кэшу.

Три решения, которые стоит объяснить
────────────────────────────────────
**1. Сумма всегда положительная, направление задаёт `kind`.** Хранить знак и
вид операции одновременно значит завести два источника правды об одном: строка
с `kind="income"` и `amount=-5000` не имеет смысла, но запишется. Знак живёт
только на входе — импорт и формы приводят его к паре (вид, модуль суммы).

**2. Две даты, а не одна.** `paid_at` — когда двинулись деньги, `accrued_at` —
когда возникло обязательство. Отчёт о движении денег считается по первой,
прибыль — по второй. Одна дата сделала бы невозможным главный вопрос
управленческого учёта: «прибыль есть, а денег нет — почему?». Если `accrued_at`
не заполнена, она равна `paid_at`, и в этом случае оба отчёта совпадают.

**3. План — это `status`, а не будущая дата.** В Finmap плановость выводится из
календаря: платёж с датой в будущем считается запланированным. Из этого следует
неприятное: наступило первое число — и неоплаченный план молча стал фактом,
которого не было. Здесь `status` объявлен явно, поэтому существует и нужное
состояние «план, срок которого прошёл», то есть просроченное ожидание. Его
видно, и по нему можно работать.

Почему разнесение по проектам — отдельная таблица
─────────────────────────────────────────────────
Один платёж часто делится между проектами (аренда пополам). Колонка
`project_id` на операции заставила бы делить платёж на два — и в журнале
появились бы две записи там, где в банке одна. Поэтому разнесение живёт
строками `operation_projects`, а сумма разнесения сверяется с суммой операции.
Расхождение не запрещается (человеку надо дать сохранить незаконченную
работу), но помечается на самой операции: см. `Operation.split_state`.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.finance.db import FinanceBase

#: jsonb на Postgres, обычный JSON на SQLite.
JSONB = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

#: Деньги. `numeric`, никогда не `float`: сумма тысячи строк по копейкам на
#: двоичной плавающей точке не сходится, и расхождение всплывает не там, где
#: возникло, а в контрольной сумме на экране начальника.
MONEY = sa.Numeric(18, 2)
#: Курс валюты. Знаков больше, чем у денег: у пары USD/KZT значимы четвёртый и
#: пятый, и округление курса до копеек даёт расхождение на крупной сумме.
RATE = sa.Numeric(18, 6)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


# ── Словари допустимых значений ──────────────────────────────────────────────
#
# Текст с CHECK, а не ENUM Postgres: перечисление больно менять — добавление
# значения требует ALTER TYPE и не откатывается внутри транзакции.

#: Вид операции. Ровно три, как в любой кассовой книге.
OPERATION_KINDS = ("income", "expense", "transfer")
#: Факт — деньги двинулись. План — ожидание, подтверждённое документом или
#: договорённостью. Третьего состояния нет намеренно: «черновик» превращает
#: журнал в свалку недописанного.
OPERATION_STATUSES = ("fact", "plan")
#: Откуда взялась операция. Нужно не для истории, а для ответа на вопрос «кто
#: это записал»: строка из импорта и строка, набранная руками, чинятся
#: по-разному.
OPERATION_SOURCES = ("app", "grid", "import", "api")
ACCOUNT_KINDS = ("bank", "cash", "card", "safe", "crypto", "other")
CATEGORY_SIDES = ("income", "expense")
#: Роль контрагента. Список повторяет тот, что сложился в отрасли: он определяет,
#: в какую часть баланса попадёт долг — в дебиторку или в кредиторку.
COUNTERPARTY_ROLES = ("client", "supplier", "staff", "owner", "creditor", "borrower", "tax")
#: Состояние разнесения по проектам. `exact` — сошлось, `partial` — разнесена
#: часть, `mismatch` — сумма разнесения больше операции. Хранится на операции, а
#: не считается при каждом чтении: отчёт по проектам обязан уметь сказать
#: «здесь цифры неполные», не пересчитывая разнесение всех строк.
SPLIT_STATES = ("none", "exact", "partial", "mismatch")
IMPORT_STATUSES = ("preview", "applied", "cancelled")
#: Что стало со строкой файла. `skipped` — строка не операция (шапка, «Итого»,
#: пустая), `failed` — операция, но данных не хватило.
IMPORT_ROW_STATES = ("imported", "skipped", "failed", "duplicate")
PLAN_METHODS = ("cash", "accrual")


def _in(column: str, values: tuple[str, ...]) -> str:
    """Условие CHECK по списку допустимых значений.

    Не `str(кортеж)`: у кортежа из одного элемента репрезентация — `('x',)`, и
    висячая запятая делает SQL невалидным. Вылезло бы не сейчас, а в день,
    когда список сократят до одного значения.
    """
    listed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({listed})"


#: Шаг позиции в справочниках. Гнёзда между значениями нужны, чтобы вставка в
#: середину не перенумеровывала весь список.
POSITION_STEP = 1024


class Workspace(FinanceBase):
    """Рабочее пространство — одна компания.

    Заведено сразу, хотя пространство пока одно: дописать многоарендность
    потом — это переписать каждый запрос и каждую миграцию, а колонка сейчас
    стоит ничего.
    """

    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(sa.Text, unique=True)
    title: Mapped[str] = mapped_column(sa.Text)
    #: Валюта, в которой сходятся отчёты. Меняется только пересчётом всех
    #: операций, поэтому пишется один раз при создании пространства.
    base_currency: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'KZT'"))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class Account(FinanceBase):
    """Счёт — место, где лежат деньги. Касса, банк, сейф, карта.

    `starting_balance` — остаток на момент начала учёта в системе. Без него
    первый же отчёт покажет остаток, собранный только из введённых операций, и
    он не совпадёт с выпиской. Это самая частая причина недоверия к
    управленческому учёту в первую неделю.
    """

    __tablename__ = "accounts"
    __table_args__ = (
        sa.CheckConstraint(_in("kind", ACCOUNT_KINDS), name="account_kind"),
        sa.UniqueConstraint("workspace_id", "normalized_name", name="uq_accounts_workspace_name"),
        sa.Index("ix_accounts_workspace_id_position", "workspace_id", "position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(sa.Text)
    #: Имя без оформления и регистра. По нему импорт узнаёт счёт, когда в файле
    #: он написан иначе: «касса », «Касса», «КАССА» — один и тот же счёт.
    normalized_name: Mapped[str] = mapped_column(sa.Text)
    kind: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'bank'"))
    currency: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'KZT'"))
    starting_balance: Mapped[Decimal] = mapped_column(MONEY, server_default=sa.text("0"))
    #: Исключён из отчётов. Не удалён: удалить счёт с операциями нельзя, а
    #: «личная карта директора» в корпоративном cash flow не нужна.
    excluded_from_reports: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))
    position: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class Category(FinanceBase):
    """Статья доходов или расходов. Подкатегория — та же таблица через `parent_id`.

    Сторона (`side`) обязательна и неизменяема после первой операции: категория
    «Аренда» не может однажды стать доходной, иначе прошлые отчёты изменятся
    задним числом, а причину этого потом никто не найдёт.
    """

    __tablename__ = "categories"
    __table_args__ = (
        sa.CheckConstraint(_in("side", CATEGORY_SIDES), name="category_side"),
        sa.UniqueConstraint(
            "workspace_id", "side", "parent_id", "normalized_name", name="uq_categories_name"
        ),
        sa.Index("ix_categories_workspace_id_side", "workspace_id", "side"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    side: Mapped[str] = mapped_column(sa.Text)
    name: Mapped[str] = mapped_column(sa.Text)
    normalized_name: Mapped[str] = mapped_column(sa.Text)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("categories.id", ondelete="CASCADE")
    )
    #: Системный ключ для категорий, у которых особый смысл в отчётах: заём,
    #: погашение кредита, налоги, дивиденды. Пусто у обычных статей.
    system_key: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    position: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Counterparty(FinanceBase):
    """Тот, с кем расчёты: клиент, поставщик, сотрудник, кредитор, налоговая."""

    __tablename__ = "counterparties"
    __table_args__ = (
        sa.CheckConstraint(_in("role", COUNTERPARTY_ROLES), name="counterparty_role"),
        sa.UniqueConstraint("workspace_id", "role", "normalized_name", name="uq_counterparties_name"),
        sa.Index("ix_counterparties_workspace_id_role", "workspace_id", "role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'client'"))
    name: Mapped[str] = mapped_column(sa.Text)
    normalized_name: Mapped[str] = mapped_column(sa.Text)
    #: Реквизиты и заметки — свободная форма. Это не CRM: поля, которые
    #: понадобятся одной компании, не должны становиться колонками у всех.
    details: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    position: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Project(FinanceBase):
    """Проект или направление. Подпроект — через `parent_id`."""

    __tablename__ = "projects"
    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "parent_id", "normalized_name", name="uq_projects_name"),
        sa.Index("ix_projects_workspace_id_position", "workspace_id", "position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(sa.Text)
    normalized_name: Mapped[str] = mapped_column(sa.Text)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("projects.id", ondelete="CASCADE")
    )
    closed: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.text("false"))
    position: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Tag(FinanceBase):
    """Метка. Отличается от категории тем, что их у операции может быть много."""

    __tablename__ = "tags"
    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "normalized_name", name="uq_tags_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(sa.Text)
    normalized_name: Mapped[str] = mapped_column(sa.Text)
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Operation(FinanceBase):
    """Операция: поступление, списание или перевод между своими счетами.

    Про `amount_base`
    ─────────────────
    Сумма в валюте компании считается один раз — при записи — и хранится. Не
    считается на чтении по текущему курсу, и это важно: отчёт за июнь, открытый
    в сентябре, обязан показывать те же цифры, что показывал в июне. Отчёт,
    который меняется сам от того, что изменился курс, невозможно сверить ни с
    чем.

    Про `external_key`
    ──────────────────
    Отпечаток строки источника: файл плюс содержимое строки. По нему повторный
    импорт того же файла не заводит вторые копии. Отпечаток считается по
    данным, а не по номеру строки: в выписку дописывают операции сверху, и
    номер строки у той же операции меняется.
    """

    __tablename__ = "operations"
    __table_args__ = (
        sa.CheckConstraint(_in("kind", OPERATION_KINDS), name="operation_kind"),
        sa.CheckConstraint(_in("status", OPERATION_STATUSES), name="operation_status"),
        sa.CheckConstraint(_in("source", OPERATION_SOURCES), name="operation_source"),
        sa.CheckConstraint(_in("split_state", SPLIT_STATES), name="operation_split_state"),
        sa.CheckConstraint("amount >= 0", name="operation_amount_sign"),
        # Отпечаток уникален в пространстве, но только когда он есть: у
        # операций, набранных руками, его нет, и NULL здесь не конфликтуют.
        sa.UniqueConstraint("workspace_id", "external_key", name="uq_operations_external_key"),
        sa.Index("ix_operations_workspace_id_paid_at", "workspace_id", "paid_at"),
        sa.Index("ix_operations_workspace_id_accrued_at", "workspace_id", "accrued_at"),
        sa.Index("ix_operations_workspace_id_status", "workspace_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(sa.Text)
    status: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'fact'"))

    #: Дата движения денег. По ней считается всё, что про деньги.
    paid_at: Mapped[date] = mapped_column(sa.Date)
    #: Дата сделки. Пусто — значит совпадает с `paid_at`.
    accrued_at: Mapped[date | None] = mapped_column(sa.Date)
    #: Период начисления: аренда за квартал, оплаченная одним платежом.
    period_start: Mapped[date | None] = mapped_column(sa.Date)
    period_end: Mapped[date | None] = mapped_column(sa.Date)

    amount: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'KZT'"))
    amount_base: Mapped[Decimal] = mapped_column(MONEY)
    rate: Mapped[Decimal] = mapped_column(RATE, server_default=sa.text("1"))

    #: Откуда ушли деньги (расход, перевод) и куда пришли (доход, перевод).
    account_from_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("accounts.id", ondelete="RESTRICT")
    )
    account_to_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("accounts.id", ondelete="RESTRICT")
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("categories.id", ondelete="SET NULL")
    )
    counterparty_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("counterparties.id", ondelete="SET NULL")
    )

    comment: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    split_state: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'none'"))

    source: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'app'"))
    external_key: Mapped[str | None] = mapped_column(sa.Text)
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("import_batches.id", ondelete="SET NULL")
    )
    #: Данные строки источника как есть. Нужны, когда в отчёте увидели
    #: странность и надо посмотреть, что именно было в выписке.
    raw: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))

    created_by: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    #: Номер правки. Растёт на каждое изменение; фронт присылает его обратно,
    #: и правка по устаревшей версии отклоняется, а не перетирает чужую.
    version: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("1"))


class OperationProject(FinanceBase):
    """Разнесение операции по проектам."""

    __tablename__ = "operation_projects"
    __table_args__ = (
        sa.UniqueConstraint("operation_id", "project_id", name="uq_operation_projects"),
        sa.CheckConstraint("amount >= 0", name="operation_project_amount_sign"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("operations.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(MONEY)


class OperationTag(FinanceBase):
    """Метки операции."""

    __tablename__ = "operation_tags"
    __table_args__ = (sa.UniqueConstraint("operation_id", "tag_id", name="uq_operation_tags"),)

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("operations.id", ondelete="CASCADE"), index=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("tags.id", ondelete="CASCADE"), index=True
    )


class Plan(FinanceBase):
    """План на месяц по статье или проекту — для отчёта «План/Факт».

    `method` различает два разных плана на одну статью: план по деньгам
    (когда заплатим) и план по начислению (когда возникнет обязательство). Это
    не дубль: бюджет платежей и бюджет расходов — разные документы, и путать их
    значит спорить о цифрах на планёрке.
    """

    __tablename__ = "plans"
    __table_args__ = (
        sa.CheckConstraint(_in("side", CATEGORY_SIDES), name="plan_side"),
        sa.CheckConstraint(_in("method", PLAN_METHODS), name="plan_method"),
        sa.UniqueConstraint(
            "workspace_id", "month", "side", "method", "category_id", "project_id",
            name="uq_plans_slot",
        ),
        sa.Index("ix_plans_workspace_id_month", "workspace_id", "month"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    #: Первое число месяца плана. Дата, а не строка «2026-09»: по дате
    #: сравнивают, сортируют и считают интервалы, по строке — нет.
    month: Mapped[date] = mapped_column(sa.Date)
    side: Mapped[str] = mapped_column(sa.Text)
    method: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'cash'"))
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("categories.id", ondelete="CASCADE")
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("projects.id", ondelete="CASCADE")
    )
    amount: Mapped[Decimal] = mapped_column(MONEY)
    comment: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    created_by: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
    )


class ImportBatch(FinanceBase):
    """Один импорт файла: что за файл, как разобрали, чем закончилось.

    Партия живёт и после применения, и это главное отличие нашего импорта от
    соседей по рынку: по ней видно, какие строки завелись, какие отложены и
    почему. Отложенную строку можно поправить и завести, не перезагружая файл.
    """

    __tablename__ = "import_batches"
    __table_args__ = (
        sa.CheckConstraint(_in("status", IMPORT_STATUSES), name="import_batch_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    file_name: Mapped[str] = mapped_column(sa.Text)
    status: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("'preview'"))
    #: Как колонки файла легли на поля учёта, вместе с тем, каким способом
    #: опознались. Это ответ на вопрос «почему сумма попала в комментарий».
    mapping: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    #: Решения, принятые за весь файл: порядок частей даты, знак суммы, строка
    #: шапки. Они общие для файла, а не для строки, — и хранятся тут.
    decisions: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    rows_total: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    rows_imported: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    rows_failed: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    rows_skipped: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    rows_duplicate: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    created_by: Mapped[str] = mapped_column(sa.Text, server_default=sa.text("''"))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    applied_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class ImportRow(FinanceBase):
    """Строка файла и её судьба.

    `problems` — список замечаний по строке: чего не хватило, что не разобрали.
    Список, а не одна строка текста: у одной записи может не быть и даты, и
    суммы, и показать надо оба замечания сразу, а не первое из них.
    """

    __tablename__ = "import_rows"
    __table_args__ = (
        sa.CheckConstraint(_in("state", IMPORT_ROW_STATES), name="import_row_state"),
        sa.Index("ix_import_rows_batch_id_line", "batch_id", "line"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("import_batches.id", ondelete="CASCADE"), index=True
    )
    #: Номер строки в файле, как его видит человек в Excel: шапка — 1.
    line: Mapped[int] = mapped_column(sa.Integer)
    state: Mapped[str] = mapped_column(sa.Text)
    raw: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    #: Разобранные значения — то, что станет операцией.
    parsed: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    problems: Mapped[list] = mapped_column(JSONB, server_default=sa.text("'[]'"))
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("operations.id", ondelete="SET NULL")
    )


__all__ = [
    "ACCOUNT_KINDS",
    "Account",
    "CATEGORY_SIDES",
    "COUNTERPARTY_ROLES",
    "Category",
    "Counterparty",
    "IMPORT_ROW_STATES",
    "IMPORT_STATUSES",
    "ImportBatch",
    "ImportRow",
    "MONEY",
    "OPERATION_KINDS",
    "OPERATION_SOURCES",
    "OPERATION_STATUSES",
    "Operation",
    "OperationProject",
    "OperationTag",
    "PLAN_METHODS",
    "POSITION_STEP",
    "Plan",
    "Project",
    "SPLIT_STATES",
    "Tag",
    "Workspace",
]
