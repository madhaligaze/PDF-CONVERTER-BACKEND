"""Повторный импорт: таблица слияния и предохранители.

Всё на чистых функциях — ни базы, ни Google. Поэтому таблица решений
проверяется целиком, а не по одному случаю на удачу.
"""
from __future__ import annotations

from app.books.ingest import (
    MirrorRow,
    SourceRow,
    merge,
    propose_identity,
    row_key,
)

FIELDS = ("contract_no", "client", "amount", "comment")
IDENTITY = ("contract_no",)


def source(position: int, **values) -> SourceRow:
    return SourceRow(position=position, values=values)


def mirror(row_id: str, base: dict | None = None, **values) -> MirrorRow:
    return MirrorRow(
        id=row_id,
        values=values,
        base=base if base is not None else dict(values),
        origin="source",
    )


def run(source_rows, mirror_rows, **kwargs):
    return merge(
        source_rows, mirror_rows,
        identity_fields=kwargs.pop("identity_fields", IDENTITY),
        fields=kwargs.pop("fields", FIELDS),
        **kwargs,
    )


# ── Таблица слияния, строка за строкой ───────────────────────────────────────


def test_nothing_changed_anywhere() -> None:
    plan = run(
        [source(2, contract_no="Д-1", amount="100")],
        [mirror("r1", contract_no="Д-1", amount="100")],
    )
    assert [row.op for row in plan.rows] == ["unchanged"]
    assert plan.summary()["update"] == 0


def test_changed_only_in_the_book_is_accepted() -> None:
    """Правку в книге принимаем: у нас поле не трогали."""
    plan = run(
        [source(2, contract_no="Д-1", amount="200")],
        [mirror("r1", contract_no="Д-1", amount="100")],
    )
    row = plan.rows[0]
    assert row.op == "update"
    assert row.accepted == {"amount": "200"}
    assert not row.conflicts


def test_changed_only_here_is_kept() -> None:
    """Нашу правку не затираем, даже если в книге лежит старое значение.

    Отличает этот случай от предыдущего только память о прошлом чтении: в
    источнике «100», у нас «200», и база говорит, что «100» мы уже видели, —
    значит двести написали мы.
    """
    plan = run(
        [source(2, contract_no="Д-1", amount="100")],
        [mirror("r1", base={"contract_no": "Д-1", "amount": "100"},
                contract_no="Д-1", amount="200")],
    )
    row = plan.rows[0]
    assert row.op == "unchanged"
    assert row.accepted == {}
    assert not row.conflicts


def test_changed_the_same_way_on_both_sides_converges() -> None:
    plan = run(
        [source(2, contract_no="Д-1", amount="300")],
        [mirror("r1", base={"contract_no": "Д-1", "amount": "100"},
                contract_no="Д-1", amount="300")],
    )
    row = plan.rows[0]
    assert row.conflicts == {}
    assert row.base_updates["amount"] == "300"
    assert row.accepted == {}


def test_changed_differently_is_a_conflict_and_nothing_is_overwritten() -> None:
    """Обе версии целы, ни одна не применена.

    Это единственный случай, где импорт останавливается перед полем. Решать
    монеткой нельзя: с обеих сторон осмысленная работа человека.
    """
    plan = run(
        [source(2, contract_no="Д-1", amount="300")],
        [mirror("r1", base={"contract_no": "Д-1", "amount": "100"},
                contract_no="Д-1", amount="200")],
    )
    row = plan.rows[0]
    assert row.conflicts == {"amount": ("100", "300", "200")}
    assert row.accepted == {}
    assert "amount" not in row.base_updates  # база не двигается до решения

    conflict = next(i for i in plan.issues if i.kind == "conflict")
    assert conflict.detail["source"] == "300"
    assert conflict.detail["ours"] == "200"


def test_merge_is_per_field_not_per_row() -> None:
    """Правка суммы в книге и комментария у нас — не конфликт.

    Строка из тридцати колонок почти всегда трогается с обеих сторон. Слияние
    по строке объявляло бы это расхождением и останавливало работу на ровном
    месте.
    """
    plan = run(
        [source(2, contract_no="Д-1", amount="300", comment="старый")],
        [mirror("r1", base={"contract_no": "Д-1", "amount": "100", "comment": "старый"},
                contract_no="Д-1", amount="100", comment="мой новый")],
    )
    row = plan.rows[0]
    assert row.accepted == {"amount": "300"}
    assert row.conflicts == {}


# ── Появление и исчезновение строк ───────────────────────────────────────────


def test_new_row_in_the_book_is_created() -> None:
    plan = run(
        [source(2, contract_no="Д-1", amount="100"),
         source(3, contract_no="Д-2", amount="200")],
        [mirror("r1", contract_no="Д-1", amount="100")],
    )
    created = [row for row in plan.rows if row.op == "create"]
    assert len(created) == 1
    assert created[0].values["contract_no"] == "Д-2"
    # У новой строки база сразу равна источнику: мы только что её и увидели.
    assert created[0].base_updates == created[0].values


def test_row_gone_from_the_book_is_marked_not_deleted() -> None:
    """Пропавшую строку помечаем, но не удаляем.

    В книге её могли удалить, а у нас она уже поправлена человеком — вместе с
    ней исчезла бы его работа. Решение оставляем человеку.
    """
    plan = run(
        [source(2, contract_no="Д-1", amount="100")],
        [mirror("r1", contract_no="Д-1", amount="100"),
         mirror("r2", contract_no="Д-2", amount="200")],
    )
    missing = [row for row in plan.rows if row.op == "missing"]
    assert [row.row_id for row in missing] == ["r2"]
    assert any(i.kind == "missing_in_source" for i in plan.issues)


def test_row_created_in_the_app_is_not_reported_as_missing() -> None:
    """Строку, заведённую в приложении, в книге искать не надо."""
    own = MirrorRow(id="r9", values={"contract_no": "Д-9"}, base={}, origin="app")
    plan = run([source(2, contract_no="Д-1")], [own])

    assert not [row for row in plan.rows if row.op == "missing"]


# ── Двойники ─────────────────────────────────────────────────────────────────


def test_identical_twins_are_matched_by_order_not_refused() -> None:
    """Две одинаковые строки — не повод отказываться.

    Раньше такой ключ выводился из слияния целиком: выбрать «правильную» из
    двух одинаковых нечем. Но в живом журнале таких строк 358 из 3632 —
    несколько платежей одной датой, с одного счёта, с одинаковым назначением.
    Отказ означал бы, что книга не импортируется вовсе.

    Выравнивание решает это так же, как diff решает одинаковые строки кода:
    ставит их друг напротив друга по порядку следования. Если строки совпадают
    целиком, безразлично, какая из них какой досталась.
    """
    plan = run(
        [source(2, contract_no="Д-1", amount="100"),
         source(3, contract_no="Д-1", amount="100")],
        [mirror("r1", contract_no="Д-1", amount="100"),
         mirror("r2", contract_no="Д-1", amount="100")],
    )
    assert [row.op for row in plan.rows] == ["unchanged", "unchanged"]
    assert not plan.issues


def test_edited_row_is_recognised_as_the_same_row() -> None:
    """Правленая строка — это правка, а не удаление плюс вставка.

    По слепку она отличается от прежней и в выравнивание не попадает. Остаток
    сопоставляется по доле совпавших полей — то же самое делает `git diff -M`,
    отыскивая переименованные файлы.
    """
    plan = run(
        [source(2, contract_no="Д-1", client="Ромашка", amount="999", comment="как было")],
        [mirror("r1", contract_no="Д-1", client="Ромашка", amount="100", comment="как было")],
        # Без якоря: иначе строка найдётся точным ключом, и путь по похожести
        # не проверится вовсе. В живых книгах якорей на всё не хватает.
        identity_fields=(),
    )
    row = plan.rows[0]
    assert row.op == "update"
    assert row.row_id == "r1"
    assert row.accepted == {"amount": "999"}
    assert row.how == "similar"


def test_fuzzy_match_with_a_change_is_shown_to_the_human() -> None:
    """Сопоставление похожестью — вывод, а не факт.

    Если по такой паре приезжает правка, человек должен увидеть её в
    предпросмотре, а не узнать из отчёта через месяц.
    """
    plan = run(
        [source(2, contract_no="Д-1", client="Ромашка", amount="999", comment="как было")],
        [mirror("r1", contract_no="Д-1", client="Ромашка", amount="100", comment="как было")],
        # Без якоря: иначе строка найдётся точным ключом, и путь по похожести
        # не проверится вовсе. В живых книгах якорей на всё не хватает.
        identity_fields=(),
    )
    fuzzy = next(i for i in plan.issues if i.kind == "fuzzy_match")
    assert fuzzy.detail["fields"] == ["amount"]
    assert 0 < fuzzy.detail["similarity"] <= 1


def test_a_row_too_different_is_a_new_row_not_an_edit() -> None:
    """Совпали меньше половины полей — это другая операция, а не правка."""
    plan = run(
        [source(2, contract_no="Д-9", client="Лютик", amount="777", comment="иное")],
        [mirror("r1", contract_no="Д-1", client="Ромашка", amount="100", comment="как было")],
    )
    assert sorted(row.op for row in plan.rows) == ["create", "missing"]


# ── Предохранители ───────────────────────────────────────────────────────────


def test_import_works_without_any_identity_fields() -> None:
    """Ключ необязателен: без него вся книга — один отрезок выравнивания.

    Ключ даёт якоря — точки, по которым книга делится на соответственные
    отрезки. Это делает выравнивание быстрее и устойчивее, но не является его
    условием. В живых книгах уникального ключа нет вовсе, и требовать его
    значило бы не импортировать ничего.
    """
    plan = run(
        [source(2, contract_no="Д-1", amount="100"),
         source(3, contract_no="Д-2", amount="200")],
        [mirror("r1", contract_no="Д-1", amount="100")],
        identity_fields=(),
    )
    assert not plan.blocked
    assert plan.summary()["create"] == 1
    assert plan.summary()["unchanged"] == 1


def test_mass_change_stops_the_import() -> None:
    """Книгу заменили целиком — импорт встаёт и спрашивает.

    Для приложения это выглядит как сотня осмысленных правок. Применив их, оно
    отзеркалило бы катастрофу вместо того, чтобы её заметить.
    """
    mirror_rows = [mirror(f"r{n}", contract_no=f"Д-{n}", amount="100") for n in range(100)]
    source_rows = [source(n + 2, contract_no=f"Д-{n}", amount="999") for n in range(100)]

    plan = merge(
        source_rows, mirror_rows,
        identity_fields=IDENTITY, fields=FIELDS,
        change_limit_ratio=0.3, change_limit_rows=10,
    )
    assert plan.blocked
    assert "изменилась слишком сильно" in plan.blocked_reason


def test_first_import_is_never_blocked() -> None:
    """На первом импорте всё — новые строки, и это норма, а не катастрофа."""
    source_rows = [source(n + 2, contract_no=f"Д-{n}") for n in range(500)]
    plan = merge(
        source_rows, [],
        identity_fields=IDENTITY, fields=FIELDS,
        change_limit_ratio=0.3, change_limit_rows=10,
    )
    assert not plan.blocked
    assert plan.summary()["create"] == 500


def test_small_change_passes_the_brake() -> None:
    mirror_rows = [mirror(f"r{n}", contract_no=f"Д-{n}", amount="100") for n in range(100)]
    source_rows = [source(2, contract_no="Д-0", amount="999")] + [
        source(n + 3, contract_no=f"Д-{n}", amount="100") for n in range(1, 100)
    ]
    plan = merge(
        source_rows, mirror_rows,
        identity_fields=IDENTITY, fields=FIELDS,
        change_limit_ratio=0.3, change_limit_rows=10,
    )
    assert not plan.blocked
    assert plan.summary()["update"] == 1


# ── Ключ строки ──────────────────────────────────────────────────────────────


def test_key_ignores_spacing_and_case() -> None:
    """Иначе «ТОО  Ромашка» и «ТОО Ромашка» стали бы разными строками.

    А повторный импорт удвоил бы книгу — молча и целиком.
    """
    assert row_key({"a": "ТОО  Ромашка"}, ("a",)) == row_key({"a": "тоо ромашка"}, ("a",))


def test_key_with_an_empty_part_is_no_key() -> None:
    """Пустая часть делает ключ ненадёжным: строки слиплись бы в одну."""
    assert row_key({"a": "Д-1", "b": ""}, ("a", "b")) == ""


def test_identity_is_proposed_as_the_smallest_unique_combination() -> None:
    rows = [
        {"contract": "Д-1", "client": "А", "date": "01.06.2026"},
        {"contract": "Д-1", "client": "Б", "date": "02.06.2026"},
        {"contract": "Д-2", "client": "А", "date": "01.06.2026"},
    ]
    # Ни одна колонка по отдельности не различает строки, пара — различает.
    assert propose_identity(rows, ("contract", "client", "date")) == ("contract", "client")


def test_identity_prefers_a_single_field_when_it_is_enough() -> None:
    rows = [{"contract": "Д-1", "client": "А"}, {"contract": "Д-2", "client": "А"}]
    assert propose_identity(rows, ("contract", "client")) == ("contract",)


def test_no_identity_is_an_honest_empty_answer() -> None:
    """Строки не различаются ничем — значит повторный импорт невозможен.

    Это не повод угадывать: без ключа каждый заход удваивал бы книгу.
    """
    rows = [{"a": "одно", "b": "то же"}, {"a": "одно", "b": "то же"}]
    assert propose_identity(rows, ("a", "b")) == ()


# ── Содержательные строки ────────────────────────────────────────────────────


def test_formula_column_does_not_make_a_row_meaningful() -> None:
    """Строка со следом формулы — не строка данных.

    Правило «заполнено хоть одно привязанное поле» было написано первым и на
    пилотной книге не отсекло ни одной строки из 5916. Виновата колонка
    «Фирма»: это формула, и значение в ней стоит во всех строках подряд,
    включая пустые.
    """
    from app.books.ingest import is_meaningful

    junk = {"firm": "ТОО «Ромашка»"}          # только след формулы
    real = {"firm": "ТОО «Ромашка»", "amount": "1000"}

    assert not is_meaningful(junk, substantive_fields=("date", "amount"))
    assert is_meaningful(real, substantive_fields=("date", "amount"))


def test_book_without_dates_and_money_falls_back_to_any_bound_field() -> None:
    """Справочник — тоже книга, и его строки тоже содержательны.

    Там нет ни дат, ни сумм, и другого признака, кроме «заполнено хоть
    что-то», просто не существует.
    """
    from app.books.ingest import is_meaningful

    row = {"name": "Иванов", "position": "бухгалтер"}
    assert is_meaningful(row, substantive_fields=(), fallback_fields=("name",))
    assert not is_meaningful({}, substantive_fields=(), fallback_fields=("name",))


def test_meaningful_rows_filters_the_junk() -> None:
    from app.books.ingest import meaningful_rows

    rows = [
        source(2, firm="ТОО", amount="100"),
        source(3, firm="ТОО"),
        source(4, firm="ТОО", amount="200"),
    ]
    kept = meaningful_rows(rows, ("amount",), ("firm",))
    assert [row.position for row in kept] == [2, 4]
