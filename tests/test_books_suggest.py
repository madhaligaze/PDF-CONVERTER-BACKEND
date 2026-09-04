"""Предложение привязок: где соглашаемся, а где отказываемся выбирать.

Проверки на чистых функциях. Живые книги проверяются отдельно —
`test_books_roles_bbc.py`.
"""
from __future__ import annotations

from app.books.roles import Role, section_status
from app.books.suggest import FieldView, propose, types_compatible


def field(key: str, title: str, kind: str = "text") -> FieldView:
    return FieldView(key=key, title=title, type=kind)


ROLE_AMOUNT = Role("contract_amount", "Сумма договора", "money", "", ("Сумма Договора",))
ROLE_PAID = Role("paid_amount", "Сумма оплаты", "money", "", ("Сумма Факт Поступ.",))
ROLE_DATE = Role("entry_date", "Дата операции", "date", "", ("Дата",))
ROLE_CLIENT = Role("client", "Заказчик", "text", "", ("Заказчик",))


# ── Совместимость типов ──────────────────────────────────────────────────────


def test_money_role_refuses_a_text_column() -> None:
    """Денежная роль не берёт текстовую колонку.

    Цена ошибки несимметрична: текст в роли денег даёт пустоту вместо сумм, и
    раздел считает по неполным данным, не сообщая об этом.
    """
    assert not types_compatible("money", "text")
    assert types_compatible("money", "number")


def test_text_role_takes_a_number_but_not_a_date() -> None:
    """Текстовая роль терпима к числам и нетерпима к датам.

    Число, показанное текстом, ничего не портит. А колонка дат, ушедшая в
    текстовую роль, перестаёт быть датой — и по ней больше не посчитать сроки.
    """
    assert types_compatible("text", "number")
    assert not types_compatible("text", "date")
    assert not types_compatible("text", "bool")


def test_empty_column_fits_anything() -> None:
    """О пустой колонке утверждать нечего — она годится подо всё.

    Запретить её значило бы требовать сначала наполнить книгу, а потом
    размечать.
    """
    assert types_compatible("money", "unknown")
    assert types_compatible("date", "unknown")


# ── Ступени и приоритет ──────────────────────────────────────────────────────


def test_exact_match_beats_similar_one() -> None:
    """Точное совпадение забирает роль, похожее остаётся ни с чем.

    Так «Дата» и «Дата ДДС (дубль)» не спорят за роль даты операции.
    """
    fields = [field("a", "Дата", "date"), field("b", "Дата ДДС", "date")]
    proposal = propose(fields, roles=[ROLE_DATE])

    assert [(s.field_key, s.confidence) for s in proposal.suggestions] == [("a", "exact")]


def test_long_glued_header_does_not_match_loosely() -> None:
    """Служебная склейка не считается похожей на короткую роль.

    «Месяц ОПиу&Контрагент» начинается со слова «Месяц» — формально префикс, по
    смыслу совпадение первых пяти букв из двадцати. Ровно на этом автоподбор
    один раз отдал служебной колонке роль месяца.
    """
    role_month = Role("month", "Месяц", "text", "", ("Месяц", "Мес"))
    proposal = propose([field("a", "Месяц ОПиу&Контрагент")], roles=[role_month])

    assert proposal.suggestions == []


def test_short_abbreviation_still_matches() -> None:
    """Но обычное сокращение по-прежнему узнаётся: «Коммент» ↔ «Комментарии»."""
    role = Role("comment", "Комментарий", "text", "", ("Комментарии",))
    proposal = propose([field("a", "Коммент")], roles=[role])

    assert [s.role_key for s in proposal.suggestions] == ["comment"]


# ── Отказы ───────────────────────────────────────────────────────────────────


def test_two_columns_on_one_role_is_a_question_not_a_coin_toss() -> None:
    """Две одинаково подходящие колонки — вопрос человеку.

    Выбор наугад дал бы уверенные неверные цифры, а это хуже, чем отсутствие
    цифр: неверные никто не перепроверяет.
    """
    fields = [
        field("a", "Сумма Договора", "money"),
        field("b", "Сумма Договора", "money"),
    ]
    proposal = propose(fields, roles=[ROLE_AMOUNT])

    assert proposal.suggestions == []
    assert len(proposal.refusals) == 1
    refusal = proposal.refusals[0]
    assert refusal.kind == "ambiguous_role"
    assert refusal.field_keys == ("a", "b")


def test_one_column_on_two_roles_is_also_a_question() -> None:
    fields = [field("a", "Сумма", "money")]
    roles = [
        Role("contract_amount", "Сумма договора", "money", "", ("Сумма",)),
        Role("paid_amount", "Сумма оплаты", "money", "", ("Сумма",)),
    ]
    proposal = propose(fields, roles=roles)

    assert proposal.suggestions == []
    assert proposal.refusals[0].kind == "ambiguous_field"


# ── Выученные синонимы ───────────────────────────────────────────────────────


def test_confirmed_spelling_works_like_a_shipped_one() -> None:
    """Написание, подтверждённое человеком, работает как поставляемое.

    Из этого каталог и умнеет: следующая компания, назвавшая колонку так же,
    получит привязку сразу.
    """
    fields = [field("a", "Оплата поступила", "money")]

    without = propose(fields, roles=[ROLE_PAID])
    assert without.suggestions == []

    with_learned = propose(
        fields, roles=[ROLE_PAID], learned={"paid_amount": ["Оплата поступила"]}
    )
    assert [s.role_key for s in with_learned.suggestions] == ["paid_amount"]
    assert with_learned.suggestions[0].confidence == "exact"


# ── Догадка по данным ────────────────────────────────────────────────────────


def test_type_alone_never_binds_anything() -> None:
    """Подходящий тип — не основание для привязки.

    Такое правило было написано и убрано после проверки на живых книгах: в
    мастер-книге свободными остались роль «дата операции» и вторая колонка
    «Счет», оказавшаяся датой. Кандидат вышел ровно один, и они связались — но
    колонка осталась свободной потому, что проиграла борьбу за свою настоящую
    роль по типу, а не потому что была ничьей.

    Тип колонки виден человеку на табло; тянет её он сам.
    """
    fields = [field("col_7", "", "date"), field("schet", "Счет", "date")]
    proposal = propose(fields, roles=[ROLE_DATE])

    assert proposal.suggestions == []
    assert proposal.refusals == []


# ── Следствия для разделов ───────────────────────────────────────────────────


def test_section_reports_what_is_missing_not_a_zero() -> None:
    """Раздел без роли говорит, чего не хватает.

    Ноль и «не знаю» — разные вещи. Ноль складывается в сумму и выглядит как
    факт; «не хватает даты операции» ведёт человека на табло привязок.
    """
    status = {item.key: item for item in section_status({"entry_date", "inflow"})}

    journal = status["journal"]
    assert not journal.computes
    assert journal.missing_required == ("outflow",)
    assert journal.missing_titles == ("Расход",)


def test_section_computes_when_everything_required_is_bound() -> None:
    bound = {"client", "contract_amount", "paid_amount", "saldo_end"}
    status = {item.key: item for item in section_status(bound)}

    assert status["receivables"].computes
    assert status["receivables"].missing_required == ()
    # Необязательные роли отсутствуют, но разделу они не мешают считать.
    assert status["receivables"].missing_optional
