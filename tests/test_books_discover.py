"""Обнаружение колонок в чужой книге.

Образцы здесь не выдуманы: каждый повторяет то, что нашлось в пилотной книге
«Копия Журнал ГК BBC (Бисултан)» — многострочные заголовки, колонки-разделители
подписанные точкой, даты с днём недели, суммы с неразрывным пробелом, сотни
пустых строк ниже данных.
"""
from __future__ import annotations

from app.books.discover import (
    detect_header_row,
    discover_fields,
    infer_type,
    last_data_row,
    sample_rows_of,
    slugify,
    unique_key,
)

NARROW = chr(0x202F)  # узкий неразрывный пробел — им в книгах разделяют разряды


# ── Ключи полей ──────────────────────────────────────────────────────────────


def test_slugify_transliterates() -> None:
    assert slugify("№ Договора") == "n_dogovora"
    assert slugify("Контрагент") == "kontragent"
    assert slugify("ДДС Мес (цифра)") == "dds_mes_cifra"


def test_slugify_collapses_yo_and_e() -> None:
    """«Платёж» и «Платеж» — одна колонка, а не две.

    Разнобой «ё/е» в книгах повсеместен, и без сведения к одному ключу
    повторный импорт считал бы переименованную колонку новой.
    """
    assert slugify("Платёж") == slugify("Платеж")


def test_separator_columns_get_positional_keys() -> None:
    """Колонки, подписанные точкой, ключа не дают — им достаётся позиционный.

    В пилотной книге таких три подряд, на позициях 15, 16 и 17.
    """
    taken: set[str] = set()
    assert unique_key(".", 15, taken) == "col_15"
    assert unique_key(".", 16, taken) == "col_16"
    assert unique_key("", 17, taken) == "col_17"


def test_duplicate_titles_get_numbered() -> None:
    taken: set[str] = set()
    assert unique_key("Сумма", 3, taken) == "summa"
    assert unique_key("Сумма", 9, taken) == "summa_2"
    assert unique_key("Сумма", 12, taken) == "summa_3"


# ── Определение типа ─────────────────────────────────────────────────────────


def test_money_recognised_by_formatting() -> None:
    values = [f"100{NARROW}000,00", f"50{NARROW}000,00", f"1{NARROW}950,00"]
    kind, stats = infer_type("Приход", values)
    assert kind == "money"
    assert stats["number_ratio"] == 1.0


def test_plain_year_is_a_number_not_money() -> None:
    """«ДДС Год» со значением 2026 — число, а не сумма.

    Ни разделителя разрядов, ни копеек, ни денежного слова в заголовке. Ошибка
    здесь стоила бы дорого: год, попавший в денежную роль, сложился бы в
    оборот.
    """
    kind, _ = infer_type("ДДС Год", ["2026", "2026", "1899"])
    assert kind == "number"


def test_money_recognised_by_header_even_without_formatting() -> None:
    kind, _ = infer_type("Сумма договора", ["100000", "250000", "80000"])
    assert kind == "money"


def test_dates_with_weekday_prefix() -> None:
    kind, stats = infer_type("Дата", ["пн 01.06.26", "ср 03.06.26", "чт 04.06.26"])
    assert kind == "date"
    assert stats["date_ratio"] == 1.0


def test_zeros_and_ones_are_numbers_not_flags() -> None:
    """Колонка из нулей и единиц — число.

    «0» и «1» разбираются и как число, и как ложь/истина. Без оговорки про
    словесные «да/нет» номер месяца объявлялся бы флажком, а форма показала бы
    для него чекбокс.
    """
    kind, _ = infer_type("Формула (через АПП Скрипт)", ["0", "1", "0", "1", "0"])
    assert kind == "number"


def test_words_make_a_flag() -> None:
    kind, _ = infer_type("Подписан", ["ДА", "НЕТ", "ДА", "TRUE", "FALSE"])
    assert kind == "bool"


def test_few_repeating_values_are_a_list() -> None:
    values = ["ИЮНЬ", "ИЮЛЬ", "АВГУСТ"] * 10
    kind, stats = infer_type("ДДС Месяц", values)
    assert kind == "enum"
    assert stats["options"] == ["АВГУСТ", "ИЮЛЬ", "ИЮНЬ"]


def test_many_distinct_values_are_text() -> None:
    kind, _ = infer_type("Контрагент", [f"ТОО «Клиент {n}»" for n in range(60)])
    assert kind == "text"


def test_empty_column_is_unknown_not_text() -> None:
    """Пустая колонка — `unknown`, а не `text`.

    Разница видна на табло привязок: «текст» приглашает привязать, `unknown`
    честно говорит, что о колонке пока ничего не известно.
    """
    kind, stats = infer_type("Проект", [])
    assert kind == "unknown"
    assert stats["filled"] == 0


def test_mixed_column_falls_back_to_text() -> None:
    """Даты в половине строк — это не колонка дат.

    Объявить её датой значит потерять вторую половину значений молча.
    """
    values = ["01.06.2026", "02.06.2026", "уточнить", "нет данных", "по договору"]
    kind, _ = infer_type("Дата", values)
    assert kind == "text"


# ── Шапка и выборка строк ────────────────────────────────────────────────────


def test_header_row_found_below_a_title_row() -> None:
    """Шапка не всегда первая: сверху бывает название отчёта и пустая строка."""
    grid = [
        ["Отчёт по движению денег за июнь 2026", "", "", ""],
        ["", "", "", ""],
        ["Дата", "Контрагент", "Приход", "Расход"],
        ["01.06.2026", "ТОО «Ромашка»", "100 000,00", ""],
    ]
    assert detect_header_row(grid) == 2


def test_empty_tail_is_not_counted_as_data() -> None:
    """Пустые строки ниже данных не занижают заполненность.

    Лист заводят с запасом: в пилотной книге 5917 строк при заметно меньшем
    числе содержательных. Считая хвост данными, каждую колонку можно объявить
    заполненной на несколько процентов.
    """
    grid = [["Дата", "Сумма"], ["01.06.2026", "100"], ["02.06.2026", "200"]]
    grid += [["", ""] for _ in range(500)]

    assert last_data_row(grid, 0) == 2
    assert len(sample_rows_of(grid, 0, 300)) == 2


def test_sample_spreads_across_the_whole_sheet() -> None:
    """Выборка берётся равномерно, а не сверху.

    В пилотном журнале сверху лежит текущий месяц, а «Категория» и «Вопросы»
    заполнены ниже. По окну из первых строк обе выходили пустыми и получали
    тип `unknown` — книга объявлялась беднее, чем она есть.
    """
    grid = [["Дата", "Категория"]]
    grid += [[f"0{1 + i % 9}.06.2026", ""] for i in range(400)]
    grid += [[f"0{1 + i % 9}.07.2026", "Доходы"] for i in range(400)]

    fields, _ = discover_fields(grid, sample_rows=200)
    category = next(f for f in fields if f.key == "kategoriya")
    assert category.type != "unknown"
    assert category.stats["filled"] > 0


def test_fill_ratio_shares_one_window_with_the_values() -> None:
    """Числитель и знаменатель заполненности берутся из одной выборки.

    Раньше значения набирались сканом всего листа, а делились на размер
    выборки: у разреженной колонки выходила смесь из числителя одного и
    знаменателя другого.
    """
    grid = [["Дата", "Сумма"]]
    grid += [["01.06.2026", "100" if i % 2 == 0 else ""] for i in range(100)]

    fields, _ = discover_fields(grid, sample_rows=50)
    amount = next(f for f in fields if f.key == "summa")
    assert amount.stats["scanned"] == 50
    assert amount.stats["filled"] == amount.stats["fill_ratio"] * 50


# ── Целиком ──────────────────────────────────────────────────────────────────


def test_multiline_header_is_read_as_one_title() -> None:
    """«Дробление\\n1 суммы» — один заголовок, а не два.

    Перенос строки в шапке — вёрстка. Оставленный в названии, он ломает и
    показ, и сравнение с синонимом роли, который человек пишет в одну строку.
    """
    grid = [["Дата\nДДС (дубль)", "Дробление\n1 суммы"], ["01.06.2026", "Общий 100тг"]]
    fields, _ = discover_fields(grid)
    assert fields[0].title == "Дата ДДС (дубль)"
    assert fields[1].title == "Дробление 1 суммы"


def test_discovery_describes_a_journal_shaped_sheet() -> None:
    grid = [
        ["Дата", "Счет", "Приход", "Расход", ".", "Фирма"],
        ["пн 01.06.26", "Kaspi", f"100{NARROW}000,00", "", "", "ТОО «А»"],
        ["ср 03.06.26", "Halyk", "", f"1{NARROW}950,00", "", "ТОО «Б»"],
        ["чт 04.06.26", "Kaspi", f"50{NARROW}000,00", "", "", "ТОО «А»"],
    ]
    fields, header_row = discover_fields(grid)

    assert header_row == 0
    by_key = {f.key: f for f in fields}
    assert by_key["data"].type == "date"
    assert by_key["prihod"].type == "money"
    assert by_key["rashod"].type == "money"
    # Разделитель опознан и помечен, но не выброшен: данные в нём тоже бывают.
    assert by_key["col_4"].stats["separator"] is True
    assert len(fields) == 6
