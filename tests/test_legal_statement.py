"""Выписка юрлица любого банка открывается видом «Юр счёт», физлица — нет.

Данные вымышленные; раскладка PDF повторяет выписку ТОО из Halyk, которая
уходила в Excel видом для физлица.
"""

from io import BytesIO
from pathlib import Path

import fitz
from openpyxl import Workbook

from app.api.routes.transforms import _build_preview_response
from app.schemas.statement import TemplateColumnConfig, TransformationTemplate
from app.services.adaptive_statement import parse_adaptive_statement
from app.services.document_service import parse_statement_with_diagnostics
from app.services.halyk_fiz_statement_service import looks_like_halyk_personal
from app.services.legal_statement import (
    counterparty_name,
    has_legal_form,
    holder_kind,
    payment_purpose,
    tax_id_type,
)
from app.services.variant_service import build_variants

_FONT = Path(r"C:\Windows\Fonts\arial.ttf")


def _xlsx(rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def test_bin_and_iin_differ_by_the_fifth_digit() -> None:
    assert tax_id_type("211240002990") == "bin"
    assert tax_id_type("БИН 960941000145") == "bin"
    assert tax_id_type("060603550913") == "iin"
    assert tax_id_type("000305500863") == "iin"
    assert tax_id_type("12345") is None


def test_legal_form_is_read_from_the_holder_name() -> None:
    assert has_legal_form("ТОО Omar Development and Consulting")
    assert has_legal_form('ТОО "BBC HR"')
    assert has_legal_form("ИП Ахатов")
    assert has_legal_form('Товарищество с ограниченной ответственностью "Алатау Пауэр"')
    assert has_legal_form("Omar Development LLP")
    assert not has_legal_form("МУРАТОВ КАЛДЫБАЙ")
    assert not has_legal_form("Гульнара Ахметова")
    assert not has_legal_form("Ипотека")


def test_a_corporate_table_alone_does_not_make_the_owner_a_company() -> None:
    header = ["Дата", "Дебет", "Кредит", "Контрагент", "Назначение платежа"]

    assert holder_kind(holder=None, tax_id=None, header_cells=header) is None
    assert holder_kind(holder="Иванов Иван", tax_id="900101300123", header_cells=header) is None
    assert holder_kind(holder=None, tax_id="211240002990") == "legal"
    assert holder_kind(holder="ИП Тест", tax_id="000305500863") == "legal"
    assert holder_kind(holder="Иванов Иван", tax_id=None, requisites="ФИО: Иванов Иван") == "personal"


def test_counterparty_and_purpose_lose_bank_noise() -> None:
    assert (
        counterparty_name('Товарищество с ограниченной ответственностью "Алатау Пауэр" БИН 260540028486')
        == 'ТОО "Алатау Пауэр"'
    )
    assert counterparty_name('ТОО "К.Эл.М" | ИИН/БИН 250340023119') == 'ТОО "К.Эл.М"'
    assert (
        payment_purpose("Референс 4078280723 Услуги. Оплата по счету № 32 Внешний референс: 163200304")
        == "Услуги. Оплата по счету № 32"
    )


def _legal_excel() -> bytes:
    return _xlsx(
        [
            ["ВЫПИСКА ПО СЧЕТУ"],
            ["Банк", "АО «Народный Банк Казахстана»"],
            ["ИИН/БИН", "211240002990"],
            ["Клиент", "ТОО Альфа Консалтинг"],
            ["Счет", "KZ75601A861076186521 (KZT)"],
            ["Входящий остаток:", "1 000.00"],
            ["Дата", "Номер документа", "Дебет", "Кредит", "Контрагент", "Детали платежа"],
            ["27.08.2026", "372", None, "25,000.00", "Товарищество с", "Референс 4088365467 Услуги"],
            [None, None, None, None, "ограниченной ответственностью", "по аренде, счет на оплату № 34"],
            [None, None, None, None, '"Алатау Пауэр" БИН 260540028486', "Внешний референс: 7726827153129576"],
            ["18.08.2026", "004577702339", "5,000.00", None, "Народный Банк", "Комиссия за операцию"],
            [None, None, None, None, "БИН 960941000145", '"Абонентская плата"'],
            [None, None, "Дебет", "Кредит"],
            ["Обороты:", None, "5 000.00", "25 000.00"],
            [None, None, None, None, None, "За период: 19-07-2026 - 27-08-2026"],
            ["Исходящий остаток:", "21 000.00"],
        ]
    )


def test_legal_excel_opens_in_the_legal_view() -> None:
    statement, _ = parse_statement_with_diagnostics("halyk-too.xlsx", _legal_excel())

    assert statement.metadata.parser_key == "adaptive_bank_statement"
    assert statement.metadata.holder_kind == "legal"
    assert statement.metadata.account_holder == "ТОО Альфа Консалтинг"
    assert statement.metadata.totals.topup_total == 25000
    assert statement.metadata.totals.purchase_total == 5000

    variants = build_variants(statement)
    legal = variants[0]
    assert legal.key == "business_compact_classic"
    assert legal.name == "Юр счёт"
    assert [column.key for column in legal.columns] == ["date", "income", "expense", "detail", "comment"]
    # Виды физлица остаются за переключателем — на случай ошибки в определении.
    assert any(variant.key == "classic_financier" for variant in variants[1:])

    first, second = legal.rows
    assert first["detail"] == 'ТОО "Алатау Пауэр"'
    assert first["comment"] == "Услуги по аренде, счет на оплату № 34"
    assert first["income"] == 25000
    assert second["detail"] == "Народный Банк"
    # Подвал «Дебет | Кредит / Обороты / За период» к операции не приклеивается.
    assert second["comment"] == 'Комиссия за операцию "Абонентская плата"'

    # «Финансы» берут контрагента из raw_counterparty — там он с БИН, как у банка.
    assert "260540028486" in (statement.transactions[0].raw_counterparty or "")
    assert statement.transactions[1].operation == "Комиссия банка"


def test_personal_statement_with_the_same_table_keeps_personal_views() -> None:
    content = _xlsx(
        [
            ["Выписка по счету"],
            ["ФИО:", "Иванов Иван Иванович"],
            ["ИИН:", "900101300123"],
            ["Входящий остаток:", "1 000.00"],
            ["Дата", "Дебет", "Кредит", "Контрагент", "Назначение платежа"],
            ["01.04.2026", "200.00", None, "ТОО Магазин", "Оплата товара"],
            ["Исходящий остаток:", "800.00"],
        ]
    )

    statement = parse_adaptive_statement("personal.xlsx", content)

    assert statement.metadata.holder_kind == "personal"
    variants = build_variants(statement)
    assert variants[0].key == "classic_financier"
    assert not any(variant.key == "business_compact_classic" for variant in variants)


def _ruled_pdf() -> bytes:
    """Таблица с линейками, подписи шапки по центру колонок — как у Halyk."""
    document = fitz.open()
    page = document.new_page()
    if _FONT.exists():
        page.insert_font(fontname="arial", fontfile=str(_FONT))
        font = "arial"
    else:
        font = "helv"

    def text(x: float, y: float, value: str, size: float = 8) -> None:
        page.insert_text((x, y), value, fontsize=size, fontname=font)

    text(30, 60, "ВЫПИСКА ПО СЧЕТУ", 11)
    text(30, 90, "ИИН/БИН")
    text(180, 90, "211240002990")
    text(30, 104, "Клиент")
    text(180, 104, "ТОО Omar Development and Consulting")
    text(30, 118, "Счет")
    text(180, 118, "KZ75601A861076186521 (KZT)")
    text(30, 140, "Входящий остаток:")
    text(180, 140, "1,000.00")

    xs = [20, 80, 151, 226, 301, 405, 525, 575]
    top, bottom = 160, 260
    for x in xs:
        page.draw_line((x, top), (x, bottom))
    for y in (top, 185, bottom):
        page.draw_line((xs[0], y), (xs[-1], y))

    # Шапка: подписи по центру своих ячеек.
    text(40, 176, "Дата")
    text(83, 176, "Номер документа")
    text(177, 176, "Дебет")
    text(250, 176, "Кредит")
    text(333, 176, "Контрагент")
    text(430, 176, "Детали платежа")  # правый край ≈ 491
    text(540, 176, "НДС")

    text(24, 198, "27.08.2026")
    text(100, 198, "372")
    text(260, 198, "25,000.00")
    text(304, 198, "Товарищество с")
    text(408, 198, "Референс 4088365467 Услуги")
    text(304, 210, "ограниченной")
    text(408, 210, "по аренде, счёт №")
    # Хвост строки правее середины между «Детали платежа» и «НДС» (≈ 515):
    # по подписям шапки он уходил в колонку НДС, по линейкам — нет.
    text(514, 210, "34")
    text(304, 222, 'ответственностью "Алатау"')
    text(304, 234, "БИН 260540028486")

    text(30, 290, "Исходящий остаток:")
    text(180, 290, "26,000.00")
    payload = document.tobytes()
    document.close()
    return payload


def test_pdf_columns_follow_the_table_rulings() -> None:
    statement = parse_adaptive_statement("halyk-too.pdf", _ruled_pdf())

    assert statement.metadata.account_holder == "ТОО Omar Development and Consulting"
    assert statement.metadata.holder_kind == "legal"
    assert "сходится" in (statement.metadata.reading_note or "")
    row = build_variants(statement)[0].rows[0]
    assert row["detail"] == 'ТОО "Алатау"'
    assert row["comment"] == "Услуги по аренде, счёт № 34"


def test_halyk_personal_template_needs_a_full_name() -> None:
    assert looks_like_halyk_personal("АО \"Народный Банк Казахстана\"\nВыписка по счету\nФИО: МУРАТОВ К.")
    assert not looks_like_halyk_personal(
        "ВЫПИСКА ПО СЧЕТУ\nАО «Народный Банк Казахстана»\nИИН/БИН\n211240002990\nВыписка по счету"
    )


def test_saved_personal_template_does_not_hide_the_legal_view(monkeypatch) -> None:
    statement = parse_adaptive_statement("halyk-too.xlsx", _legal_excel())
    template = TransformationTemplate(
        template_id="t-personal",
        parser_key="adaptive_bank_statement",
        name="Мой вид физлица",
        base_variant_key="classic_financier",
        columns=[TemplateColumnConfig(key="date", label="Дата")],
        is_default=True,
    )
    monkeypatch.setattr("app.api.routes.transforms.list_templates", lambda *_args, **_kwargs: [template])

    response = _build_preview_response("session", statement, None, [])

    assert response.default_variant_key == "business_compact_classic"
