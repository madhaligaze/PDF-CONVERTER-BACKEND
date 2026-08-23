"""Разбор листа «Сводка все ЮР лица»: раскладка и долг.

Эти два предмета проверяются вместе не случайно. Долг читается из колонки
«Дебет / Кредит», и если книга сдвинется, парсер обязан прочитать её на новом
месте, а не соседнюю ячейку — иначе на экран уедет правдоподобная, но неверная
сумма денег. Поэтому здесь проверяется и то, что сдвиг переживается, и то, что
неопознанная раскладка останавливает чтение.
"""
from __future__ import annotations

import pytest

from app.bbc.dataset import (
    EXPECTED_SPREADSHEET_ID,
    EXPECTED_WORKSHEET,
    MASTER_COLUMNS,
    LayoutError,
    parse_contract_row,
    parse_contract_rows,
    parse_dataset,
    resolve_master_layout,
)

#: Ширина книги с запасом — реальный лист на август 2026 имеет 61 колонку.
WIDTH = 61

#: Логическое имя колонки → её позиция в эталонной книге.
HINT = {column.key: column.hint for column in MASTER_COLUMNS}


def header() -> list[str]:
    """Шапка эталонной книги: каждое название на своём месте."""
    row = [""] * WIDTH
    for column in MASTER_COLUMNS:
        if column.hint is not None:
            row[column.hint] = column.names[0]
    return row


def sheet_row(**cells) -> list[str]:
    """Строка листа. «Вид Услуги» по умолчанию заполнен: пустой означает
    «договор не вступил в силу», и тест на долг молча считал бы ноль."""
    row = [""] * WIDTH
    row[HINT["service_kind"]] = "Абон.П."
    for name, value in cells.items():
        row[HINT[name]] = value
    return row


LAYOUT = resolve_master_layout(header())


def parse_row(index: int, row: list[str], layout=None):
    return parse_contract_row(index, row, layout or LAYOUT)


# ── Раскладка ───────────────────────────────────────────────────────────────


def test_the_reference_header_resolves_every_column() -> None:
    layout = resolve_master_layout(header())
    assert not layout.shifted
    assert not layout.absent
    for column in MASTER_COLUMNS:
        assert layout.at(column.key) == column.hint, column.key


def test_the_expected_book_is_recorded_in_the_code() -> None:
    """`.env` в репозиторий не попадает, поэтому нужная книга записана в коде."""
    assert EXPECTED_SPREADSHEET_ID == "1xEp_QEirE49gREHrSvXwcYJRO1ZTVVzGF4Web43tDvI"
    assert EXPECTED_WORKSHEET == "Сводка все ЮР лица"


def test_a_column_inserted_at_the_front_no_longer_breaks_anything() -> None:
    """Ровно та поломка, из-за которой дашборд встал: колонка сдвинула все.

    Раньше это была ошибка «раскладка не совпала». Теперь колонки находятся по
    названию, сдвиг переживается, а долг читается с нового места — не с соседнего.
    """
    grid = [[""] + header(), [""] + sheet_row(client="ТОО Тест", debit_credit="85 000")]
    rows, layout = parse_dataset(grid)
    assert layout is not None and layout.shifted
    assert rows[0].client == "ТОО Тест"
    assert rows[0].debt == 85_000


def test_the_august_2026_avr_columns_are_survived() -> None:
    """В блок АВР добавили четыре колонки — всё правее уехало на +4.

    Это настоящая поломка августа 2026: «Сальдо Конец» переехал с 42 на 46,
    «Дебет / Кредит» с 47 на 51. Долг обязан приехать целым.
    """
    #: Книга ДО августа: четырёх новых колонок нет, поэтому всё, что стояло
    #: правее блока АВР, лежит на 4 позиции левее.
    NEW = {"avr_accepted", "esf_sent"}
    INSERT_AT = HINT["avr_date"] + 1

    def old_position(column) -> int:
        return column.hint - 4 if column.hint > INSERT_AT else column.hint

    old_header = [""] * (WIDTH - 4)
    positions: dict[str, int] = {}
    for column in MASTER_COLUMNS:
        if column.hint is None or column.key in NEW:
            continue
        positions[column.key] = old_position(column)
        old_header[positions[column.key]] = column.names[-1]

    old_row = [""] * (WIDTH - 4)
    old_row[positions["service_kind"]] = "Абон.П."
    old_row[positions["client"]] = "ИП Vector"
    old_row[positions["debit_credit"]] = "1 200 000"
    old_row[positions["saldo_end"]] = "-1 200 000"

    rows, layout = parse_dataset([old_header, old_row])

    assert layout is not None
    # Именно те позиции, что были до августа, — и парсер их находит.
    assert layout.at("saldo_end") == 42
    assert layout.at("debit_credit") == 47
    assert layout.shifted, "переезд обязан быть виден в примечании"
    assert set(layout.absent) >= NEW
    # Главное: деньги приехали целыми, а не из соседней ячейки.
    assert rows[0].debt == 1_200_000
    assert rows[0].saldo_end == -1_200_000


def test_a_renamed_required_column_stops_the_read() -> None:
    """Переименование — единственное, что теперь ломает чтение. И должно."""
    broken = header()
    broken[HINT["department"]] = "Направление"
    with pytest.raises(LayoutError) as caught:
        resolve_master_layout(broken)
    assert "Отдел" in str(caught.value)


def test_the_error_shows_what_stands_near_the_old_position() -> None:
    """Ошибка обязана подсказывать, чем колонку заменили, а не только что её нет."""
    broken = header()
    broken[HINT["debit_credit"]] = "Итого к оплате"
    with pytest.raises(LayoutError) as caught:
        resolve_master_layout(broken)
    assert "итого к оплате" in str(caught.value).lower()


def test_optional_columns_may_be_absent() -> None:
    """Старая копия книги без колонок «АВР (клиент принял)» и «ЭСФ» читается."""
    old = header()
    old[HINT["avr_accepted"]] = ""
    old[HINT["esf_sent"]] = ""
    layout = resolve_master_layout(old)
    assert set(layout.absent) >= {"avr_accepted", "esf_sent"}
    row = parse_row(2, sheet_row(debit_credit="1 000"), layout)
    assert row.avr_accepted is None and row.esf_sent is None


def test_parsing_refuses_a_sheet_with_a_foreign_layout() -> None:
    with pytest.raises(LayoutError):
        parse_contract_rows([[""] * WIDTH, sheet_row(client="ТОО Тест")])


def test_an_empty_grid_is_not_an_error() -> None:
    assert parse_contract_rows([]) == []


# ── Долг ────────────────────────────────────────────────────────────────────


def test_debt_is_read_from_the_book() -> None:
    row = parse_row(2, sheet_row(client="ИП Vector", debit_credit="85 000"))
    assert row.debt == 85_000
    assert row.debt_pending is False
    assert row.debt_broken is False
    assert row.total_debt == 85_000


def test_a_period_that_has_not_started_is_not_debt() -> None:
    """«Еще рано» — книга сама решает, когда долг становится долгом."""
    row = parse_row(2, sheet_row(contract_amount="85 000", debit_credit="Еще рано"))
    assert row.debt is None
    assert row.debt_pending is True
    assert row.total_debt == 0


def test_a_formula_error_is_marked_rather_than_silently_zeroed() -> None:
    row = parse_row(2, sheet_row(debit_credit="#VALUE!"))
    assert row.debt is None
    assert row.debt_broken is True
    assert row.debt_pending is False


# ── Входящий остаток ────────────────────────────────────────────────────────


def contract(*periods: tuple[str, int, str, str]) -> list:
    """Строки одного договора: (период, мес, сальдо начало, долг)."""
    return [
        [""] * WIDTH,  # шапку parse_contract_rows пропускает
        *[
            sheet_row(
                client="ИП Тест",
                contract_no="№1",
                service_kind="Абон.П.",
                period_label=label,
                month=str(month),
                saldo_start=saldo,
                debit_credit=debt,
            )
            for label, month, saldo, debt in periods
        ],
    ]


def parse(grid: list) -> list:
    grid[0] = header()
    return parse_contract_rows(grid)


def test_carry_in_comes_from_the_first_period_of_the_contract() -> None:
    """ИП ПЕН Н.И.: в июне сальдо −66 000, и книга показывает 66 000 + 99 000."""
    rows = parse(
        contract(
            ("ИЮНЬ 2026", 6, "-66 000", "33 000"),
            ("ИЮЛЬ 2026", 7, "-99 000", "33 000"),
            ("АВГУСТ 2026", 8, "-132 000", "33 000"),
        )
    )
    assert [r.carry_in for r in rows] == [66_000, None, None]
    assert sum(r.total_debt for r in rows) == 165_000


def test_later_periods_never_contribute_their_saldo() -> None:
    """ИП Vector: в июле сальдо −85 000 при долге 85 000 — сложение удвоило бы долг."""
    rows = parse(
        contract(
            ("ИЮНЬ 2026", 6, "", "85 000"),
            ("ИЮЛЬ 2026", 7, "-85 000", "85 000"),
            ("АВГУСТ 2026", 8, "-85 000", "Еще рано"),
        )
    )
    assert [r.carry_in for r in rows] == [None, None, None]
    assert sum(r.total_debt for r in rows) == 170_000


def test_the_old_debt_row_sorts_before_the_months() -> None:
    """У строки «Старые…» месяца нет, и остаток должен достаться именно ей."""
    rows = parse(
        contract(
            ("ИЮНЬ 2026", 6, "-450 000", "0"),
            ("Старые (до Мая/Июня 2026)", 0, "-1 600 000", "450 000"),
        )
    )
    old = next(r for r in rows if r.period_label.startswith("Старые"))
    assert old.carry_in == 1_600_000
    assert sum(r.total_debt for r in rows) == 2_050_000


def test_a_contract_without_an_opening_saldo_carries_nothing() -> None:
    rows = parse(contract(("ИЮНЬ 2026", 6, "", "400 000")))
    assert rows[0].carry_in is None
    assert sum(r.total_debt for r in rows) == 400_000


# ── Договор не вступил в силу ───────────────────────────────────────────────


def test_a_contract_not_in_force_is_not_debt() -> None:
    """«Вид Услуги» = «нет»: договор зафиксирован, но платить по нему не за что.

    Не наша трактовка: ни одна такая строка не попала во вкладки «(для Рук)»,
    по которым живут отделы, — бизнес уже не считает их долгом.
    """
    row = parse_row(2, sheet_row(service_kind="нет", debit_credit="4 060 000"))
    assert row.in_force is False
    assert row.total_debt == 0
    assert row.parked_debt == 4_060_000


def test_an_empty_service_kind_is_also_not_in_force() -> None:
    row = parse_row(2, sheet_row(service_kind="", debit_credit="100 000"))
    assert row.in_force is False
    assert row.total_debt == 0


def test_a_normal_service_kind_is_in_force() -> None:
    for kind in ("Абон.П.", "Разовый", "Аренда", "По квартальный ???"):
        row = parse_row(2, sheet_row(service_kind=kind, debit_credit="85 000"))
        assert row.in_force is True, kind
        assert row.total_debt == 85_000, kind
        assert row.parked_debt == 0, kind


def test_a_parked_contract_does_not_carry_its_opening_saldo_into_debt() -> None:
    rows = parse(
        contract(
            ("Старые (до Мая/Июня 2026)", 0, "-1 000 000", "350 000"),
        )
    )
    for row in rows:
        row.in_force = False
    assert sum(r.total_debt for r in rows) == 0
    assert sum(r.parked_debt for r in rows) == 1_350_000


def test_carry_in_is_counted_once_per_contract_not_per_client() -> None:
    """У клиента два договора — у каждого свой входящий остаток."""
    grid = [header()]
    for contract_no, saldo in (("№1", "-100 000"), ("№2", "-50 000")):
        for month, debt in ((6, "10 000"), (7, "10 000")):
            grid.append(
                sheet_row(
                    client="ИП Тест",
                    contract_no=contract_no,
                    period_label=f"МЕСЯЦ {month}",
                    month=str(month),
                    saldo_start=saldo if month == 6 else "-999 999",
                    debit_credit=debt,
                )
            )
    rows = parse_contract_rows(grid)
    assert sum(r.carry_in or 0 for r in rows) == 150_000
    assert sum(r.total_debt for r in rows) == 190_000
