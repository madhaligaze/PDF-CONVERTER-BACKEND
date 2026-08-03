"""Колонка «Проект» на листе «Фактическая стоимость».

Имя обзвона набирают на той раскладке, которая была включена, и «02.08 D2» на
экране неотличимо от «18.06 Д2». Пока буква искалась только в кириллице, у 155
из 176 записанных строк колонка «Проект» молча оставалась пустой — сумма при
этом писалась верная, поэтому по итогам таблицы дефект не был виден.
"""
import pytest

from app.services.autocall_service import (
    _already_in_sheet,
    _autocall_key,
    _extract_project_letter,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("18.06 Б1", "б"),
        ("18.06 Д2", "д"),
        ("02.08 B1", "б"),   # латинская B
        ("02.08 D2", "д"),   # латинская D
        ("02.08 b1", "б"),
        ("02.08 d1", "д"),
        ("17.06", ""),       # буквы нет вовсе
        ("", ""),
        (None, ""),
    ],
)
def test_project_letter_reads_both_layouts(name, expected) -> None:
    assert _extract_project_letter(name) == expected


def test_latin_and_cyrillic_names_give_the_same_letter() -> None:
    """Один проект в двух раскладках должен давать один ключ, иначе строка задвоится."""
    assert _extract_project_letter("02.08 D2") == _extract_project_letter("02.08 Д2")


def test_legacy_blank_project_row_counts_as_already_written() -> None:
    """Строки до починки лежат с пустым «Проект» — их нельзя дописать повторно."""
    seen = {_autocall_key("02.08.2026", "", 1254.96)}

    assert _already_in_sheet(seen, "02.08.2026", "д", 1254.96)


def test_matching_row_is_recognised_after_backfill() -> None:
    seen = {_autocall_key("02.08.2026", "д", 1254.96)}

    assert _already_in_sheet(seen, "02.08.2026", "д", 1254.96)


def test_new_campaign_is_not_mistaken_for_an_existing_row() -> None:
    seen = {_autocall_key("02.08.2026", "д", 1254.96)}

    assert not _already_in_sheet(seen, "02.08.2026", "б", 2105.61)
    assert not _already_in_sheet(seen, "03.08.2026", "д", 1254.96)


def test_blank_project_candidate_does_not_match_a_filled_row() -> None:
    """Послабление работает в одну сторону: пустое в таблице, а не пустое в кандидате."""
    seen = {_autocall_key("02.08.2026", "д", 1254.96)}

    assert not _already_in_sheet(seen, "02.08.2026", "", 1254.96)
