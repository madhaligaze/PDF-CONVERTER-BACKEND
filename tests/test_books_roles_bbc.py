"""Каталог ролей воспроизводит константы, написанные для BBC руками.

Зачем этот тест — самый важный в разделе
────────────────────────────────────────
`MASTER_COLUMNS` и `JOURNAL_COLUMNS` — знание, накопленное о книгах BBC за
годы: какие колонки как называются, какие написания встречаются, что означает
каждая. Раздел «Книги» обещает то же самое делать автоматически, для любой
компании.

Обещание нужно чем-то подтвердить, иначе каталог ролей — это просто список
слов, который кто-то однажды придумал. Здесь он проверяется об реальность:
автоподбор запускается на **настоящих шапках** боевых книг и обязан разложить
колонки ровно так, как их разложил человек, писавший парсер.

Если этот тест падает после правки каталога — значит правка сделала автоподбор
хуже, чем накопленное знание, и её надо отменить.

Про образцы
───────────
`tests/fixtures/books_live_headers.json` — шапка и десять строк данных из
«Сводки все ЮР лица» и из пилотной копии «Журнала». Данные нужны: тип колонки
определяется по значениям, а без него «Счет» в одной книге и «Счет» в другой
неразличимы.

Десять строк — компромисс. Их достаточно, чтобы отличить даты от денег и
текста, и мало, чтобы образец превратился в выгрузку книги в репозиторий.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.bbc.dataset import MASTER_COLUMNS
from app.bbc.journal import JOURNAL_COLUMNS
from app.books.discover import discover_fields
from app.books.suggest import FieldView, propose

FIXTURE = Path(__file__).parent / "fixtures" / "books_live_headers.json"

#: Колонка парсера BBC → роль каталога. Это и есть утверждение теста.
JOURNAL_EXPECTED = {
    "dds_month": "dds_month",
    "pnl_period": "pnl_period",
    "date": "entry_date",
    "account": "account",
    "inflow": "inflow",
    "outflow": "outflow",
    "counterparty": "counterparty",
    "contract": "contract_no",
    "subcategory": "subcategory",
    "payroll_loan": "payroll_detail",
    "project": "project",
    "category": "category",
    "comment": "comment",
    "product": "product",
    "firm": "firm",
}

MASTER_EXPECTED = {
    "month": "month",
    "period_label": "period_label",
    "firm": "firm",
    "service_kind": "service_kind",
    "employee": "employee",
    "client": "client",
    "contract_amount": "contract_amount",
    "department": "department",
    "subject": "contract_subject",
    "signed_at": "signed_at",
    "contract_no": "contract_no",
    "addendum": "addendum",
    "period_start": "period_start",
    "period_end": "period_end",
    "saldo_start": "saldo_start",
    "invoiced": "invoiced",
    "invoice_no": "invoice_no",
    "invoice_date": "invoice_date",
    "paid_flag": "paid_flag",
    "paid_amount": "paid_amount",
    "pay_date_1": "payment_date_1",
    "pay_amount_1": "payment_amount_1",
    "pay_date_2": "payment_date_2",
    "pay_amount_2": "payment_amount_2",
    "pay_date_3": "payment_date_3",
    "pay_amount_3": "payment_amount_3",
    "avr_flag": "avr_flag",
    "avr_amount": "avr_amount",
    "avr_no": "avr_no",
    "avr_date": "avr_date",
    "avr_accepted": "avr_accepted",
    "esf_sent": "esf_sent",
    "saldo_end": "saldo_end",
    "status": "status",
    "diff_avr_paid": "diff_avr_paid",
    "debit_credit": "debt",
}


def _load(book: str) -> list[list[str]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data[book]


def _bind(grid: list[list[str]]) -> tuple[dict[str, str], list]:
    """Разложить книгу по ролям. Возвращает {роль: заголовок} и отказы."""
    fields, _ = discover_fields(grid)
    views = [
        FieldView(key=f.key, title=f.title, type=f.type, names=tuple(f.names))
        for f in fields
    ]
    proposal = propose(views)
    titles = {f.key: f.title for f in fields}
    bound = {s.role_key: titles[s.field_key] for s in proposal.suggestions}
    return bound, proposal.refusals


def _matches_any_name(title: str, column) -> bool:
    """Совпал ли заголовок с одним из написаний, объявленных в парсере."""
    from app.books.layout import norm

    return any(norm(title) == norm(name) for name in column.names)


@pytest.mark.parametrize(
    "book,columns,expected",
    [
        ("journal", JOURNAL_COLUMNS, JOURNAL_EXPECTED),
        ("master", MASTER_COLUMNS, MASTER_EXPECTED),
    ],
    ids=["Журнал", "Сводка"],
)
def test_autobinding_reproduces_hand_written_columns(book, columns, expected) -> None:
    grid = _load(book)
    bound, _ = _bind(grid)
    by_key = {column.key: column for column in columns}

    problems: list[str] = []
    for column_key, role_key in expected.items():
        column = by_key[column_key]
        title = bound.get(role_key)
        if title is None:
            problems.append(f"роль «{role_key}» не привязана (колонка «{column.title}»)")
        elif not _matches_any_name(title, column):
            problems.append(
                f"роль «{role_key}» привязана к «{title}», "
                f"а парсер ждёт одно из {column.names}"
            )

    assert not problems, "автоподбор разошёлся с константами парсера:\n" + "\n".join(
        f"  · {line}" for line in problems
    )


def test_journal_binds_exactly_the_known_columns() -> None:
    """В журнале не привязывается ничего лишнего.

    Лишняя привязка опаснее пропущенной: пропуск виден на табло как пустое
    гнездо, а лишняя тихо подставляет служебную колонку в расчёт. Ровно так
    склейка «Месяц ОПиу&Контрагент» однажды заняла роль месяца.
    """
    grid = _load("journal")
    bound, refusals = _bind(grid)

    assert set(bound) == set(JOURNAL_EXPECTED.values()), (
        "лишние роли: " + ", ".join(sorted(set(bound) - set(JOURNAL_EXPECTED.values())))
    )
    assert not refusals


def test_same_header_two_roles_resolved_by_type() -> None:
    """«Счет» — расчётный счёт в журнале и признак выставления в сводке.

    Одно написание на две роли. Разводит их только тип значений, и обе книги
    обязаны разобраться сами, без вопроса человеку.
    """
    journal_bound, _ = _bind(_load("journal"))
    master_bound, _ = _bind(_load("master"))

    assert journal_bound.get("account") == "Счет"
    assert master_bound.get("invoiced") == "Счет"
    assert "invoiced" not in journal_bound
    assert "account" not in master_bound


def test_service_columns_stay_unbound() -> None:
    """Служебные колонки книги не попадают в роли.

    Разделители «.», склейки «Техн. 2Наша Фирма», рабочие пометки «По 1C» —
    всё это часть книги, но не данные для расчёта. Привязать их значило бы
    подставить мусор в дашборд.
    """
    grid = _load("master")
    bound, _ = _bind(grid)
    titles = set(bound.values())

    for junk in ("Техн. 2", "По 1C", "По Выписке Банка", "Техн. 2Наша Фирма", "."):
        assert junk not in titles, f"служебная колонка «{junk}» привязана к роли"
