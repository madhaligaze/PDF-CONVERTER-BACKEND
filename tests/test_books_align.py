"""Выравнивание строк книги между импортами.

Проверяется то, ради чего модуль написан: книга без уникального ключа должна
сопоставляться правильно. Все случаи — на чистых функциях.
"""
from __future__ import annotations

from app.books.align import align, signature, similarity

FIELDS = ("date", "account", "amount", "comment")


def row(date="01.06.2026", account="Kaspi", amount="100", comment=""):
    return {"date": date, "account": account, "amount": amount, "comment": comment}


def how(alignment) -> list[str]:
    return [pair.how for pair in alignment.pairs]


# ── Слепок и похожесть ───────────────────────────────────────────────────────


def test_signature_ignores_formatting() -> None:
    """Двойной пробел — не различие.

    Без нормализации строка, в которой кто-то поправил отступ, выглядела бы
    удалённой и созданной заново.
    """
    assert signature(row(comment="за  услуги"), FIELDS) == signature(
        row(comment="За услуги"), FIELDS
    )


def test_similarity_ignores_fields_empty_on_both_sides() -> None:
    """Две почти пустые строки не «похожи на 90 %».

    Иначе совпадение двадцати восьми пустых колонок из тридцати объявляло бы
    разные операции одной и той же.
    """
    left = ("01.06.2026", "", "", "")
    right = ("02.07.2026", "", "", "")
    assert similarity(left, right) == 0.0


# ── Книга без ключа ──────────────────────────────────────────────────────────


def test_identical_rows_are_matched_in_order() -> None:
    """Три одинаковые строки сопоставляются трём одинаковым по порядку.

    Ровно этот случай ломал сопоставление по ключу: в живом журнале 358 полных
    двойников на 3632 строки.
    """
    rows = [row(comment="платёж"), row(comment="платёж"), row(comment="платёж")]
    result = align(rows, list(rows), fields=FIELDS)

    assert len(result.matched()) == 3
    assert all(pair.mirror_index == pair.source_index for pair in result.matched())


def test_unique_rows_become_anchors_without_any_key() -> None:
    """Уникальные по содержимому строки — якоря, даже когда ключа нет.

    Это приём patience diff. Без него книга остаётся одним отрезком на тысячи
    строк, упирается в потолок выравнивания и объявляется несопоставимой
    целиком — именно так пилотный «Журнал» и не импортировался.
    """
    rows = [row(comment=f"платёж {n}") for n in range(50)]
    result = align(rows, list(rows), fields=FIELDS)

    assert result.stats.get("anchor") == 50
    assert "missing" not in result.stats
    assert "created" not in result.stats


def test_insert_in_the_middle_shifts_nothing() -> None:
    """Вставили строку в середину — одна новая, остальные на месте.

    Сопоставление по номеру строки здесь сдвинуло бы половину книги.
    """
    before = [row(comment=f"п{n}") for n in range(20)]
    after = before[:10] + [row(comment="новая")] + before[10:]

    result = align(before, after, fields=FIELDS)
    assert result.stats.get("created") == 1
    assert len(result.matched()) == 20


def test_deleted_row_is_the_only_loss() -> None:
    before = [row(comment=f"п{n}") for n in range(20)]
    after = before[:5] + before[6:]

    result = align(before, after, fields=FIELDS)
    assert result.stats.get("missing") == 1
    assert len(result.matched()) == 19


# ── Правки ───────────────────────────────────────────────────────────────────


def test_edited_row_is_matched_by_similarity() -> None:
    """Правленая строка остаётся собой, а не становится другой.

    По слепку она отличается и в выравнивание не попадает; спасает четвёртый
    проход — тот же, которым `git diff -M` находит переименованные файлы.
    """
    before = [row(comment="п0"), row(comment="п1"), row(comment="п2")]
    after = [row(comment="п0"), row(comment="п1", amount="999"), row(comment="п2")]

    result = align(before, after, fields=FIELDS)
    similar = [pair for pair in result.pairs if pair.how == "similar"]
    assert len(similar) == 1
    assert similar[0].mirror_index == similar[0].source_index == 1


def test_row_below_the_threshold_is_not_an_edit() -> None:
    """Совпало меньше большинства полей — это другая операция."""
    before = [row(date="01.06.2026", account="Kaspi", amount="100", comment="аренда")]
    after = [row(date="15.09.2026", account="Halyk", amount="777", comment="зарплата")]

    result = align(before, after, fields=FIELDS)
    assert sorted(how(result)) == ["created", "missing"]


# ── Устойчивость ─────────────────────────────────────────────────────────────


def test_contradicting_anchors_do_not_cut_the_book_wrongly() -> None:
    """Строку перенесли — её якорь противоречит соседям и отбрасывается.

    Оставить оба нельзя: они разрезали бы книгу на отрезки, которые не
    соответствуют один другому. Берётся самая длинная непротиворечивая цепочка.
    """
    before = [row(comment="a"), row(comment="b"), row(comment="c"), row(comment="d")]
    after = [row(comment="d"), row(comment="a"), row(comment="b"), row(comment="c")]

    result = align(before, after, fields=FIELDS)
    # a, b, c идут по порядку в обеих версиях — они и составляют цепочку.
    matched = {(p.mirror_index, p.source_index) for p in result.matched()}
    assert (0, 1) in matched and (1, 2) in matched and (2, 3) in matched


def test_key_anchors_survive_an_edit_that_content_anchors_would_miss() -> None:
    """Якорь по ключу переживает правку строки, якорь по содержимому — нет.

    Поэтому проходов два, и ключевой идёт первым: номер договора остаётся
    собой, даже когда в строке поправили всё остальное.
    """
    before = [{"no": "Д-1", "amount": "100", "comment": "старый"}]
    after = [{"no": "Д-1", "amount": "999", "comment": "новый"}]
    fields = ("no", "amount", "comment")

    with_key = align(before, after, identity_fields=("no",), fields=fields)
    assert [pair.how for pair in with_key.pairs] == ["anchor"]

    without_key = align(before, after, fields=fields)
    # Без ключа совпало одно поле из трёх — это уже другая строка.
    assert sorted(how(without_key)) == ["created", "missing"]


def test_empty_rows_never_become_anchors() -> None:
    """Пустая строка не якорь: она связала бы два случайных места книги."""
    before = [row(comment="a"), {}, row(comment="b")]
    after = [row(comment="a"), {}, {}, row(comment="b")]

    result = align(before, after, fields=FIELDS)
    anchors = [p for p in result.pairs if p.how == "anchor"]
    assert all(before[p.mirror_index] for p in anchors)
