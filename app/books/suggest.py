"""Предложение привязок: какое поле на какую роль похоже.

Это то, что делает табло привязок работой на пять минут, а не на час. Человек
не раскладывает сорок колонок руками — он проверяет и правит предложенное.

Два источника
─────────────
1. **По названию.** Тремя ступенями `books.layout` — точное совпадение
   заголовка, сжатое, похожее. Ровно та же машинерия, которой парсер BBC
   привязывает колонки: если она годится для денег на экране начальника, она
   годится и здесь.
2. **По прошлому опыту.** Написание, однажды подтверждённое человеком, входит
   в синонимы роли и работает как поставляемое с продуктом. Следующая
   компания, назвавшая колонку так же, получит привязку сразу. Из этого
   каталог и умнеет.

Почему нет третьего — привязки по данным
────────────────────────────────────────
Он был написан и убран после проверки на живых книгах. Замысел выглядел
безопасным: связывать, только когда колонка такого типа в книге единственная и
роль такого типа осталась одна — выбор без выбора ошибиться не может.

На боевой сводке он ошибся с первого раза. Свободными остались роль «дата
операции» и вторая колонка «Счет», оказавшаяся датой; кандидат вышел ровно
один, и они связались. Но колонка осталась свободной не потому, что была
ничьей, — она проиграла борьбу за свою настоящую роль по типу. Единственность
остатка оказалась не доводом, а следствием того, что всё остальное уже
разобрали.

Счёт по итогу: ноль верных привязок, одна неверная — в денежной книге. Тип
колонки при этом никуда не делся: он виден на табло рядом с каждым полем
(«дата, заполнена на 97 %»), и человек тянет её сам. Подсказывать — полезно,
решать за него на таком основании — нет.

Чего здесь нет: угадывания
──────────────────────────
Предложение выдаётся, только когда кандидат ровно один. Два поля, одинаково
похожих на «сумму договора», дают отказ, а не выбор наугад, — и отказ виден на
табло как вопрос человеку. Это то же правило, что в `layout._pick`, и по той же
причине: ошибка в денежной колонке даёт уверенные неверные цифры, а это хуже,
чем отсутствие цифр.

Живой пример, ради которого правило и написано: заголовок «Счет» в мастер-книге
означает «счёт выставлен» (да/нет), а в журнале — расчётный счёт (текст). Двум
разным ролям подходит одно название, и разводит их только тип значений.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field as dc_field
from typing import Iterable, Sequence

from app.books.layout import Column, squash
from app.books.roles import Role, all_roles

#: Вес ступеней: чем меньше, тем надёжнее. Порядок обхода при разрешении
#: конфликтов — от самого надёжного к самому слабому.
TIERS: tuple[str, ...] = ("exact", "squashed", "loose")
_TIER_RANK = {tier: index for index, tier in enumerate(TIERS)}

#: Какие типы поля годятся под какой тип роли.
#:
#: Правила несимметричны, и это главное в них. Цена ошибки разная: колонка с
#: текстом, привязанная к роли денег, даёт пустоту вместо сумм; число,
#: привязанное к текстовой роли, не даёт ничего плохого — оно просто
#: показывается как есть.
#:
#: Отсюда: денежные, датовые и булевы роли строги, текстовые — терпимы, но и
#: они не берут даты и флажки. Иначе колонка дат ушла бы в текстовую роль
#: только потому, что похоже названа, и вместе с ней пропала бы возможность
#: считать по ней сроки.
#:
#: На этом же разводятся одинаково названные колонки. «Счет» в мастер-книге —
#: признак выставления счёта (да/нет), в журнале — расчётный счёт (текст). Имя
#: одно, тип разный, и потому в каждой книге подходит ровно одна роль.
COMPATIBLE: dict[str, frozenset[str]] = {
    "money": frozenset({"money", "number"}),
    "number": frozenset({"number", "money"}),
    "date": frozenset({"date"}),
    "bool": frozenset({"bool"}),
    "enum": frozenset({"enum", "text", "number"}),
    "text": frozenset({"text", "enum", "number"}),
}

#: Насколько похожими должны быть длины при нечётком совпадении.
#:
#: Порог введён после живой проверки: «Месяц ОПиу&Контрагент» — служебная
#: колонка-склейка — нечётко совпала с ролью «Месяц», потому что начинается с
#: неё, а «Счет за Период (Коммент)» — с ролью «Счёт». Формально это префикс,
#: по смыслу — совпадение первых пяти букв из двадцати.
#:
#: 0.6 оставляет полезное: «Коммент» ↔ «Комментарии» (0.64) и «Сумма Дог.» ↔
#: «Сумма Договора» (0.62) по-прежнему находятся.
LOOSE_MIN_RATIO = 0.6


def types_compatible(role_type: str, field_type: str) -> bool:
    """Годится ли поле такого типа под роль такого типа.

    `unknown` годится подо всё: колонка пуста, и утверждать о ней нечего.
    Запретить её значило бы заставить человека сначала наполнить книгу, а
    потом размечать.
    """
    if field_type == "unknown":
        return True
    return field_type in COMPATIBLE.get(role_type, frozenset({role_type}))


@dataclass(frozen=True)
class FieldView:
    """То, что предложению нужно знать о поле.

    Отдельный тип, а не модель из базы: предложение считается и для полей,
    которые ещё не сохранены — при первом импорте, до того как что-либо
    записано.
    """

    key: str
    title: str
    type: str
    names: tuple[str, ...] = ()

    @property
    def spellings(self) -> tuple[str, ...]:
        seen = [self.title, *self.names]
        return tuple(dict.fromkeys(name for name in seen if name))


@dataclass(frozen=True)
class Suggestion:
    field_key: str
    role_key: str
    confidence: str
    reason: str


@dataclass(frozen=True)
class Refusal:
    """Кандидатов несколько, и выбрать нельзя — вопрос человеку."""

    kind: str  # ambiguous_role | ambiguous_field
    role_key: str
    field_keys: tuple[str, ...]
    reason: str


@dataclass
class Proposal:
    suggestions: list[Suggestion] = dc_field(default_factory=list)
    refusals: list[Refusal] = dc_field(default_factory=list)

    @property
    def bound_roles(self) -> set[str]:
        return {item.role_key for item in self.suggestions}

    def by_field(self) -> dict[str, Suggestion]:
        return {item.field_key: item for item in self.suggestions}


def _role_column(role: Role, extra: Sequence[str] = ()) -> Column:
    """Роль как `Column` — чтобы искать её теми же тремя ступенями."""
    names = tuple(dict.fromkeys((*role.synonyms, *extra, role.title)))
    return Column(key=role.key, title=role.title, names=names)


def _name_candidates(
    fields: Sequence[FieldView],
    roles: Sequence[Role],
    learned: dict[str, Sequence[str]],
) -> tuple[list[tuple[int, str, str, str]], set[str]]:
    """Пары «поле ↔ роль» по названию и поля с узнаваемыми заголовками.

    Возвращается двумя частями, и это не удобство. «Совпало имя» и «подходит
    тип» — разные вопросы, и смешивать их нельзя: колонка «Счет», оказавшаяся
    датой, по имени узнаётся прекрасно, а по типу не годится ни одной из ролей
    с таким названием. Пара не рождается — но заголовок-то говорящий.

    Второе множество нужно догадке по данным: она применяется только к
    колонкам, чьё название не сказало вообще ничего. Без этого разделения
    «Счет», проигравший борьбу за свою роль по типу, выглядел бы ничьим — и
    доставался бы первой же роли, которой подходит его тип.
    """
    pairs: list[tuple[int, str, str, str]] = []
    recognised: set[str] = set()

    for role in roles:
        column = _role_column(role, learned.get(role.key, ()))
        for view in fields:
            tier, spelling = _match_tier(column, view)
            if tier is None:
                continue
            recognised.add(view.key)
            if not types_compatible(role.value_type, view.type):
                continue
            pairs.append((_TIER_RANK[tier], view.key, role.key,
                          _REASONS[tier].format(spelling=spelling)))
    return pairs, recognised


#: Обоснование привязки — то, что человек читает на табло рядом с полем.
_REASONS = {
    "exact": "заголовок «{spelling}» совпал с названием роли",
    "squashed": "заголовок «{spelling}» совпал без учёта знаков",
    "loose": "заголовок «{spelling}» похож на название роли",
}


def _match_tier(column: Column, view: FieldView) -> tuple[str | None, str]:
    """Самая надёжная ступень, на которой заголовок поля совпал с ролью."""
    for spelling in view.spellings:
        if column.matches_exact(spelling):
            return "exact", spelling
        if column.matches_squashed(spelling):
            return "squashed", spelling
        if column.matches_loose(spelling) and _lengths_close(column, spelling):
            return "loose", spelling
    return None, ""


def _lengths_close(column: Column, spelling: str) -> bool:
    """Не слишком ли разной длины совпавшие названия.

    `matches_loose` принимает любой префикс от четырёх символов, и для парсера
    этого достаточно: он сравнивает короткое с коротким. Здесь же заголовок
    может оказаться служебной склейкой на двадцать символов, начинающейся с
    названия роли, — формально префикс, по смыслу совпадение первых пяти букв.
    """
    actual = squash(spelling)
    for name in column.names:
        wanted = squash(name)
        if not wanted or not actual:
            continue
        if not (actual.startswith(wanted) or wanted.startswith(actual)):
            continue
        shorter, longer = sorted((len(actual), len(wanted)))
        if longer and shorter / longer >= LOOSE_MIN_RATIO:
            return True
    return False


def _resolve(
    pairs: Iterable[tuple[int, str, str, str]],
    proposal: Proposal,
    taken_fields: set[str],
    taken_roles: set[str],
) -> None:
    """Разложить пары по гнёздам, отказываясь там, где выбрать нельзя.

    Идём ступенями от надёжной к слабой: точное совпадение всегда бьёт похожее,
    поэтому «Дата» и «Дата ДДС (дубль)» не спорят за роль даты операции —
    первая совпала точно, вторая лишь похожа.

    Конфликт внутри одной ступени не разрешается вовсе. Он бывает двух видов, и
    оба означают вопрос к человеку, а не выбор монеткой:
    два поля claim одну роль, либо одно поле claim две роли.
    """
    ranked: dict[int, list[tuple[str, str, str]]] = defaultdict(list)
    for rank, field_key, role_key, reason in pairs:
        ranked[rank].append((field_key, role_key, reason))

    for rank in sorted(ranked):
        tier = TIERS[rank]
        live = [
            item for item in ranked[rank]
            if item[0] not in taken_fields and item[1] not in taken_roles
        ]

        by_role: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        by_field: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        for item in live:
            by_role[item[1]].append(item)
            by_field[item[0]].append(item)

        blocked_roles = {key for key, items in by_role.items() if len(items) > 1}
        blocked_fields = {key for key, items in by_field.items() if len(items) > 1}

        for role_key in sorted(blocked_roles):
            fields = tuple(sorted(item[0] for item in by_role[role_key]))
            proposal.refusals.append(
                Refusal(
                    kind="ambiguous_role",
                    role_key=role_key,
                    field_keys=fields,
                    reason=(
                        f"на роль претендуют сразу {len(fields)} колонки "
                        f"({', '.join(fields)}) — выберите нужную"
                    ),
                )
            )
        for field_key in sorted(blocked_fields):
            roles = tuple(sorted(item[1] for item in by_field[field_key]))
            proposal.refusals.append(
                Refusal(
                    kind="ambiguous_field",
                    role_key="",
                    field_keys=(field_key,),
                    reason=(
                        f"колонка подходит сразу на {len(roles)} роли "
                        f"({', '.join(roles)}) — выберите нужную"
                    ),
                )
            )

        for field_key, role_key, reason in live:
            if role_key in blocked_roles or field_key in blocked_fields:
                continue
            if field_key in taken_fields or role_key in taken_roles:
                continue
            proposal.suggestions.append(
                Suggestion(field_key=field_key, role_key=role_key,
                           confidence=tier, reason=reason)
            )
            taken_fields.add(field_key)
            taken_roles.add(role_key)


def propose(
    fields: Sequence[FieldView],
    *,
    roles: Sequence[Role] | None = None,
    learned: dict[str, Sequence[str]] | None = None,
) -> Proposal:
    """Предложить привязки для полей книги.

    `learned` — выученные синонимы на это рабочее пространство:
    `{ключ роли: [написания]}`. Они дописываются к синонимам роли и участвуют
    в тех же трёх ступенях, то есть подтверждённое человеком написание работает
    так же, как поставляемое с продуктом.
    """
    catalog = list(roles if roles is not None else all_roles())
    proposal = Proposal()
    taken_fields: set[str] = set()
    taken_roles: set[str] = set()

    by_name, _ = _name_candidates(fields, catalog, learned or {})
    _resolve(by_name, proposal, taken_fields, taken_roles)
    return proposal


__all__ = [
    "COMPATIBLE",
    "LOOSE_MIN_RATIO",
    "TIERS",
    "FieldView",
    "Proposal",
    "Refusal",
    "Suggestion",
    "propose",
    "types_compatible",
]
