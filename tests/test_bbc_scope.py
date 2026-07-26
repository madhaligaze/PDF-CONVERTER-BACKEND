"""Access isolation for the BBC dashboard.

The row mix mirrors the real «Сводка все ЮР лица» distribution, so the expected
counts here are the production ones: ОБО 239 own rows, НО 117, ЮО 95, HR 40,
ФО 1, plus 3 rows shared by four departments and 29 with no department at all.
"""
from __future__ import annotations

import pytest

from app.bbc.scope import (
    LINK_BLOCKS,
    Scope,
    filter_rows,
    parse_departments,
    row_visible,
)

# (department cell, number of rows) — the real distribution of the master sheet.
_DISTRIBUTION = [
    ("ОБО", 239),
    ("НО", 117),
    ("ЮО", 95),
    ("HR", 40),
    ("ФО", 1),
    ("ОБО, НО,\n ЮО, HR", 3),
    ("", 29),
]

TOTAL_ROWS = sum(count for _, count in _DISTRIBUTION)


@pytest.fixture
def rows() -> list[dict]:
    out: list[dict] = []
    for cell, count in _DISTRIBUTION:
        for index in range(count):
            out.append({"id": f"{cell or 'none'}-{index}", "departments": parse_departments(cell)})
    return out


# ── Parsing ──────────────────────────────────────────────────────────────────────


def test_parses_single_department() -> None:
    assert parse_departments("НО") == ("НО",)


def test_parses_multi_department_cell_with_newline() -> None:
    # The sheet really contains this value, newline included.
    assert parse_departments("ОБО, НО,\n ЮО, HR") == ("ОБО", "НО", "ЮО", "HR")


def test_blank_department_yields_empty_tuple() -> None:
    assert parse_departments("") == ()
    assert parse_departments(None) == ()
    assert parse_departments("   ") == ()


def test_unknown_department_is_dropped_not_guessed() -> None:
    assert parse_departments("Маркетинг") == ()


def test_parsing_is_case_and_space_insensitive() -> None:
    assert parse_departments(" обо ") == ("ОБО",)
    assert parse_departments("hr") == ("HR",)


def test_duplicates_collapse() -> None:
    assert parse_departments("НО, НО") == ("НО",)


# ── Visibility ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("department", "expected"),
    [("ОБО", 242), ("НО", 120), ("ЮО", 98), ("HR", 43), ("ФО", 1)],
)
def test_department_sees_own_rows_plus_shared(rows, department, expected) -> None:
    """Own rows + the 3 shared ones (ФО is not listed in the shared cell)."""
    assert len(filter_rows(rows, Scope.for_departments([department]))) == expected


def test_admin_sees_everything(rows) -> None:
    assert len(filter_rows(rows, Scope.admin())) == TOTAL_ROWS == 524


def test_denied_scope_sees_nothing(rows) -> None:
    """Default deny: an absent credential must not fall through to full access."""
    assert filter_rows(rows, Scope.denied()) == []


def test_empty_scope_is_denied_not_wildcard(rows) -> None:
    assert filter_rows(rows, Scope()) == []


def test_no_foreign_rows_reach_a_department(rows) -> None:
    for row in filter_rows(rows, Scope.for_departments(["НО"])):
        assert "НО" in row["departments"]


def test_unassigned_rows_are_admin_only(rows) -> None:
    unassigned = [row for row in rows if not row["departments"]]
    assert len(unassigned) == 29
    for department in ("ОБО", "НО", "ЮО", "HR", "ФО"):
        visible = filter_rows(rows, Scope.for_departments([department]))
        assert not any(row in visible for row in unassigned)


def test_scoped_totals_differ_from_company_totals(rows) -> None:
    """Aggregates must be computed over the filtered set, never the raw one."""
    scoped = filter_rows(rows, Scope.for_departments(["НО"]))
    assert len(scoped) < len(rows)


def test_row_visible_accepts_raw_cell_string() -> None:
    scope = Scope.for_departments(["ЮО"])
    assert row_visible(parse_departments("ОБО, НО,\n ЮО, HR"), scope)
    assert not row_visible(parse_departments("ОБО"), scope)


def test_filter_rows_parses_a_raw_string_field() -> None:
    raw_rows = [{"departments": "ОБО, НО,\n ЮО, HR"}, {"departments": "ОБО"}]
    assert len(filter_rows(raw_rows, Scope.for_departments(["HR"]))) == 1


# ── Serialisation round-trip ─────────────────────────────────────────────────────


def test_scope_round_trips_through_storage() -> None:
    original = Scope.for_departments(["НО"], LINK_BLOCKS, label="НО")
    assert Scope.from_dict(original.to_dict()) == original


def test_admin_scope_round_trips() -> None:
    assert Scope.from_dict(Scope.admin().to_dict()).is_admin


def test_malformed_stored_scope_degrades_to_denied() -> None:
    for payload in (None, {}, {"departments": "НО"}, {"departments": None}, "nonsense"):
        assert Scope.from_dict(payload).sees_nothing  # type: ignore[arg-type]


def test_unknown_department_in_stored_scope_is_dropped(rows) -> None:
    """A tampered scope naming a bogus department must not widen access."""
    scope = Scope.from_dict({"departments": ["Маркетинг"], "blocks": list(LINK_BLOCKS)})
    assert filter_rows(rows, scope) == []


# ── Blocks ───────────────────────────────────────────────────────────────────────


def test_link_scope_reaches_only_its_three_blocks() -> None:
    scope = Scope.for_departments(["НО"])
    assert scope.allows_block("receivables")
    assert scope.allows_block("analytics")
    assert scope.allows_block("calendar")
    assert not scope.allows_block("reports")
    assert not scope.allows_block("journal")
    assert not scope.allows_block("sales")


def test_admin_reaches_every_block() -> None:
    for block in ("receivables", "reports", "journal", "sales", "roadmap"):
        assert Scope.admin().allows_block(block)


def test_denied_scope_reaches_no_block() -> None:
    assert not Scope.denied().allows_block("receivables")
