"""Translate Web-Excel (Univer) column edits into reusable {field} rules.

The Web-Excel page lets finance users shape a statement like a spreadsheet. To
re-apply that "образ" to FUTURE statements (different row counts), every computed
column must become a per-column rule in our own formula model
(formula_engine syntax: "{amount} * 0.12", IF(CONTAINS(...))).

Two strategies, in order:
1. A1 formula translation — when the editor provides a uniform Excel formula for a
   column referencing source columns (the common direct cases: =D2*0.12, =B2-C2).
2. Value-based inference via diff_analyzer (the "Понять расчёт" engine) — infer the
   rule from the resulting column VALUES vs the source statement rows. This covers
   text categorisation (FACEBK → "Реклама Facebook") and numeric ratios robustly,
   and is the fallback whenever a formula is missing or unsupported.
"""
from __future__ import annotations

import re
from typing import Any

from app.services import diff_analyzer

# Output column keys / Univer source columns that map onto formula_engine fields.
_FIELD_ALIASES = {
    "income", "expense", "amount", "net", "detail", "operation",
    "comment", "date", "direction", "currency_op", "note",
}


def _column_letter_to_index(letters: str) -> int:
    """A→0, B→1, … AA→26."""
    idx = 0
    for ch in letters.upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _translate_a1(formula: str, columns: list[dict]) -> str | None:
    """Translate a simple A1 arithmetic formula to {field} syntax, else None.

    Handles cell refs that map onto known source fields combined with numbers and
    + - * / operators (e.g. "=D2*0.12", "=B2-C2"). Anything richer returns None so
    the caller falls back to value-based inference.
    """
    expr = formula.strip()
    if expr.startswith("="):
        expr = expr[1:]
    if not expr:
        return None

    def repl(match: re.Match) -> str:
        col_letters = match.group(1)
        index = _column_letter_to_index(col_letters)
        if 0 <= index < len(columns):
            key = columns[index].get("key", "")
            if key in _FIELD_ALIASES:
                return f"{{{key}}}"
        raise ValueError("unmappable cell reference")

    try:
        translated = re.sub(r"\$?([A-Za-z]+)\$?\d+", repl, expr)
    except ValueError:
        return None

    # Only accept a safe arithmetic shape after translation.
    if re.fullmatch(r"[\s\d.+\-*/()%{}\w]+", translated) and "{" in translated:
        return translated.strip()
    return None


def infer_column_rules(
    columns: list[dict],
    rows: list[dict],
    source_rows: list[dict],
    a1_formulas: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return {column_key: formula} for every column that looks computed.

    columns:     ordered output columns ({key, label, kind}).
    rows:        the edited/computed rows from the Web-Excel grid.
    source_rows: the freshly parsed base-variant rows (formula context source).
    a1_formulas: optional {column_key: excel_formula} from Univer.
    """
    rules: dict[str, str] = {}
    a1_formulas = a1_formulas or {}

    for col in columns:
        key = col.get("key")
        if not key:
            continue

        # 1. Direct A1 formula translation (common arithmetic cases).
        a1 = a1_formulas.get(key)
        if a1:
            translated = _translate_a1(a1, columns)
            if translated:
                rules[key] = translated
                continue

        # 2. Value-based inference (text categorisation + numeric patterns).
        edit_vals = [r.get(key) for r in rows]
        findings = diff_analyzer._analyze_new_column(key, edit_vals, source_rows)
        detected = next(
            (f for f in findings if f.type == "formula_detected" and f.detected_formula),
            None,
        )
        if detected:
            rules[key] = detected.detected_formula

    return rules
