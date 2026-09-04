"""Модель данных внутренних книг.

Главное разделение: правда и производное
────────────────────────────────────────
Единственное, что реально держит базу в порядке годами, — чёткое знание, что
можно пересчитать, а что нельзя.

**Правда** — `workspaces`, `books`, `tables`, `fields`, `rows`, `bindings`,
`role_catalog`, `role_synonyms`, `import_runs`, `import_issues`, `row_edits`.
Потеряли — потеряли навсегда.

**Производное** — `row_facts`. Собирается из `rows` × `bindings` одной
командой. Уронить и пересобрать — законная операция.

Правило: если производное нельзя пересобрать, значит это не производное, и
называть его так нельзя.

Почему значения строки — jsonb, а расчёт — по типизированной проекции
─────────────────────────────────────────────────────────────────────
Таблица приходит от чужой компании и имеет произвольную форму. Три способа это
хранить, и каждый плох по-своему: колонка на поле требует DDL в рантайме и
тысяч таблиц; EAV превращает любой отчёт в десяток самосоединений; jsonb гибок,
но сам по себе не типизирован и плохо агрегируется.

Взят jsonb — и его слабое место вылечено точечно. `rows.values` терпит что
угодно и является правдой. А дашборд считает не по нему, а по `row_facts`:
узкой типизированной проекции только тех полей, у которых есть привязка к роли.
Непривязанные поля живут в jsonb, видны в гриде и в формах, но в расчёты не
попадают — и это честно показано на табло привязок, а не замолчано.

Почему составные внешние ключи
──────────────────────────────
`workspace_id` стоит в каждой таблице правды. Само по себе это ничего не
гарантирует: ничто не мешает строке одного пространства сослаться на таблицу
другого — и утечка между арендаторами будет выглядеть как обычная строка.
Поэтому ссылки составные: `(table_id, workspace_id) → tables(id, workspace_id)`.
Тогда несоответствие пространства становится невозможным на уровне схемы, а не
на уровне внимательности того, кто пишет запрос.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.books.db import BooksBase

#: jsonb на Postgres, обычный JSON на SQLite. jsonb — не украшение: по нему
#: работают индексы и операторы поиска, а обычный json в Postgres хранится
#: текстом и каждый раз разбирается заново.
JSONB = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

#: Деньги. `numeric`, никогда не `float`: сумма тысячи строк по копейкам на
#: двоичной плавающей точке не сходится, и расхождение всплывает не там, где
#: возникло, а в контрольной сумме на экране начальника.
MONEY = sa.Numeric(18, 2)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


# ── Словари допустимых значений ──────────────────────────────────────────────
#
# Текст с CHECK, а не ENUM Postgres: тип-перечисление больно менять — добавление
# значения требует ALTER TYPE и не откатывается внутри транзакции.

SOURCE_KINDS = ("google_sheets", "app")
FIELD_TYPES = ("text", "number", "money", "date", "bool", "enum", "formula", "unknown")
ROW_ORIGINS = ("source", "app")
ROW_STATES = ("live", "missing_in_source")
BINDING_CONFIDENCE = ("exact", "squashed", "loose", "data", "manual")
SYNONYM_SOURCES = ("seed", "learned")
IMPORT_STATUSES = ("preview", "applied", "blocked", "failed")
EDIT_SOURCES = ("app", "grid", "import")


def _in(column: str, values: tuple[str, ...]) -> str:
    """Условие CHECK по списку допустимых значений.

    Не `str(кортеж)`: у кортежа из одного элемента репрезентация — `('x',)`,
    и висячая запятая делает SQL невалидным. Ошибка вылезла бы не сейчас, а
    в тот день, когда список значений сократят до одного.
    """
    listed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({listed})"


#: Шаг между соседними строками при вставке. Гнёзда между позициями нужны,
#: чтобы вставка строки в середину не перенумеровывала весь лист: новой строке
#: достаётся середина промежутка. Когда промежутки кончаются, лист
#: перенумеровывается целиком — редкая операция обслуживания.
POSITION_STEP = 1024


class Workspace(BooksBase):
    """Рабочее пространство — одна компания.

    Заведено сразу, хотя пространство пока одно. Дописать многоарендность
    потом — это переписать каждый запрос и каждую миграцию; колонка сейчас
    стоит ничего.
    """

    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(sa.Text, unique=True)
    title: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class Book(BooksBase):
    """Книга: импортированная из Google или заведённая в приложении."""

    __tablename__ = "books"
    __table_args__ = (
        sa.ForeignKeyConstraint(["workspace_id"], ["books.workspaces.id"], ondelete="RESTRICT"),
        # Мишень для составных ссылок из дочерних таблиц.
        sa.UniqueConstraint("id", "workspace_id", name="uq_books_id_workspace"),
        sa.CheckConstraint(_in("source_kind", SOURCE_KINDS), name="source_kind"),
        sa.Index("ix_books_workspace_id", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    title: Mapped[str] = mapped_column(sa.Text)
    source_kind: Mapped[str] = mapped_column(sa.Text, default="google_sheets")
    #: id книги в Google. Пусто для книг, заведённых в приложении.
    source_ref: Mapped[str] = mapped_column(sa.Text, default="")
    #: Когда последний раз успешно импортировали. NULL — ни разу.
    imported_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class BookTable(BooksBase):
    """Вкладка книги. В коде `BookTable`, чтобы не путаться с `sa.Table`."""

    __tablename__ = "tables"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["book_id", "workspace_id"],
            ["books.books.id", "books.books.workspace_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("id", "workspace_id", name="uq_tables_id_workspace"),
        sa.UniqueConstraint("book_id", "name", name="uq_tables_book_name"),
        sa.Index("ix_tables_workspace_id", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    book_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    name: Mapped[str] = mapped_column(sa.Text)
    #: gid вкладки в Google — чтобы узнать её после переименования.
    source_ref: Mapped[str] = mapped_column(sa.Text, default="")
    position: Mapped[int] = mapped_column(sa.Integer, default=0)
    #: Номер строки с заголовками, считая с 1. В чужих книгах шапка не всегда
    #: первая: сверху бывает заголовок отчёта или строка с периодом.
    header_row: Mapped[int] = mapped_column(sa.Integer, default=1)
    #: Поля, образующие ключ строки при повторном импорте. Список ключей полей.
    #: Пусто — книга ещё не размечена, повторный импорт невозможен.
    identity_fields: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class BookField(BooksBase):
    """Колонка вкладки — то, что нашлось при импорте.

    Описательная запись: заголовок, выведенный тип, статистика. Про смысл для
    приложения она ничего не знает — смысл появляется только в `bindings`.
    """

    __tablename__ = "fields"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["table_id", "workspace_id"],
            ["books.tables.id", "books.tables.workspace_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("id", "workspace_id", name="uq_fields_id_workspace"),
        sa.UniqueConstraint("table_id", "key", name="uq_fields_table_key"),
        sa.CheckConstraint(_in("type", FIELD_TYPES), name="type"),
        sa.Index("ix_fields_workspace_id", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    table_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    #: Стабильный ключ поля — им адресуются значения в `rows.values`.
    key: Mapped[str] = mapped_column(sa.Text)
    #: Заголовок, как он написан в книге.
    title: Mapped[str] = mapped_column(sa.Text)
    type: Mapped[str] = mapped_column(sa.Text, default="unknown")
    position: Mapped[int] = mapped_column(sa.Integer, default=0)
    #: Все написания заголовка, которые считаются этим полем. Книгу
    #: переименовывают, и повторный импорт обязан узнать колонку по любому из
    #: прежних имён.
    names: Mapped[list] = mapped_column(JSONB, default=list)
    required: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    editable: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    #: Заполненность, число различных значений, примеры, доля разбираемых
    #: значений. То, из чего складывается предложение привязки по данным.
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    #: Оформление задаётся по полю, а не по ячейке: формат числа, ширина,
    #: выравнивание. Ячейка со своим цветом — это то, что делает привязку
    #: хрупкой, и здесь её нет намеренно.
    display: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class BookRow(BooksBase):
    """Строка вкладки — правда о данных."""

    __tablename__ = "rows"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["table_id", "workspace_id"],
            ["books.tables.id", "books.tables.workspace_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("id", "workspace_id", name="uq_rows_id_workspace"),
        sa.CheckConstraint(_in("origin", ROW_ORIGINS), name="origin"),
        sa.CheckConstraint(_in("state", ROW_STATES), name="state"),
        sa.Index("ix_rows_table_position", "table_id", "position"),
        # Ключ строки в источнике: по нему повторный импорт узнаёт, какую
        # строку он видит. Не уникален намеренно — двойники возможны, и это
        # случай для разбора, а не для отказа записать.
        sa.Index("ix_rows_table_source_key", "table_id", "source_key"),
        sa.Index("ix_rows_workspace_id", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    table_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    #: Позиция с гнёздами (см. POSITION_STEP).
    position: Mapped[int] = mapped_column(sa.BigInteger, default=0)
    #: Значения по ключу поля. Правда.
    values: Mapped[dict] = mapped_column(JSONB, default=dict)
    #: Что мы последний раз видели в источнике. Общий предок для слияния при
    #: повторном импорте: он и отличает «человек поправил у нас» от «мы этого
    #: ещё не видели».
    base: Mapped[dict] = mapped_column(JSONB, default=dict)
    source_key: Mapped[str] = mapped_column(sa.Text, default="")
    origin: Mapped[str] = mapped_column(sa.Text, default="app")
    state: Mapped[str] = mapped_column(sa.Text, default="live")
    #: Растёт на каждую правку. Оптимистичная блокировка для формы и грида:
    #: сохранение с устаревшей версией отклоняется, а не затирает чужое.
    version: Mapped[int] = mapped_column(sa.Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_by: Mapped[str] = mapped_column(sa.Text, default="")
    deleted_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class RoleCatalog(BooksBase):
    """Роль — гнездо, которое разделу нужно заполнить, чтобы считать.

    Общая на все компании: роли объявляет продукт, а не арендатор. Специфична
    для компании только привязка поля к роли.

    Раздела здесь нет намеренно. Роль «заказчик» нужна сразу дебиторке,
    журналу касаний и календарю — колонка `section` заставила бы либо
    заводить три почти одинаковые роли, либо приписывать общую роль одному
    разделу и врать. Что каждому разделу требуется, объявляет код продукта
    (`books/roles.py`): разделы — это функции приложения, а не настройка
    арендатора, и в базе им делать нечего.
    """

    __tablename__ = "role_catalog"
    __table_args__ = (
        sa.CheckConstraint(_in("value_type", FIELD_TYPES), name="value_type"),
    )

    key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    title: Mapped[str] = mapped_column(sa.Text)
    value_type: Mapped[str] = mapped_column(sa.Text)
    description: Mapped[str] = mapped_column(sa.Text, default="")
    position: Mapped[int] = mapped_column(sa.Integer, default=0)


class RoleSynonym(BooksBase):
    """Написание заголовка, по которому роль узнаётся автоматически.

    `workspace_id IS NULL` — синоним, поставляемый с продуктом. Заполненный —
    выученный из подтверждённой человеком привязки в этой компании. Именно из
    вторых каталог со временем умнеет: следующая книга, где колонку назвали так
    же, привяжется сама.
    """

    __tablename__ = "role_synonyms"
    __table_args__ = (
        sa.ForeignKeyConstraint(["role_key"], ["books.role_catalog.key"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["books.workspaces.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("role_key", "workspace_id", "normalized", name="uq_role_synonyms_key"),
        sa.CheckConstraint(_in("source", SYNONYM_SOURCES), name="source"),
        sa.Index("ix_role_synonyms_normalized", "normalized"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    role_key: Mapped[str] = mapped_column(sa.Text)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    #: Заголовок как написан — для показа человеку.
    text: Mapped[str] = mapped_column(sa.Text)
    #: Он же после нормализации — по нему идёт сравнение.
    normalized: Mapped[str] = mapped_column(sa.Text)
    source: Mapped[str] = mapped_column(sa.Text, default="seed")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class Binding(BooksBase):
    """Поле → роль. Единственное, что специфично для компании."""

    __tablename__ = "bindings"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["table_id", "workspace_id"],
            ["books.tables.id", "books.tables.workspace_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["field_id", "workspace_id"],
            ["books.fields.id", "books.fields.workspace_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["role_key"], ["books.role_catalog.key"], ondelete="CASCADE"),
        # Одна роль в таблице заполняется ровно одним полем, и одно поле играет
        # ровно одну роль. Иначе «сумма договора» могла бы приехать сразу из
        # двух колонок, и какая победит — зависело бы от порядка строк.
        sa.UniqueConstraint("table_id", "role_key", name="uq_bindings_table_role"),
        sa.UniqueConstraint("table_id", "field_id", name="uq_bindings_table_field"),
        sa.CheckConstraint(_in("confidence", BINDING_CONFIDENCE), name="confidence"),
        sa.Index("ix_bindings_workspace_id", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    table_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    field_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    role_key: Mapped[str] = mapped_column(sa.Text)
    #: Чем обосновано: точным заголовком, сжатым, похожим, данными или рукой.
    confidence: Mapped[str] = mapped_column(sa.Text, default="manual")
    #: Пока не подтверждена человеком, привязка считается предложением.
    confirmed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    confirmed_by: Mapped[str] = mapped_column(sa.Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class RowFact(BooksBase):
    """ПРОИЗВОДНОЕ. Типизированная проекция строки по ролям.

    Здесь и только здесь дашборд берёт цифры: `rows.values` терпит что угодно,
    а агрегировать надо по числам, датам и строкам с известными типами.

    Таблицу можно уронить целиком и собрать заново из `rows` × `bindings`.
    Ровно это и проверяет `test_books_rebuild_facts`: если пересборка даёт
    другой результат, значит здесь появилось что-то, чего в правде нет, — и
    это уже не производное.
    """

    __tablename__ = "row_facts"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["row_id", "workspace_id"],
            ["books.rows.id", "books.rows.workspace_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["role_key"], ["books.role_catalog.key"], ondelete="CASCADE"),
        sa.Index("ix_row_facts_table_role", "table_id", "role_key"),
        sa.Index("ix_row_facts_table_role_date", "table_id", "role_key", "date_value"),
        sa.Index("ix_row_facts_workspace_id", "workspace_id"),
    )

    row_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    role_key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    table_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    num_value: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    date_value: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    text_value: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    bool_value: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)


class ImportRun(BooksBase):
    """Один заход импорта. Всегда с предпросмотром — ничего не применяется молча."""

    __tablename__ = "import_runs"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["book_id", "workspace_id"],
            ["books.books.id", "books.books.workspace_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_in("status", IMPORT_STATUSES), name="status"),
        sa.Index("ix_import_runs_book_started", "book_id", "started_at"),
        sa.Index("ix_import_runs_workspace_id", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    book_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    status: Mapped[str] = mapped_column(sa.Text, default="preview")
    started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    started_by: Mapped[str] = mapped_column(sa.Text, default="")
    #: Сколько строк добавится, обновится, разойдётся, исчезнет. То, что
    #: человек видит до применения.
    summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    #: Заполнено, если импорт встал: превышен порог массового изменения,
    #: не сошлась раскладка, пропал ключ строки.
    blocked_reason: Mapped[str] = mapped_column(sa.Text, default="")
    error: Mapped[str] = mapped_column(sa.Text, default="")


class ImportIssue(BooksBase):
    """Что импорт не смог решить сам. Показывается человеку, не гадается."""

    __tablename__ = "import_issues"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["import_run_id"], ["books.import_runs.id"], ondelete="CASCADE"
        ),
        sa.Index("ix_import_issues_run", "import_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    import_run_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    #: conflict | duplicate_key | unparsed | missing_in_source | column_drift
    kind: Mapped[str] = mapped_column(sa.Text)
    row_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    field_key: Mapped[str] = mapped_column(sa.Text, default="")
    #: Обе версии значения при расхождении, номера строк при двойниках и т. п.
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    resolved_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    resolved_by: Mapped[str] = mapped_column(sa.Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class RowEdit(BooksBase):
    """Журнал правок: кто, когда, что было, что стало.

    Пишется на каждую правку из любой поверхности — формы, грида, импорта. Это
    и аудит, и то, из чего при необходимости восстанавливается состояние на
    любой момент.
    """

    __tablename__ = "row_edits"
    __table_args__ = (
        sa.CheckConstraint(_in("source", EDIT_SOURCES), name="source"),
        sa.Index("ix_row_edits_row_at", "row_id", "at"),
        sa.Index("ix_row_edits_table_at", "table_id", "at"),
        sa.Index("ix_row_edits_workspace_id", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    table_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    #: Без внешнего ключа намеренно: журнал переживает удаление строки. Запись
    #: о том, что строку правили, не должна исчезать вместе с ней.
    row_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    actor: Mapped[str] = mapped_column(sa.Text, default="")
    source: Mapped[str] = mapped_column(sa.Text, default="app")
    #: {ключ поля: [было, стало]}
    changes: Mapped[dict] = mapped_column(JSONB, default=dict)


__all__ = [
    "BINDING_CONFIDENCE",
    "EDIT_SOURCES",
    "FIELD_TYPES",
    "IMPORT_STATUSES",
    "JSONB",
    "MONEY",
    "POSITION_STEP",
    "ROW_ORIGINS",
    "ROW_STATES",
    "SOURCE_KINDS",
    "SYNONYM_SOURCES",
    "Binding",
    "Book",
    "BookField",
    "BookRow",
    "BookTable",
    "ImportIssue",
    "ImportRun",
    "RoleCatalog",
    "RoleSynonym",
    "RowEdit",
    "RowFact",
    "Workspace",
]
