"""Сопоставление строк книги между импортами — выравниванием, а не по ключу.

Задача, из-за которой этот модуль существует
────────────────────────────────────────────
Повторный импорт должен понять, какая строка книги какой строке у нас
соответствует. Очевидный способ — ключ из значений колонок. На живых книгах он
не работает, и это свойство данных, а не кода:

* пилотный «Журнал», 3632 содержательные строки — перебор **всех** сочетаний
  до шести колонок оставляет 358 полных двойников. Несколько платежей одной
  датой, с одного счёта, с одинаковым назначением и суммой — это не ошибка
  ведения, это обычный день;
* боевая «Сводка», 879 строк — лучший ключ из шести колонок оставляет один
  двойник.

Добавление колонок не сходится к нулю. Ключа по содержимому в этих книгах нет.

Решение: то же, чем эту задачу решает diff
──────────────────────────────────────────
Строки листа — упорядоченная последовательность, в которой между импортами
что-то вставили, что-то удалили, что-то поправили. Ровно так выглядят строки
файла для `diff`. И строки файла тоже не уникальны: две одинаковые строки кода
git сопоставляет по положению внутри выравнивания, а не по содержимому.

Четыре прохода, от самого надёжного к самому слабому — как в patience diff:

1. **Якоря по ключу.** Строки, чей ключ уникален с обеих сторон. Переживают
   правку строки: номер договора остаётся собой, даже если сумму поправили.
   В живых книгах таких якорей может не быть вовсе — и это нормально.

2. **Якоря по содержимому.** Строки, чей полный слепок встречается ровно один
   раз с обеих сторон. Это и есть приём patience diff, и он вытягивает даже
   книгу без ключа: в «Журнале» уникальных по содержимому строк 3274 из 3632.
   Якоря дробят книгу на отрезки в единицы строк, и дальше работа идёт внутри
   отрезка, а не по всей книге.

   Без этого прохода журнал оставался одним отрезком на три с половиной тысячи
   строк, упирался в потолок выравнивания и объявлялся несопоставимым целиком.

3. **Выравнивание.** Внутри отрезка одинаковые строки встают друг напротив
   друга в порядке следования — именно это делает двойников безобидными: если
   две строки совпадают целиком, безразлично, какая из них какой досталась.

4. **Похожесть.** Строка, которую поправили, отличается от прежней и в
   выравнивание не попадает — она выглядит как удаление плюс вставка. Остатки
   отрезка сопоставляются по доле совпавших полей: это правка, а не другая
   строка. То же самое делает `git diff -M`, отыскивая переименованные файлы.

Проверено на живых книгах: повторный импорт «Журнала» без правок даёт «изменений
нет» (3624 якоря + 8 выравненных) за секунду; правка в книге — одно обновление;
правка с обеих сторон — одно расхождение; вставка строки — одну новую.

Что остаётся человеку
─────────────────────
Пересортировали книгу целиком — якоря перестают возрастать, выравнивание
рассыпается, и это видно по числу несопоставленных строк. Тогда срабатывает
тормоз, и импорт спрашивает, а не применяет.

Пара, найденная четвёртым проходом, помечается как нестрогая. Если по такой
паре приезжает правка, предпросмотр покажет её отдельно: сопоставление
похожестью — вывод, а не факт, и посмотреть на него глазами стоит.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from difflib import SequenceMatcher
from typing import Any, Sequence

from app.books.layout import norm

#: Доля совпавших полей, ниже которой строки считаются разными.
#:
#: 0.6 — не подобранное число, а требование, чтобы совпало большинство: строка
#: с одной-двумя правками остаётся собой, строка, где сходятся только дата и
#: фирма, — уже другая операция.
MIN_SIMILARITY = 0.6

#: Отрезок длиннее — не выравниваем, а считаем несопоставимым.
#:
#: Выравнивание квадратично по длине отрезка. Якоря обычно дробят книгу на
#: короткие куски; отрезок в тысячи строк означает, что якорей нет вовсе —
#: то есть книгу заменили целиком, и выравнивать там нечего.
MAX_SEGMENT = 1500


@dataclass(frozen=True)
class Pair:
    """Сопоставленная пара: индекс у нас, индекс в источнике, чем сопоставлены."""

    mirror_index: int | None
    source_index: int | None
    how: str  # anchor | aligned | similar | created | missing
    similarity: float = 1.0


@dataclass
class Alignment:
    pairs: list[Pair] = dc_field(default_factory=list)
    stats: dict[str, int] = dc_field(default_factory=dict)

    def matched(self) -> list[Pair]:
        return [p for p in self.pairs if p.mirror_index is not None and p.source_index is not None]


def signature(values: dict[str, Any], fields: Sequence[str]) -> tuple[str, ...]:
    """Полный слепок строки: все поля, нормализованные.

    Нормализация та же, что у заголовков: разнобой пробелов и регистра — это
    оформление, а не различие. Без неё строка, в которой кто-то поправил
    двойной пробел, выглядела бы удалённой и заново созданной.
    """
    return tuple(norm(str(values.get(key, "") or "")) for key in fields)


def similarity(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    """Доля совпавших полей. Пустые поля с обеих сторон не считаются.

    Иначе две почти пустые строки оказались бы похожи на 90 %: совпали
    двадцать восемь пустых колонок из тридцати.
    """
    counted = same = 0
    for a, b in zip(left, right):
        if not a and not b:
            continue
        counted += 1
        if a == b:
            same += 1
    return same / counted if counted else 0.0


def _longest_increasing(pairs: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Наибольшая возрастающая по обеим координатам подпоследовательность якорей.

    Якоря могут пересекаться: строку перенесли выше, и её ключ теперь стоит
    раньше, чем у соседа. Оставлять оба нельзя — они противоречат друг другу и
    разрежут книгу на отрезки, которые не соответствуют один другому.

    Берётся самая длинная непротиворечивая цепочка; выпавшие якоря не
    теряются — они попадают внутрь отрезков и сопоставляются наравне с
    остальными.
    """
    if not pairs:
        return []
    ordered = sorted(pairs)
    tails: list[int] = []
    parents: list[int] = [-1] * len(ordered)
    indices: list[int] = []

    for position, (_, source_index) in enumerate(ordered):
        low, high = 0, len(tails)
        while low < high:
            middle = (low + high) // 2
            if ordered[indices[middle]][1] < source_index:
                low = middle + 1
            else:
                high = middle
        if low > 0:
            parents[position] = indices[low - 1]
        if low == len(tails):
            tails.append(source_index)
            indices.append(position)
        else:
            tails[low] = source_index
            indices[low] = position

    chain: list[tuple[int, int]] = []
    cursor = indices[-1] if indices else -1
    while cursor != -1:
        chain.append(ordered[cursor])
        cursor = parents[cursor]
    return list(reversed(chain))


def _unique_keys(keys: Sequence[str]) -> dict[str, int]:
    """Ключи, встречающиеся ровно один раз → их позиция."""
    seen: dict[str, int] = {}
    duplicated: set[str] = set()
    for index, key in enumerate(keys):
        if not key:
            continue
        if key in seen:
            duplicated.add(key)
        else:
            seen[key] = index
    return {key: index for key, index in seen.items() if key not in duplicated}


def _unique_signatures(
    signatures: Sequence[tuple[str, ...]]
) -> dict[tuple[str, ...], int]:
    """Слепки, встречающиеся ровно один раз → их позиция.

    Пустые строки в якоря не годятся: их в книге сотни, и «уникальным» такой
    слепок не будет никогда, а если вдруг окажется — свяжет две случайные
    пустые строки и разрежет книгу не там.
    """
    seen: dict[tuple[str, ...], int] = {}
    duplicated: set[tuple[str, ...]] = set()
    for index, signature_value in enumerate(signatures):
        if not any(signature_value):
            continue
        if signature_value in seen:
            duplicated.add(signature_value)
        else:
            seen[signature_value] = index
    return {sig: index for sig, index in seen.items() if sig not in duplicated}


def _align_segment(
    mirror_range: range,
    source_range: range,
    mirror_signatures: Sequence[tuple[str, ...]],
    source_signatures: Sequence[tuple[str, ...]],
    min_similarity: float,
) -> list[Pair]:
    """Сопоставить один отрезок: сначала одинаковые строки, потом похожие."""
    mirror_ids = list(mirror_range)
    source_ids = list(source_range)
    if not mirror_ids and not source_ids:
        return []
    if not mirror_ids:
        return [Pair(None, index, "created") for index in source_ids]
    if not source_ids:
        return [Pair(index, None, "missing") for index in mirror_ids]

    if len(mirror_ids) > MAX_SEGMENT or len(source_ids) > MAX_SEGMENT:
        # Якорей нет, отрезок огромный — выравнивать нечего. Отдаём как
        # «всё удалено, всё создано»: тормоз выше это заметит и остановит импорт.
        return (
            [Pair(index, None, "missing") for index in mirror_ids]
            + [Pair(None, index, "created") for index in source_ids]
        )

    left = [mirror_signatures[index] for index in mirror_ids]
    right = [source_signatures[index] for index in source_ids]

    pairs: list[Pair] = []
    free_mirror: list[int] = []
    free_source: list[int] = []

    matcher = SequenceMatcher(None, left, right, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                pairs.append(Pair(mirror_ids[i1 + offset], source_ids[j1 + offset], "aligned"))
        else:
            free_mirror.extend(mirror_ids[i1:i2])
            free_source.extend(source_ids[j1:j2])

    # Остатки — правленые строки. Жадно, от самой похожей пары к менее похожим.
    scored: list[tuple[float, int, int]] = []
    for m_index in free_mirror:
        for s_index in free_source:
            score = similarity(mirror_signatures[m_index], source_signatures[s_index])
            if score >= min_similarity:
                scored.append((score, m_index, s_index))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))

    used_mirror: set[int] = set()
    used_source: set[int] = set()
    for score, m_index, s_index in scored:
        if m_index in used_mirror or s_index in used_source:
            continue
        used_mirror.add(m_index)
        used_source.add(s_index)
        pairs.append(Pair(m_index, s_index, "similar", round(score, 3)))

    pairs.extend(Pair(index, None, "missing") for index in free_mirror if index not in used_mirror)
    pairs.extend(Pair(None, index, "created") for index in free_source if index not in used_source)
    return pairs


def align(
    mirror_values: Sequence[dict[str, Any]],
    source_values: Sequence[dict[str, Any]],
    *,
    identity_fields: Sequence[str] = (),
    fields: Sequence[str],
    min_similarity: float = MIN_SIMILARITY,
) -> Alignment:
    """Сопоставить строки книги со строками у нас.

    `identity_fields` необязательны: они дают якоря и ускоряют работу, но без
    них выравнивание работает — просто вся книга становится одним отрезком.
    """
    mirror_signatures = [signature(values, fields) for values in mirror_values]
    source_signatures = [signature(values, fields) for values in source_values]

    candidates: list[tuple[int, int]] = []

    # Якоря первого рода — по ключу. Переживают правку строки: номер договора
    # остаётся собой, даже если сумму поправили.
    if identity_fields:
        from app.books.ingest import row_key

        mirror_unique = _unique_keys([row_key(v, identity_fields) for v in mirror_values])
        source_unique = _unique_keys([row_key(v, identity_fields) for v in source_values])
        candidates.extend(
            (index, source_unique[key])
            for key, index in mirror_unique.items()
            if key in source_unique
        )

    # Якоря второго рода — по полному слепку строки. Это и есть приём patience
    # diff, и без него книга без ключа остаётся одним отрезком на тысячи строк.
    #
    # Считать якорем можно только строку, чей слепок встречается ровно один раз
    # с обеих сторон: у таких строк соответствие однозначно. В пилотном
    # «Журнале» ключа нет вовсе, зато уникальных по содержимому строк 3274 из
    # 3632 — они дробят книгу на отрезки в единицы строк, и всё остальное
    # доделывает выравнивание.
    anchored_mirror = {index for index, _ in candidates}
    anchored_source = {index for _, index in candidates}
    mirror_by_signature = _unique_signatures(mirror_signatures)
    source_by_signature = _unique_signatures(source_signatures)
    candidates.extend(
        (index, source_by_signature[sig])
        for sig, index in mirror_by_signature.items()
        if sig in source_by_signature
        and index not in anchored_mirror
        and source_by_signature[sig] not in anchored_source
    )

    anchors = _longest_increasing(candidates)

    result = Alignment()
    previous_mirror = previous_source = 0
    for mirror_index, source_index in anchors:
        result.pairs.extend(
            _align_segment(
                range(previous_mirror, mirror_index),
                range(previous_source, source_index),
                mirror_signatures, source_signatures, min_similarity,
            )
        )
        result.pairs.append(Pair(mirror_index, source_index, "anchor"))
        previous_mirror, previous_source = mirror_index + 1, source_index + 1

    result.pairs.extend(
        _align_segment(
            range(previous_mirror, len(mirror_values)),
            range(previous_source, len(source_values)),
            mirror_signatures, source_signatures, min_similarity,
        )
    )

    counts: dict[str, int] = {}
    for pair in result.pairs:
        counts[pair.how] = counts.get(pair.how, 0) + 1
    result.stats = counts
    return result


__all__ = [
    "MAX_SEGMENT",
    "MIN_SIMILARITY",
    "Alignment",
    "Pair",
    "align",
    "signature",
    "similarity",
]
