"""
Safe formula evaluation engine for column calculations.

Formulas use {field_name} syntax for variable substitution.
Example: "{amount} * 0.12"  →  evaluates with row context.
"""
from __future__ import annotations

import re
import math
import statistics
from typing import Any

from simpleeval import SimpleEval, NameNotDefined, EvalWithCompoundTypes


# Fields available in formula context (StatementTransaction fields + computed)
AVAILABLE_FIELDS: set[str] = {
    "income", "expense", "amount", "net", "direction",
    "date", "detail", "operation", "comment", "currency_op",
    "processing_date", "document_number", "note",
    "source_confidence", "row_index",
}

# Fields that must be exposed as strings so text functions never see None.
_TEXT_FIELDS: set[str] = {
    "direction", "date", "detail", "operation", "comment",
    "currency_op", "processing_date", "document_number", "note",
}

_VAR_RE = re.compile(r"\{(\w+)\}")


def _safe_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (ValueError, TypeError):
        return 0.0


def _build_context(row: dict[str, Any], row_index: int = 0) -> dict[str, Any]:
    """Build evaluation context from a row dict."""
    ctx: dict[str, Any] = {}
    for field in AVAILABLE_FIELDS:
        ctx[field] = row.get(field)

    # Expose every actual column key in the row (variants use keys like
    # "details_operation" that aren't in AVAILABLE_FIELDS). Text → str (None-safe),
    # numbers as-is, so formulas can reference any visible column.
    for key, value in row.items():
        if key in ("running_sum",):
            continue
        if isinstance(value, (int, float)):
            ctx[key] = value
        elif key not in ctx or not isinstance(ctx.get(key), (int, float)):
            ctx[key] = str(value) if value is not None else ""

    # Text fields → always strings so CONTAINS/STARTSWITH/etc. never hit None.
    for field in _TEXT_FIELDS:
        ctx[field] = str(row.get(field) or "")

    # Computed numeric helpers (locale-safe coercion).
    income = _safe_float(row.get("income"))
    expense = _safe_float(row.get("expense"))
    amount_raw = row.get("amount")
    amount = _safe_float(amount_raw) if amount_raw not in (None, "") else (income or expense)
    ctx["income"] = income
    ctx["expense"] = expense
    ctx["amount"] = amount
    ctx["net"] = income - expense
    ctx["source_confidence"] = _safe_float(row.get("source_confidence"))
    ctx["row_index"] = row_index
    # running_sum is injected by evaluate_column(); expose it (0.0 if absent) so
    # balance-style formulas like "running_sum" resolve instead of NameNotDefined.
    ctx["running_sum"] = _safe_float(row.get("running_sum"))
    return ctx


def _contains(haystack: Any, needle: Any) -> bool:
    """Case-insensitive substring check, None-safe (the core of text categorisation)."""
    return str(needle or "").lower() in str(haystack or "").lower()


def _regex_match(text: Any, pattern: Any) -> bool:
    try:
        return re.search(str(pattern or ""), str(text or ""), re.IGNORECASE) is not None
    except re.error:
        return False


def _safe_functions() -> dict[str, Any]:
    return {
        "round": round,
        "abs": abs,
        "int": int,
        "float": float,
        "str": str,
        "len": len,
        "min": min,
        "max": max,
        "upper": lambda s: str(s).upper() if s is not None else "",
        "lower": lambda s: str(s).lower() if s is not None else "",
        "trim": lambda s: str(s).strip() if s is not None else "",
        "concat": lambda *args: "".join(str(a) for a in args),
        "IF": lambda cond, a, b: a if cond else b,
        "ЕСЛИ": lambda cond, a, b: a if cond else b,
        "ISNULL": lambda v, default=0: default if v is None else v,
        "EMPTY": lambda v: v is None or str(v).strip() == "",
        # Text / categorisation helpers (all None-safe, case-insensitive).
        "CONTAINS": _contains,
        "СОДЕРЖИТ": _contains,
        "STARTSWITH": lambda s, p: str(s or "").lower().startswith(str(p or "").lower()),
        "ENDSWITH": lambda s, p: str(s or "").lower().endswith(str(p or "").lower()),
        "REGEX": _regex_match,
        "MATCH_ANY": lambda s, *needles: any(_contains(s, n) for n in needles),
        "sqrt": math.sqrt,
        "floor": math.floor,
        "ceil": math.ceil,
    }


def _preprocess(formula: str) -> str:
    """Replace {field} with field name for simpleeval."""
    return _VAR_RE.sub(r"\1", formula.strip())


class FormulaResult:
    __slots__ = ("value", "error", "provenance")

    def __init__(self, value: Any, error: str | None, provenance: str):
        self.value = value
        self.error = error
        self.provenance = provenance


def evaluate(formula: str, row: dict[str, Any], row_index: int = 0) -> FormulaResult:
    """
    Evaluate a formula against a single row context.
    Returns FormulaResult with .value, .error, .provenance.
    """
    provenance = f"formula_engine::{formula}"
    if not formula or not formula.strip():
        return FormulaResult(None, "empty formula", provenance)

    expr = _preprocess(formula)
    ctx = _build_context(row, row_index)

    try:
        ev = SimpleEval(names=ctx, functions=_safe_functions())
        result = ev.eval(expr)
        # Normalise floats to avoid float noise
        if isinstance(result, float):
            result = round(result, 10)
        return FormulaResult(result, None, provenance)
    except NameNotDefined as e:
        return FormulaResult(None, f"неизвестная переменная: {e}", provenance)
    except ZeroDivisionError:
        return FormulaResult(None, "деление на ноль", provenance)
    except Exception as e:  # noqa: BLE001
        return FormulaResult(None, str(e), provenance)


def evaluate_column(
    formula: str,
    rows: list[dict[str, Any]],
) -> list[FormulaResult]:
    """Evaluate formula for every row.

    `running_sum` is the cumulative (income − expense) up to and including the
    current row — i.e. a running balance — and is derived from the row data, NOT
    from the formula's own output (which would compound exponentially).
    """
    running_total: float = 0.0
    results: list[FormulaResult] = []

    for i, row in enumerate(rows):
        running_total += _safe_float(row.get("income")) - _safe_float(row.get("expense"))
        augmented = {**row, "running_sum": running_total}
        results.append(evaluate(formula, augmented, row_index=i + 1))

    return results


def validate_formula(formula: str) -> tuple[bool, str | None]:
    """
    Validate formula syntax without a real row.
    Returns (is_valid, error_message).

    A formula may reference arbitrary column keys (variants use keys like
    "details_operation"). We can't know them all here, so every identifier the
    formula references gets a dummy value — unknown columns default to a numeric
    dummy that survives both arithmetic and the (str-coercing) text helpers.
    """
    dummy_row: dict[str, Any] = {f: 100.0 for f in AVAILABLE_FIELDS}
    dummy_row.update({
        "direction": "outflow",
        "detail": "FACEBK *A2LN7VV6K2Покупка",
        "operation": "Покупка",
        "date": "2024-01-01",
        "comment": "",
        "note": "",
        "currency_op": "USD",
        "document_number": "1",
        "processing_date": "2024-01-01",
    })
    funcs = set(_safe_functions().keys())
    for ident in set(re.findall(r"[A-Za-zА-Яа-я_][A-Za-zА-Яа-я0-9_]*", _preprocess(formula))):
        if ident in funcs or ident in dummy_row or ident in {"running_sum", "True", "False", "None"}:
            continue
        dummy_row[ident] = 1.0

    result = evaluate(formula, dummy_row)
    if result.error:
        return False, result.error
    return True, None
