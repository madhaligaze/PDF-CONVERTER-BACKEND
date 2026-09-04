"""Повторный импорт: что изменилось в книге и что с этим делать.

Google продолжает жить, пока переход не закончен, а внутреннюю книгу уже
правят. Значит импорт — не перезапись, а слияние, и у него есть третья
сторона: то, что мы видели в источнике в прошлый раз.

    base   — как было в источнике, когда мы смотрели в него последний раз
    theirs — как в источнике сейчас
    ours   — как у нас сейчас

Таблица решений, по каждому полю отдельно:

    base  theirs  ours   →  что делаем
     A      A      A        ничего
     A      B      A        принимаем B: правка сделана в книге
     A      A      B        оставляем своё: правка сделана у нас
     A      B      B        сошлись, двигаем базу
     A      B      C        расхождение — обе версии целы, решает человек

Почему `base` обязателен
────────────────────────
Без него «в источнике B, у нас A» неразличимо с «в источнике B, у нас A, но A
мы сами только что и написали». В первом случае надо принять B, во втором —
затереть работу человека. Отличает их только память о прошлом чтении.

Почему по полям, а не по строкам
────────────────────────────────
Строка из тридцати колонок почти всегда трогается с обеих сторон: в книге
поправили сумму, у нас — комментарий. Слияние по строке объявило бы это
конфликтом и остановило бы работу на ровном месте. Расхождение — это когда
поправили **одно и то же** поле по-разному.

Импорт никогда не применяется молча
───────────────────────────────────
Всё, что здесь считается, — это план: сколько строк добавится, сколько
обновится, где расхождения. Человек смотрит и подтверждает. Применение —
отдельный шаг, и в этом модуле его нет.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from itertools import combinations
from typing import Any, Iterable, Sequence

from app.books.layout import norm

#: Разделитель частей ключа строки. Символ, которого не бывает в ячейках, —
#: иначе «Иванов|ООО» и «Иванов|» + «ООО» дали бы один ключ.
KEY_SEPARATOR = "\x1f"

#: До скольких полей пробуем составлять ключ строки. Три — потолок разумного:
#: если строка не опознаётся тремя колонками, ключ надо задавать руками.
MAX_IDENTITY_FIELDS = 3


# ── Что кладут на вход ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class SourceRow:
    """Строка, прочитанная из источника."""

    position: int
    values: dict[str, Any]


@dataclass(frozen=True)
class MirrorRow:
    """Строка внутренней книги — то, что у нас уже есть."""

    id: str
    values: dict[str, Any]
    base: dict[str, Any] = dc_field(default_factory=dict)
    source_key: str = ""
    origin: str = "source"
    state: str = "live"


# ── Что получается на выходе ─────────────────────────────────────────────────


@dataclass
class RowPlan:
    """Что произойдёт с одной строкой."""

    op: str  # create | update | unchanged | missing
    key: str
    row_id: str | None = None
    position: int = 0
    #: Для create — все значения новой строки.
    values: dict[str, Any] = dc_field(default_factory=dict)
    #: Поля, которые примем из источника.
    accepted: dict[str, Any] = dc_field(default_factory=dict)
    #: Новое значение базы по полям — то, что мы теперь «видели».
    base_updates: dict[str, Any] = dc_field(default_factory=dict)
    #: Поле → (было, в источнике, у нас). Не применяется, ждёт человека.
    conflicts: dict[str, tuple[Any, Any, Any]] = dc_field(default_factory=dict)
    #: Чем сопоставлена строка: anchor | aligned | similar | created | missing.
    #: Показывается в предпросмотре: «похоже» — это вывод, а не факт.
    how: str = "aligned"
    similarity: float = 1.0

    @property
    def touched(self) -> bool:
        return self.op != "unchanged"


@dataclass(frozen=True)
class Issue:
    """Что импорт не смог решить сам."""

    kind: str  # duplicate_in_source | duplicate_in_mirror | conflict | missing_in_source
    key: str
    detail: dict[str, Any] = dc_field(default_factory=dict)


@dataclass
class Plan:
    rows: list[RowPlan] = dc_field(default_factory=list)
    issues: list[Issue] = dc_field(default_factory=list)
    #: Заполнено — импорт не применяется, пока человек не разберётся.
    blocked_reason: str = ""
    #: Чем сопоставлены строки: сколько по якорю, по выравниванию, по похожести.
    alignment: dict[str, int] = dc_field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_reason)

    def summary(self) -> dict[str, int]:
        counts = {"create": 0, "update": 0, "unchanged": 0, "missing": 0, "conflict": 0}
        for row in self.rows:
            counts[row.op] = counts.get(row.op, 0) + 1
            if row.conflicts:
                counts["conflict"] += 1
        counts["issues"] = len(self.issues)
        return counts

    def describe(self) -> str:
        """Строка для человека: «примем 14 новых, обновим 3, 2 расхождения»."""
        counts = self.summary()
        parts = []
        if counts["create"]:
            parts.append(f"примем {counts['create']} новых")
        if counts["update"]:
            parts.append(f"обновим {counts['update']}")
        if counts["conflict"]:
            parts.append(f"{counts['conflict']} расхождений")
        if counts["missing"]:
            parts.append(f"{counts['missing']} пропали из книги")
        return ", ".join(parts) if parts else "изменений нет"


# ── Содержательные строки ────────────────────────────────────────────────────


def is_meaningful(
    values: dict[str, Any],
    substantive_fields: Sequence[str],
    fallback_fields: Sequence[str] = (),
) -> bool:
    """Есть ли в строке данные, а не только следы формул.

    В книгах на порядок больше строк, чем данных: лист заводят с запасом, между
    блоками оставляют пустые строки, снизу тянутся заготовки.

    `substantive_fields` — поля, привязанные к ролям дат и денег. Именно они
    отличают операцию от разлиновки, и это обобщение правила, которое годами
    работает в парсере журнала («есть дата либо сумма»): там колонки были
    известны заранее, здесь их называет привязка.

    Почему не «заполнено хоть одно привязанное поле»
    ───────────────────────────────────────────────
    Так и было написано сначала, и на пилотной книге правило не отсекло ни
    одной строки из 5916. Виновата колонка «Фирма»: это формула, и значение в
    ней стоит во всех строках подряд, включая пустые. Правило по датам и
    деньгам на той же книге даёт 3632 содержательные строки — ровно столько же,
    сколько насчитывает парсер журнала.

    `fallback_fields` нужны для книг, где ни дат, ни денег нет вовсе —
    справочников, списков сотрудников. Там содержательность определяется по
    любому привязанному полю: другого признака просто нет.
    """
    if substantive_fields:
        return any(_filled(values.get(key)) for key in substantive_fields)
    return any(_filled(values.get(key)) for key in fallback_fields)


def _filled(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def meaningful_rows(
    rows: Sequence[SourceRow],
    substantive_fields: Sequence[str],
    fallback_fields: Sequence[str] = (),
) -> list[SourceRow]:
    return [
        row for row in rows
        if is_meaningful(row.values, substantive_fields, fallback_fields)
    ]


# ── Ключ строки ──────────────────────────────────────────────────────────────


def row_key(values: dict[str, Any], identity_fields: Sequence[str]) -> str:
    """Ключ строки из значений полей. Пусто — строка не опознаётся.

    Значения нормализуются так же, как заголовки колонок: без разнобоя
    регистра, лишних пробелов и «ё». Иначе «ТОО Ромашка» и «ТОО  Ромашка»
    оказались бы разными строками, и повторный импорт удвоил бы книгу.
    """
    if not identity_fields:
        return ""
    parts: list[str] = []
    for key in identity_fields:
        value = values.get(key)
        text = norm("" if value is None else str(value))
        if not text:
            # Пустая часть ключа делает его ненадёжным: две строки без номера
            # договора слиплись бы в одну.
            return ""
        parts.append(text)
    return KEY_SEPARATOR.join(parts)


def propose_identity(
    rows: Sequence[dict[str, Any]],
    candidates: Sequence[str],
    *,
    max_fields: int = MAX_IDENTITY_FIELDS,
) -> tuple[str, ...]:
    """Наименьший набор полей, который различает все строки.

    Перебираются сочетания по одному полю, потом по два, потом по три —
    первое, где ключи всех строк заполнены и не повторяются, и есть ответ.

    Пусто означает «строки этой книги не различаются ни одним сочетанием из
    трёх полей». Это не повод угадывать: без ключа повторный импорт невозможен,
    и человеку надо об этом сказать, а не молча удваивать книгу при каждом
    заходе.
    """
    if not rows or not candidates:
        return ()

    for size in range(1, min(max_fields, len(candidates)) + 1):
        for combo in combinations(candidates, size):
            keys = [row_key(row, combo) for row in rows]
            if any(not key for key in keys):
                continue
            if len(set(keys)) == len(keys):
                return combo
    return ()


def _group_by_key(
    items: Iterable[Any], identity_fields: Sequence[str], values_of
) -> tuple[dict[str, list[Any]], list[Any]]:
    """Сгруппировать по ключу; строки без ключа — отдельно."""
    grouped: dict[str, list[Any]] = {}
    keyless: list[Any] = []
    for item in items:
        key = row_key(values_of(item), identity_fields)
        if not key:
            keyless.append(item)
            continue
        grouped.setdefault(key, []).append(item)
    return grouped, keyless


# ── Слияние ──────────────────────────────────────────────────────────────────


def _merge_row(
    key: str,
    mirror: MirrorRow,
    source: SourceRow,
    fields: Sequence[str],
) -> RowPlan:
    plan = RowPlan(op="unchanged", key=key, row_id=mirror.id, position=source.position)

    for field_key in fields:
        theirs = source.values.get(field_key)
        ours = mirror.values.get(field_key)
        base = mirror.base.get(field_key)

        if theirs == base:
            # В источнике ничего не менялось. Что бы ни было у нас — оставляем.
            continue
        if ours == base:
            # Правка сделана в книге, у нас поле не трогали.
            plan.accepted[field_key] = theirs
            plan.base_updates[field_key] = theirs
            continue
        if ours == theirs:
            # Поправили одинаково с обеих сторон — сошлись, спорить не о чем.
            plan.base_updates[field_key] = theirs
            continue
        # Поправили по-разному. Не применяем ничего и не двигаем базу: пока
        # человек не решит, расхождение должно оставаться видимым.
        plan.conflicts[field_key] = (base, theirs, ours)

    if plan.accepted or plan.base_updates or plan.conflicts:
        plan.op = "update"
    return plan


def merge(
    source_rows: Sequence[SourceRow],
    mirror_rows: Sequence[MirrorRow],
    *,
    identity_fields: Sequence[str] = (),
    fields: Sequence[str],
    change_limit_ratio: float = 0.3,
    change_limit_rows: int = 200,
    min_similarity: float = 0.6,
) -> Plan:
    """Посчитать, что изменится. Ничего не применяет.

    Строки сопоставляются выравниванием последовательностей (`books.align`), а
    не поиском по ключу: ключа по содержимому в живых книгах нет — в пилотном
    «Журнале» 358 полных двойников на 3632 строки. `identity_fields`
    необязательны и работают как якоря: они ускоряют выравнивание и делают его
    устойчивее, но без них оно тоже работает.

    `fields` — поля, участвующие в слиянии. Обычно все поля вкладки; колонка,
    объявленная нередактируемой, сюда не попадает.
    """
    from app.books.align import align

    plan = Plan()
    live_mirror = [row for row in mirror_rows if row.state == "live"]

    alignment = align(
        [row.values for row in live_mirror],
        [row.values for row in source_rows],
        identity_fields=identity_fields,
        fields=fields,
        min_similarity=min_similarity,
    )
    plan.alignment = alignment.stats

    for pair in alignment.pairs:
        if pair.source_index is None:
            mirror = live_mirror[pair.mirror_index]
            if mirror.origin != "source":
                # Строку завели в приложении — в книге её и не должно быть.
                continue
            key = row_key(mirror.values, identity_fields)
            plan.rows.append(RowPlan(op="missing", key=key, row_id=mirror.id, how="missing"))
            plan.issues.append(
                Issue(
                    kind="missing_in_source",
                    key=key,
                    detail={"row_id": mirror.id, "message": "строки больше нет в книге"},
                )
            )
            continue

        source = source_rows[pair.source_index]
        if pair.mirror_index is None:
            plan.rows.append(
                RowPlan(
                    op="create",
                    key=row_key(source.values, identity_fields),
                    position=source.position,
                    values=dict(source.values),
                    base_updates=dict(source.values),
                    how="created",
                )
            )
            continue

        mirror = live_mirror[pair.mirror_index]
        row_plan = _merge_row(
            row_key(source.values, identity_fields), mirror, source, fields
        )
        row_plan.how = pair.how
        row_plan.similarity = pair.similarity
        plan.rows.append(row_plan)

        for field_key, (base, theirs, ours) in row_plan.conflicts.items():
            plan.issues.append(
                Issue(
                    kind="conflict",
                    key=row_plan.key,
                    detail={
                        "row_id": row_plan.row_id,
                        "field": field_key,
                        "base": base,
                        "source": theirs,
                        "ours": ours,
                    },
                )
            )

        # Сопоставление похожестью — вывод, а не факт. Если по такой паре
        # приезжает правка, человек должен её увидеть, а не узнать из отчёта.
        if pair.how == "similar" and row_plan.accepted:
            plan.issues.append(
                Issue(
                    kind="fuzzy_match",
                    key=row_plan.key,
                    detail={
                        "row_id": mirror.id,
                        "similarity": pair.similarity,
                        "fields": sorted(row_plan.accepted),
                        "message": (
                            f"строка сопоставлена по похожести ({pair.similarity:.0%}) — "
                            f"проверьте: {', '.join(sorted(row_plan.accepted))}"
                        ),
                    },
                )
            )

    _apply_brake(plan, len(live_mirror), change_limit_ratio, change_limit_rows)
    return plan


def _apply_brake(
    plan: Plan, mirror_count: int, ratio: float, absolute: int
) -> None:
    """Тормоз на массовое изменение.

    Ловит случай, когда книгу использовали как песочницу: пересортировали,
    вставили поверх новую версию, удалили половину. Для приложения это
    выглядит как тысяча осмысленных правок, и применив их, оно отзеркалит
    катастрофу вместо того, чтобы её заметить.

    На первом импорте тормоз не работает: там всё — новые строки, и это норма.
    """
    if mirror_count == 0:
        return

    touched = sum(1 for row in plan.rows if row.touched)
    threshold = max(absolute, int(mirror_count * ratio))
    if touched > threshold:
        plan.blocked_reason = (
            f"Книга изменилась слишком сильно: затронуто {touched} строк из "
            f"{mirror_count}. Похоже, её пересортировали или заменили целиком. "
            "Импорт остановлен — проверьте книгу и подтвердите вручную."
        )


__all__ = [
    "KEY_SEPARATOR",
    "MAX_IDENTITY_FIELDS",
    "Issue",
    "MirrorRow",
    "is_meaningful",
    "meaningful_rows",
    "Plan",
    "RowPlan",
    "SourceRow",
    "merge",
    "propose_identity",
    "row_key",
]
