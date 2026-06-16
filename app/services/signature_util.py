"""Column-signature helpers for auto-matching transformation templates.

Mirrors the matching approach used for OCR mapping templates
(see ocr_mapping_template_service._match_score) but for parsed statement
variants: a template stores the normalised tokens of the columns it was built
on, and a freshly parsed statement is matched by Jaccard overlap.
"""
from __future__ import annotations

import re
from typing import Any

# Templates are auto-applied when column-structure overlap is at least this high.
MATCH_THRESHOLD = 0.6


def normalize_token(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _column_token(column: Any) -> str:
    if isinstance(column, dict):
        label = column.get("label") or column.get("key")
    else:
        label = getattr(column, "label", None) or getattr(column, "key", None)
    return normalize_token(label)


def signature_from_columns(columns: list[Any]) -> list[str]:
    """Ordered, de-duplicated normalised tokens for a list of columns."""
    seen: set[str] = set()
    out: list[str] = []
    for column in columns:
        token = _column_token(column)
        if token and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def jaccard(left: list[str] | set[str], right: list[str] | set[str]) -> float:
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
