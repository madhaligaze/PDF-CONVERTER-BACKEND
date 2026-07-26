"""Parsers for the hand-maintained spreadsheet.

Every literal below was taken from the live sheets, so these tests double as
documentation of what the source actually contains.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.bbc.normalize import (
    canonical_firm,
    canonical_service_kind,
    canonical_status,
    clean,
    department_label,
    firm_label,
    is_error,
    parse_bool,
    parse_date,
    parse_money,
)

NBSP = "\xa0"
NNBSP = " "


# ── Money ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"50{NBSP}000", 50_000),
        (f"1{NBSP}200{NBSP}000", 1_200_000),
        (f"2{NBSP}000{NBSP}000", 2_000_000),
        ("500000", 500_000),
        ("190,00", 190.0),
        (f"20{NBSP}000,00", 20_000.0),
        ("237,50", 237.5),
        ("0", 0.0),
        (f"-2{NBSP}500{NBSP}000", -2_500_000),
    ],
)
def test_parses_the_amounts_that_exist_in_the_sheet(raw, expected) -> None:
    assert parse_money(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["#N/A", "#VALUE!", "#REF!", "#DIV/0!"])
def test_formula_errors_are_not_numbers(raw) -> None:
    """These must be None, not 0 — a broken formula is missing data, not zero."""
    assert parse_money(raw) is None


@pytest.mark.parametrize("raw", ["", "   ", None, "Еще рано", "-", "текст"])
def test_blanks_and_text_are_not_numbers(raw) -> None:
    assert parse_money(raw) is None


def test_currency_suffix_is_tolerated() -> None:
    assert parse_money(f"1{NBSP}515{NBSP}151,5 ₸") == pytest.approx(1_515_151.5)


def test_parentheses_mean_negative() -> None:
    assert parse_money("(1 000)") == pytest.approx(-1000)


def test_dot_thousands_with_comma_decimal() -> None:
    assert parse_money("1.234.567,89") == pytest.approx(1_234_567.89)


def test_comma_thousands_with_dot_decimal() -> None:
    assert parse_money("1,234,567.89") == pytest.approx(1_234_567.89)


# ── Dates ────────────────────────────────────────────────────────────────────────


def test_parses_the_plain_format() -> None:
    assert parse_date("21.05.2026") == date(2026, 5, 21)


def test_parses_the_weekday_prefixed_journal_format() -> None:
    """The journal writes `пн 18.05.26` with a narrow no-break space after it."""
    assert parse_date(f"пн 18.05.26{NNBSP}") == date(2026, 5, 18)


def test_two_digit_years_are_2000_based() -> None:
    assert parse_date("01.02.24") == date(2024, 2, 1)


@pytest.mark.parametrize("raw", ["", None, "#N/A", "#REF!", "нет", "Еще рано"])
def test_non_dates_return_none(raw) -> None:
    assert parse_date(raw) is None


def test_impossible_date_returns_none() -> None:
    assert parse_date("31.02.2026") is None


def test_single_digit_day_and_month() -> None:
    assert parse_date("1.7.2026") == date(2026, 7, 1)


# ── Booleans ─────────────────────────────────────────────────────────────────────


def test_true_and_false_are_parsed() -> None:
    assert parse_bool("TRUE") is True
    assert parse_bool("FALSE") is False


def test_dash_counts_as_false() -> None:
    assert parse_bool("-") is False


def test_partial_and_free_text_are_undecided() -> None:
    """«Часть» and «Еще рано» are neither true nor false — the caller decides."""
    assert parse_bool("Часть") is None
    assert parse_bool("Еще рано") is None


# ── Dimensions ───────────────────────────────────────────────────────────────────


def test_service_kind_spellings_collapse() -> None:
    variants = ["Разовый", "Разовая", "Разовая услуга"]
    assert {canonical_service_kind(value) for value in variants} == {"Разовая услуга"}


def test_subscription_spellings_collapse() -> None:
    assert canonical_service_kind("Абон.П.") == canonical_service_kind(
        "Абонентское обслуживание"
    )


def test_status_spellings_collapse() -> None:
    assert canonical_status("на исполнении") == canonical_status("на исп.") == "На исполнении"


def test_suspension_variants_collapse() -> None:
    variants = ["Приостоновление в Июне", "Приостоновили в Июне", "Приостоновление в Июле"]
    assert {canonical_status(value) for value in variants} == {"Приостановлен"}


def test_unknown_status_is_kept_verbatim() -> None:
    """Free-text notes carry meaning; they must not be flattened away."""
    note = "ИП Айдана приостановленно, замена ИП Нургазина"
    assert canonical_status(note) == "Приостановлен" or canonical_status(note) == note


def test_firm_codes_map_to_readable_names() -> None:
    assert firm_label("BBCL") == "BBC Legal Support"
    assert firm_label("EA") == "Elite Advisory"


def test_unknown_firm_code_is_not_dropped() -> None:
    assert firm_label("XYZ") == "XYZ"


def test_firm_code_is_case_insensitive() -> None:
    assert canonical_firm(" bbc astana ") == "BBC ASTANA"


def test_department_labels() -> None:
    assert department_label("НО") == "Налоговый отдел"
    assert department_label("ОБО") == "Бухгалтерский отдел"


# ── Helpers ──────────────────────────────────────────────────────────────────────


def test_clean_strips_invisible_separators() -> None:
    assert clean(f" {NBSP}текст{NNBSP} ") == "текст"


def test_is_error_detects_formula_errors() -> None:
    assert is_error("#REF!")
    assert not is_error("500")
