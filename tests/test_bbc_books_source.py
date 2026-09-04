"""Дашборд из внутренней книги — второй источник тех же цифр.

Переключатель существует, чтобы источники можно было сверить. Это работает,
только если разбор у них общий: два пути, считающие по-разному, дали бы два
разных ответа на один вопрос, и оба выглядели бы одинаково уверенно.
"""
from __future__ import annotations

from datetime import date

from app.bbc.books_source import REQUIRED_ROLES, ROLE_BY_COLUMN
from app.bbc.dataset import MASTER_COLUMNS, build_contract_row, parse_contract_row
from app.bbc.layout import resolve_layout
from app.books.roles import role


# ── Таблица соответствия колонок и ролей ─────────────────────────────────────


def test_every_master_column_maps_to_a_role() -> None:
    """У каждой колонки парсера есть роль в каталоге.

    Пропущенная колонка не ломает импорт и не роняет дашборд — она просто
    молча не доезжает: величина есть в книге, но её нет в цифрах. Именно так и
    выглядят самые дорогие ошибки в этом продукте.
    """
    missing = [column.key for column in MASTER_COLUMNS if column.key not in ROLE_BY_COLUMN]
    assert not missing, f"колонки без роли: {missing}"


def test_every_mapped_role_exists_in_the_catalog() -> None:
    """И обратно: роль, которой нет в каталоге, привязать невозможно."""
    unknown = [
        f"{column} → {role_key}"
        for column, role_key in ROLE_BY_COLUMN.items()
        if role(role_key) is None
    ]
    assert not unknown, f"ролей нет в каталоге: {unknown}"


def test_required_roles_are_the_ones_receivables_cannot_work_without() -> None:
    """Без заказчика, суммы договора и сальдо дебиторка не считается вовсе.

    Проверка защищает от соблазна ослабить требование: вкладка, где этих ролей
    нет, — не мастер-книга, и подставлять её значило бы показать дашборд,
    посчитанный по журналу платежей.
    """
    for role_key in REQUIRED_ROLES:
        assert role(role_key) is not None
    assert set(REQUIRED_ROLES) <= set(ROLE_BY_COLUMN.values())


# ── Разбор общий для обоих источников ────────────────────────────────────────


HEADER = [column.names[0] for column in MASTER_COLUMNS]
VALUES = {
    "Мес": "6",
    "Техн. 1": "ИЮНЬ 2026",
    "Наша Фирма": "BBC",
    "Вид Услуги": "Абон.П.",
    "Наш Сотрудник": "Иванов",
    "Заказчик (Название Фирмы)": "ТОО «Ромашка»",
    "Число Выставления Счета": "25",
    "Сумма Договора": "1 200 000,00",
    "Отдел": "ОБО",
    "Предмет договра": "Бухгалтерские услуги",
    "Дата заключения Договора": "01.01.2026",
    "№ Договора": "ОБО/42",
    "Доп Соглашения": "",
    "Период с (нач.)": "01.06.2026",
    "Период по (зав.)": "30.06.2026",
    "Сальдо Начало": "0",
    "Счет": "TRUE",
    "№ Счета": "77",
    "Дата выстав. Счета": "05.06.2026",
    "Факт Оплата": "TRUE",
    "Сумма Факт Поступ.": "400 000,00",
    "Дата (часть 1)": "10.06.2026",
    "Сумма (часть 1)": "400 000,00",
    "Дата (часть 2)": "",
    "Сумма (часть 2)": "",
    "Дата (часть 3)": "",
    "Сумма (часть 3)": "",
    "АВР (наша)": "TRUE",
    "Сумма (наша)": "1 200 000,00",
    "№ АВР (наша)": "А-9",
    "Дата (наша)": "30.06.2026",
    "АВР (клиент принял)": "FALSE",
    "ЭСФ (отпр.)": "TRUE",
    "Сальдо Конец": "800 000,00",
    "Статус (Продл./Приостн./Разв./На исп.)": "Действ.",
    "Разница (АВР-Факт)": "800 000,00",
    "Дебет / Кредит (в т.ч без АВР)": "800 000,00",
}


def test_same_values_give_the_same_row_from_either_side() -> None:
    """Лист и книга дают одинаковую строку — иначе сверять было бы нечего.

    Это главная проверка переключателя. Если разбор разойдётся, дашборд начнёт
    отвечать по-разному на один и тот же вопрос, и понять, какой ответ верный,
    будет не по чему.
    """
    layout = resolve_layout("тест", MASTER_COLUMNS, HEADER)
    row = [VALUES[name] for name in HEADER]
    from_sheet = parse_contract_row(2, row, layout)

    # Сторона книги: значения лежат по ключу поля, поле привязано к роли.
    by_role = {ROLE_BY_COLUMN[column.key]: VALUES[column.names[0]] for column in MASTER_COLUMNS}
    from_book = build_contract_row(
        2,
        cell=lambda key: by_role.get(ROLE_BY_COLUMN.get(key, ""), ""),
        has=lambda key: ROLE_BY_COLUMN.get(key, "") in by_role,
    )

    assert from_sheet == from_book


def test_the_row_is_actually_parsed_not_just_equal_by_emptiness() -> None:
    """Обе стороны должны разобрать значения, а не совпасть на пустоте.

    Без этой проверки предыдущая осталась бы зелёной, даже если бы оба пути
    возвращали одинаково пустые строки.
    """
    layout = resolve_layout("тест", MASTER_COLUMNS, HEADER)
    row = parse_contract_row(2, [VALUES[name] for name in HEADER], layout)

    assert row.client == "ТОО «Ромашка»"
    assert row.contract_amount == 1_200_000.0
    assert row.paid_amount == 400_000.0
    assert row.debt == 800_000.0
    assert row.period_start == date(2026, 6, 1)
    assert row.avr_signed is True
    assert row.avr_accepted is False


def test_a_column_absent_from_the_book_reads_as_unknown_not_false() -> None:
    """Нет колонки — «книга про это не знает», а не «нет».

    Схлопнуть одно в другое значило бы отчитаться, что клиент не принял ни
    одного АВР, на книге, которая этого просто не отслеживает.
    """
    row = build_contract_row(2, cell=lambda key: "", has=lambda key: False)
    assert row.avr_accepted is None
    assert row.esf_sent is None
