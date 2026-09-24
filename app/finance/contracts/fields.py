"""Поля реестра, значения списков со смыслом и засев новой компании.

Засев общий, не под BBC
───────────────────────
Новая компания получает системные поля, статусы с фазами, виды договоров со
способом начисления и подписями сторон, хозяйственные смыслы и главный лист
«Все договоры». Всё, чем компания отличается (колонка «Ссылка на Битрикс 24»,
статус «нужно закрыть по бух», лист «Исполнитель ГК»), приходит из её
собственного Excel при загрузке или заводится в настройке. Под одну компанию
здесь не пишется ни строки.

Засев повторяемый: системное поле, добавленное в следующей версии, появится у
всех компаний при первом обращении к реестру, а не останется только у новых.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.books.layout import norm, squash
from app.finance.contracts.models import (
    ECONOMIC_ROLES,
    Counter,
    EntityField,
    EntityView,
    ListValue,
)
from app.finance.models import POSITION_STEP, Workspace

ENTITY = "contract"


@dataclass(frozen=True)
class FieldDef:
    """Системное поле: ключ, тип, подпись по умолчанию и написания шапки."""

    key: str
    type: str
    title: str
    names: tuple[str, ...] = ()
    #: Поле только для чтения везде: «как было в файле».
    readonly: bool = False
    #: Спрятано по умолчанию (есть, но в листе и карточке не показывается).
    hidden: bool = False


#: Системные поля в порядке «Сводной». Написания — это то, как колонку
#: подписывают в реестрах; разбор сравнивает по шапке блока, а не по номеру.
SYSTEM_FIELDS: tuple[FieldDef, ...] = (
    FieldDef("status", "list", "Текущее состояние",
             ("текущее состояние", "статус", "состояние", "статус договора")),
    FieldDef("folder_url", "url", "Папка договора",
             ("ссылка на битрикс 24", "ссылка на битрикс", "папка договора", "битрикс", "ссылка")),
    FieldDef("planned_end_at", "date", "Планируемый срок завершения",
             ("планируемый срок завершения", "план дата завершения", "срок завершения")),
    FieldDef("people", "person", "Ответственное лицо",
             ("ответственное лицо (ф.и.о)", "ответственное лицо", "ответственный",
              "наш сотрудник", "ответ лицо")),
    FieldDef("executor", "party", "Исполнитель",
             ("исполнитель bbc", "исполнитель", "продавец", "арендодатель bbc", "арендодатель",
              "арендадатель bbc", "арендадатель", "займодавец", "наша фирма")),
    FieldDef("customer", "party", "Заказчик",
             ("заказчик (клиент)", "заказчик", "покупатель", "арендатор (клиент)", "арендатор",
              "займополучатель", "заказчик (название фирмы)", "клиент")),
    FieldDef("number", "text", "№ Договора",
             ("№ договора", "номер договора", "договор №")),
    FieldDef("signed_at", "date", "Дата заключения Договора",
             ("дата заключения договора", "дата договора", "дата заключения")),
    FieldDef("department", "department", "Отдел", ("отдел",)),
    FieldDef("type", "list", "Вид услуги", ("вид услуги", "вид договора", "вид")),
    FieldDef("subject", "list", "Предмет Договора",
             ("предмет договора", "предмет", "предмет исполнения")),
    FieldDef("amount", "money", "Сумма Договора", ("сумма договора", "сумма")),
    FieldDef("paid_snapshot", "money", "Оплачено (в файле)",
             ("оплачено на текущую дату", "оплачено"), readonly=True),
    FieldDef("remaining_snapshot", "money", "Остаток (в файле)",
             ("остаток оплаты", "остаток", "сумма остаток"), readonly=True),
    FieldDef("amendments_text", "text", "№ Доп. соглашения / дата",
             ("№ дополнительное соглашение/дата", "№ дополнительное соглашение",
              "дополнительное соглашение", "доп соглашения", "доп. соглашения")),
    FieldDef("amendments_summary_text", "text", "Предмет доп. соглашения",
             ("предмет доп.соглашения", "предмет доп. соглашения", "предмет доп соглашения")),
    FieldDef("end_date", "date", "Дата расторжения / исполнения",
             ("дата расторжения договора/дата исполения разового договора",
              "дата расторжения договора/дата исполнения разового договора",
              "дата расторжения договора", "дата расторжения", "дата исполнения",
              "факт дата завершения")),
    FieldDef("note", "text", "Примечания", ("*примечания", "примечания", "примечание", "комментарий")),
    # Ниже — поля, которых в файле BBC нет колонкой: они выводятся или
    # задаются в карточке.
    FieldDef("billing", "choice", "Начисление"),
    FieldDef("economic_role", "list", "Хозяйственный смысл"),
    FieldDef("amount_terms", "text", "Условие суммы"),
    FieldDef("end_kind", "choice", "Смысл даты окончания"),
    FieldDef("currency", "text", "Валюта", hidden=True),
)
SYSTEM_KEYS = frozenset(item.key for item in SYSTEM_FIELDS)
FIELD_BY_KEY = {item.key: item for item in SYSTEM_FIELDS}
#: Поля-списки с собственными значениями в `list_values`.
SYSTEM_LISTS = ("status", "type", "subject", "economic_role")
#: Поля, правка которых у существующего договора спрашивает «опечатка или с
#: даты». Список отдаёт схема, чтобы клиент не держал свой.
MODE_FIELDS = ("executor", "customer", "amount")
#: Поля «как было в файле» — только чтение.
SNAPSHOT_FIELDS = ("paid_snapshot", "remaining_snapshot")
CHOICES = {
    "billing": (("month", "В месяц"), ("total", "Вся сумма"), ("terms", "Условие")),
    "end_kind": (("terminated", "Расторжение"), ("fulfilled", "Исполнение"), ("unknown", "Не ясен")),
}


# ── Засев ────────────────────────────────────────────────────────────────────

#: Статусы. Фаза — системный смысл; подпись компания меняет как хочет.
SEED_STATUSES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("Действующий", {"phase": "active"}),
    ("На исполнении", {"phase": "in_progress"}),
    ("Приостановлен", {"phase": "suspended"}),
    ("Исполнен", {"phase": "fulfilled"}),
    ("Недействующий", {"phase": "terminated"}),
    ("Не состоялся", {"phase": "failed"}),
)
#: Виды договоров: как начислять, какой смысл по умолчанию, как подписаны
#: стороны. «Иное» — вид без начисления, но со смыслом «вид известен».
SEED_TYPES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("Абонентское обслуживание", {"billing": "month", "economic_role": "revenue"}),
    ("Разовая услуга", {"billing": "total", "economic_role": "revenue"}),
    ("Аренда", {"billing": "month", "economic_role": "revenue",
                "roles": {"executor": "Арендодатель", "customer": "Арендатор"}}),
    ("Агентский", {"billing": "terms"}),
    ("Заём", {"billing": "total", "economic_role": "financing",
              "roles": {"executor": "Займодавец", "customer": "Займополучатель"}}),
    ("Купля-продажа", {"billing": "total",
                       "roles": {"executor": "Продавец", "customer": "Покупатель"}}),
    ("Иное", {"kind": "other"}),
)
SEED_ECONOMIC_ROLES: tuple[tuple[str, str], ...] = (
    ("Выручка", "revenue"),
    ("Расход", "expense"),
    ("Финансирование", "financing"),
    ("Внутри группы", "intra_group"),
)

#: Смысл предмета по словам в названии. Это не угадывание, а подстановка с
#: пометкой `{ по предмету }`: её видно в карточке, и человек её правит. Без
#: неё «Финансовая помощь» при виде «Иное» считалась бы выручкой и попала бы в
#: порог НДС.
SUBJECT_HINTS: tuple[tuple[re.Pattern[str], dict[str, Any]], ...] = (
    (re.compile(r"финанс\w*\s+помощ|за[её]м|займ", re.IGNORECASE),
     {"billing": "total", "economic_role": "financing",
      "roles": {"executor": "Займодавец", "customer": "Займополучатель"}}),
    (re.compile(r"агент", re.IGNORECASE), {"billing": "terms"}),
    (re.compile(r"аренд", re.IGNORECASE),
     {"billing": "month", "roles": {"executor": "Арендодатель", "customer": "Арендатор"}}),
    (re.compile(r"купл\w*[-\s]*продаж", re.IGNORECASE),
     {"billing": "total", "roles": {"executor": "Продавец", "customer": "Покупатель"}}),
)


def subject_meaning(value: str) -> dict[str, Any]:
    """Смысл нового предмета по словам в названии; пусто — смысла нет."""
    for pattern, meaning in SUBJECT_HINTS:
        if pattern.search(value or ""):
            return dict(meaning)
    return {}


# ── Нормализация ─────────────────────────────────────────────────────────────

_QUOTES = re.compile(r"[«»\"'“”„`]")
_ORG_FORMS = (
    (re.compile(r"товарищество\s+с\s+ограниченной\s+ответственностью", re.IGNORECASE), "тоо"),
    (re.compile(r"индивидуальный\s+предприниматель", re.IGNORECASE), "ип"),
    (re.compile(r"акционерное\s+общество", re.IGNORECASE), "ао"),
)


def party_key(name: Any) -> str:
    """Ключ стороны: без кавычек, регистра, пробелов и знаков.

    «ТОО «Атриум плюс»», 'ТОО "АТРИУМ ПЛЮС"' и «ТОО Атриум  плюс» — один
    ключ. Организационная форма остаётся: «ТОО Альфа» и «ИП Альфа» — разные
    лица, и склеивать их молча нельзя. Похожие, но не равные по ключу имена
    сводит только человек («Это ТОО «Атриум плюс»?»).
    """
    text = _QUOTES.sub(" ", str(name or ""))
    for pattern, short in _ORG_FORMS:
        text = pattern.sub(short, text)
    return squash(text)


_NUMBER_JUNK = re.compile(r"[\s№#]+")


def number_key(number: Any) -> str:
    """Номер договора без пробелов, «№» и регистра — для поиска повторов."""
    return _NUMBER_JUNK.sub("", str(number or "")).lower()


# ── Счётчики ─────────────────────────────────────────────────────────────────


def bump(session: Session, workspace_id: uuid.UUID, name: str) -> int:
    """Следующее значение счётчика компании.

    Блокировка строки держится до конца транзакции — номера видны строго по
    порядку (см. `Counter`). Первая запись счётчика заводится во вложенной
    транзакции: два первых запроса одновременно не должны уронить друг друга
    на уникальном ключе.
    """
    table = Counter.__table__
    statement = (
        sa.update(table)
        .where(table.c.workspace_id == workspace_id, table.c.name == name)
        .values(value=table.c.value + 1)
        .returning(table.c.value)
    )
    value = session.execute(statement).scalar()
    if value is not None:
        return int(value)
    try:
        with session.begin_nested():
            session.add(Counter(workspace_id=workspace_id, name=name, value=1))
        return 1
    except IntegrityError:
        value = session.execute(statement).scalar()
        return int(value or 1)


def current(session: Session, workspace_id: uuid.UUID, name: str) -> int:
    value = session.scalar(
        sa.select(Counter.value).where(Counter.workspace_id == workspace_id, Counter.name == name)
    )
    return int(value or 0)


# ── Засев реестра ────────────────────────────────────────────────────────────


def _field_rows(session: Session, workspace_id: uuid.UUID) -> dict[str, EntityField]:
    rows = session.scalars(
        sa.select(EntityField).where(
            EntityField.workspace_id == workspace_id, EntityField.entity == ENTITY
        )
    )
    return {row.key: row for row in rows}


def ensure_registry(session: Session, workspace: Workspace) -> None:
    """Досеять реестр компании. Повторный вызов ничего не меняет.

    Всё во вложенной транзакции: реестр открывается несколькими запросами
    сразу (схема, договоры, опрос), и два засева одновременно не должны
    уронить друг друга — второй просто увидит готовое. Урок «Финансов»: раздел
    открывался двумя запросами, оба создавали пространство, второй падал на
    уникальном ключе, и экран оставался пустым.
    """
    existing = _field_rows(session, workspace.id)
    if SYSTEM_KEYS <= set(existing) and existing and _has_main_view(session, workspace.id):
        return
    try:
        with session.begin_nested():
            _seed(session, workspace, existing)
    except IntegrityError:
        # Соседний запрос успел засеять — берём его результат.
        session.expire_all()


def _has_main_view(session: Session, workspace_id: uuid.UUID) -> bool:
    return (
        session.scalar(
            sa.select(sa.func.count())
            .select_from(EntityView)
            .where(EntityView.workspace_id == workspace_id, EntityView.main.is_(True))
        )
        or 0
    ) > 0


def _seed(session: Session, workspace: Workspace, existing: dict[str, EntityField]) -> None:
    first_time = not existing
    for index, item in enumerate(SYSTEM_FIELDS):
        if item.key in existing:
            continue
        session.add(
            EntityField(
                workspace_id=workspace.id,
                entity=ENTITY,
                key=item.key,
                system=True,
                type=item.type,
                title=item.title,
                names=list(item.names),
                hidden=item.hidden,
                position=(index + 1) * POSITION_STEP,
            )
        )
    if first_time:
        for index, (value, meaning) in enumerate(SEED_STATUSES):
            _seed_value(session, workspace.id, "status", value, meaning, index)
        for index, (value, meaning) in enumerate(SEED_TYPES):
            _seed_value(session, workspace.id, "type", value, meaning, index)
        for index, (value, system) in enumerate(SEED_ECONOMIC_ROLES):
            _seed_value(session, workspace.id, "economic_role", value, {"system": system}, index)
    if not _has_main_view(session, workspace.id):
        session.add(
            EntityView(
                workspace_id=workspace.id,
                entity=ENTITY,
                key="main",
                title="Все договоры",
                main=True,
                blocks=[{"title": "", "filter": {"any": []}, "roles": {}, "columns": [], "defaults": {}}],
                position=0,
            )
        )
    session.flush()
    bump(session, workspace.id, "schema")


def _seed_value(
    session: Session, workspace_id: uuid.UUID, field_key: str, value: str, meaning: dict, index: int
) -> None:
    session.add(
        ListValue(
            workspace_id=workspace_id,
            field_key=field_key,
            value=value,
            normalized=norm(value),
            meaning=dict(meaning),
            position=(index + 1) * POSITION_STEP,
        )
    )


def economic_role_values(session: Session, workspace_id: uuid.UUID) -> dict[str, ListValue]:
    """Значения хозяйственного смысла по системному смыслу: `revenue` → «Выручка».

    Если у компании несколько значений с одним системным смыслом («Агентский
    внутри ГК» поверх «Внутри группы»), подстановка берёт первое по порядку —
    своё значение человек выбирает руками.
    """
    rows = session.scalars(
        sa.select(ListValue)
        .where(
            ListValue.workspace_id == workspace_id,
            ListValue.field_key == "economic_role",
            ListValue.archived_at.is_(None),
        )
        .order_by(ListValue.position)
    )
    out: dict[str, ListValue] = {}
    for row in rows:
        system = (row.meaning or {}).get("system")
        if system in ECONOMIC_ROLES and system not in out:
            out[system] = row
    return out


@dataclass
class FieldView:
    """Поле, как его видит конкретный человек: подпись, тип, можно ли править."""

    key: str
    type: str
    title: str
    system: bool
    editable: bool
    required: bool
    hidden: bool
    position: int
    names: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out = {
            "key": self.key,
            "type": self.type,
            "title": self.title,
            "system": self.system,
            "editable": self.editable,
            "required": self.required,
            "hidden": self.hidden,
            "position": self.position,
        }
        if self.key in CHOICES:
            out["choices"] = [{"value": value, "label": label} for value, label in CHOICES[self.key]]
        return out


def fields_of(session: Session, workspace_id: uuid.UUID) -> list[EntityField]:
    """Живые поля реестра по порядку: системные и свои."""
    return list(
        session.scalars(
            sa.select(EntityField)
            .where(
                EntityField.workspace_id == workspace_id,
                EntityField.entity == ENTITY,
                EntityField.archived_at.is_(None),
            )
            .order_by(EntityField.position, EntityField.created_at)
        )
    )


def slug_for(title: str, taken: set[str]) -> str:
    """Ключ своего поля из подписи: «Источник клиента» → `istochnik_klienta`."""
    base = _translit(title) or "pole"
    base = base[:40].strip("_") or "pole"
    candidate, index = base, 2
    while candidate in taken or candidate in SYSTEM_KEYS:
        candidate = f"{base}_{index}"
        index += 1
    return candidate


_TRANSLIT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
        "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
        "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
        "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
        "я": "ya", "қ": "k", "ғ": "g", "ң": "n", "ү": "u", "ұ": "u", "ө": "o", "һ": "h",
        "ә": "a", "і": "i",
    }
)


def _translit(title: str) -> str:
    text = norm(title).translate(_TRANSLIT)
    return re.sub(r"[^0-9a-z]+", "_", text).strip("_")


__all__ = [
    "CHOICES",
    "ENTITY",
    "FIELD_BY_KEY",
    "FieldDef",
    "FieldView",
    "MODE_FIELDS",
    "SNAPSHOT_FIELDS",
    "SUBJECT_HINTS",
    "SYSTEM_FIELDS",
    "SYSTEM_KEYS",
    "SYSTEM_LISTS",
    "bump",
    "current",
    "economic_role_values",
    "ensure_registry",
    "fields_of",
    "number_key",
    "party_key",
    "slug_for",
    "subject_meaning",
]
