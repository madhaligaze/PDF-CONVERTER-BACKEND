from datetime import datetime
from io import BytesIO
from pathlib import Path

import fitz
from openpyxl import Workbook

from app.schemas.statement import ParserMatch
from app.services.adaptive_statement import detect_adaptive_statement, parse_adaptive_statement
from app.services.document_service import (
    DocumentParseError,
    _detect_generic_bank_statement,
    _selection_order,
    parse_statement_with_diagnostics,
)

_FONT = Path(r"C:\Windows\Fonts\arial.ttf")


def _xlsx(rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def _pdf_table(rows: list[list[str]], preamble: list[str] | None = None) -> bytes:
    document = fitz.open()
    page = document.new_page()
    if _FONT.exists():
        page.insert_font(fontname="arial", fontfile=str(_FONT))
        fontname = "arial"
    else:
        fontname = "helv"
    y = 48
    for line in preamble or []:
        page.insert_text((48, y), line, fontsize=11, fontname=fontname)
        y += 16
    y += 8
    columns = [48, 160, 250, 340, 500]
    for row in rows:
        for x, cell in zip(columns, row):
            page.insert_text((x, y), cell, fontsize=10, fontname=fontname)
        y += 18
    payload = document.tobytes()
    document.close()
    return payload


def _pdf_lines(lines: list[str]) -> bytes:
    document = fitz.open()
    page = document.new_page()
    if _FONT.exists():
        page.insert_font(fontname="arial", fontfile=str(_FONT))
        fontname = "arial"
    else:
        fontname = "helv"
    y = 72
    for line in lines:
        page.insert_text((72, y), line, fontsize=12, fontname=fontname)
        y += 22
    payload = document.tobytes()
    document.close()
    return payload


def test_debit_credit_keeps_balance_out_of_turnover() -> None:
    content = _xlsx(
        [
            ["Выписка Forte Bank"],
            ["Период", "с 01.04.2026 по 02.04.2026"],
            ["Клиент", "ИП Тест"],
            ["Счёт", "KZ123456789012345678"],
            ["Валюта", "KZT"],
            ["Входящий остаток", 1000],
            ["Исходящий остаток", 1300],
            ["Дата операции", "Дебет", "Кредит", "Назначение платежа", "Остаток"],
            ["01.04.2026", 200, None, "Оплата аренды", 800],
            ["02.04.2026", None, 500, "Зачисление от клиента", 1300],
        ]
    )

    assert _detect_generic_bank_statement("forte.xlsx", content) == 0
    statement, matches = parse_statement_with_diagnostics("forte.xlsx", content)

    assert statement.metadata.parser_key == "adaptive_bank_statement"
    assert any(match.key == "adaptive_bank_statement" and match.matched for match in matches)
    assert statement.metadata.currency == "KZT"
    assert statement.metadata.account_holder == "ИП Тест"
    assert statement.metadata.account_number == "KZ123456789012345678"
    assert statement.metadata.period_start == "01.04.2026"
    assert statement.metadata.period_end == "02.04.2026"
    assert statement.metadata.opening_balance == 1000
    assert statement.metadata.closing_balance == 1300
    assert statement.metadata.title == "Выписка Forte Bank"
    assert statement.metadata.totals.expense_total == 200
    assert statement.metadata.totals.income_total == 500
    assert statement.metadata.totals.purchase_total == 200
    assert statement.metadata.totals.topup_total == 500
    note = statement.metadata.reading_note or ""
    assert "Дебет посчитан как расход" in note
    assert "Остаток на конец сходится" in note
    assert "не входит" in note
    assert statement.transactions[0].date == "01.04.2026"
    assert statement.transactions[0].expense == 200
    assert statement.transactions[1].income == 500


def test_unsigned_amount_takes_its_sign_from_the_running_balance() -> None:
    content = _xlsx(
        [
            ["Входящий остаток", 1000],
            ["Исходящий остаток", 1300],
            ["Дата", "Сумма", "Остаток", "Описание"],
            ["01.04.2026", 500, 1500, "Оплата не это"],
            ["02.04.2026", 200, 1300, "Возврат части"],
        ]
    )

    statement = parse_adaptive_statement("balance.xlsx", content)

    assert statement.metadata.totals.income_total == 500
    assert statement.metadata.totals.expense_total == 200
    assert statement.transactions[0].direction == "inflow"
    assert statement.transactions[1].direction == "outflow"
    assert "изменению остатка" in (statement.metadata.reading_note or "")
    assert "сходится" in (statement.metadata.reading_note or "")


def test_debit_and_credit_are_swapped_when_only_the_swap_reconciles() -> None:
    content = _xlsx(
        [
            ["Входящий остаток", 1000],
            ["Исходящий остаток", 1200],
            ["Дата", "Дебет", "Кредит", "Назначение"],
            ["01.04.2026", 200, None, "Возврат"],
        ]
    )

    statement = parse_adaptive_statement("swapped.xlsx", content)

    assert statement.metadata.totals.income_total == 200
    assert statement.metadata.totals.expense_total == 0
    assert "поменяны местами" in (statement.metadata.reading_note or "")
    assert "сходится" in (statement.metadata.reading_note or "")


def test_excel_datetime_and_four_digit_dates() -> None:
    content = _xlsx(
        [
            ["Дата операции", "Дебет", "Кредит", "Назначение платежа"],
            [datetime(2026, 4, 3, 21, 40, 5), 100, None, "Оплата связи"],
            ["04.04.2026 09:15:00", None, 50, "Зачисление"],
        ]
    )

    statement = parse_adaptive_statement("dates.xlsx", content)

    assert [row.date for row in statement.transactions] == ["03.04.2026", "04.04.2026"]
    assert statement.transactions[0].expense == 100
    assert statement.transactions[1].income == 50
    assert statement.metadata.period_start == "03.04.2026"
    assert statement.metadata.period_end == "04.04.2026"


def test_pdf_table_uses_word_positions() -> None:
    content = _pdf_table(
        [
            ["Дата операции", "Дебет", "Кредит", "Назначение платежа", "Остаток"],
            ["01.04.2026", "200,00", "", "Оплата аренды", "800,00"],
            ["02.04.2026", "", "500,00", "Зачисление от клиента", "1 300,00"],
        ],
        preamble=[
            "Выписка Forte Bank",
            "Период: с 01.04.2026 по 02.04.2026",
            "Входящий остаток: 1 000,00 KZT",
            "Исходящий остаток: 1 300,00 KZT",
        ],
    )

    statement = parse_adaptive_statement("forte.pdf", content)

    assert statement.metadata.parser_key == "adaptive_bank_statement"
    assert statement.metadata.totals.expense_total == 200
    assert statement.metadata.totals.income_total == 500
    assert statement.metadata.opening_balance == 1000
    assert statement.metadata.closing_balance == 1300
    assert statement.metadata.currency == "KZT"
    assert "сходится" in (statement.metadata.reading_note or "")
    assert statement.transactions[0].detail.startswith("Оплата аренды")


def test_pdf_lines_without_a_table_still_parse() -> None:
    content = _pdf_lines(
        [
            "Выписка Jusan Bank",
            "01.04.2026   -1 200,50   Оплата Magnum",
            "02.04.2026   +5 000,00   Зачисление зарплаты",
        ]
    )

    statement = parse_adaptive_statement("jusan.pdf", content)

    assert statement.metadata.title == "Выписка Jusan Bank"
    assert statement.metadata.totals.expense_total == 1200.50
    assert statement.metadata.totals.income_total == 5000
    assert statement.transactions[0].operation == "Покупка"
    assert statement.transactions[1].operation == "Пополнение"
    assert "Строки выписки" in (statement.metadata.reading_note or "")


def test_known_bank_stays_ahead_of_a_higher_adaptive_score() -> None:
    order = _selection_order(
        [
            ParserMatch(key="adaptive_bank_statement", label="adaptive", score=0.64),
            ParserMatch(key="kaspi_business_statement", label="kaspi", score=0.35),
            ParserMatch(key="generic_bank_statement", label="generic", score=1),
            ParserMatch(key="ocr_scanned_statement", label="ocr", score=0.45),
        ]
    )

    assert [item.key for item in order] == [
        "kaspi_business_statement",
        "adaptive_bank_statement",
        "generic_bank_statement",
        "ocr_scanned_statement",
    ]


def test_failed_specialist_falls_back_to_adaptive(monkeypatch) -> None:
    content = _xlsx(
        [
            ["Дата операции", "Дебет", "Кредит", "Назначение платежа"],
            ["01.04.2026", 10, None, "Оплата"],
        ]
    )

    def _reject(*_args):
        raise DocumentParseError("нет таблицы kaspi")

    monkeypatch.setattr("app.services.document_service._detect_kaspi_statement", lambda *_args: 1)
    monkeypatch.setattr("app.services.document_service._parse_kaspi_statement", _reject)

    statement, matches = parse_statement_with_diagnostics("plain.xlsx", content)

    assert statement.metadata.parser_key == "adaptive_bank_statement"
    assert statement.transactions[0].expense == 10
    assert matches[0].key == "kaspi_gold_statement"
    assert matches[0].matched is False
    assert any(match.key == "adaptive_bank_statement" and match.matched for match in matches)


def test_images_are_left_to_the_scan_parser() -> None:
    assert detect_adaptive_statement("scan.png", b"\x89PNG\r\n") == 0
